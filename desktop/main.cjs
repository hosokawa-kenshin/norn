const { app, BrowserWindow, Menu, Tray, nativeImage, session, ipcMain, shell, dialog, powerMonitor } = require('electron')
const fs = require('node:fs')
const path = require('node:path')
const assert = require('node:assert/strict')
const { spawn } = require('node:child_process')
const { createLocalServer, launchBackend } = require('./core.cjs')

app.setName('NORN')
app.setAppUserModelId('dev.norn.assistant')
if (process.env.NORN_DESKTOP_USER_DATA) app.setPath('userData', process.env.NORN_DESKTOP_USER_DATA)
const smoke = process.argv.includes('--smoke-test')
let shouldShow = !process.argv.includes('--background')
let window = null
let tray = null
let local = null
let backend = null
let quitting = false
let voicePhase = 'off'
let preferences = null
let logFile = null
const labels = { off: 'マイク停止中', starting: 'マイク準備中', standby: 'ねぇノルンを待機中', listening: '聞き取り中', processing: '処理中', playing: '再生中' }

function log(message) {
  if (logFile) fs.appendFileSync(logFile, `${new Date().toISOString()} ${message}`)
}

function focusWindow() {
  shouldShow = true
  if (!window || window.isDestroyed()) return
  if (window.isMinimized()) window.restore()
  window.show()
  window.focus()
}

function setAutoStart(enabled) {
  if (smoke) return
  app.setLoginItemSettings({ openAtLogin: enabled, path: process.execPath, args: [app.getAppPath(), '--background'] })
  preferences.autoStart = enabled
  fs.writeFileSync(path.join(app.getPath('userData'), 'preferences.json'), JSON.stringify(preferences, null, 2))
}

function refreshTray() {
  if (!tray) return
  tray.setToolTip(`NORN / ${labels[voicePhase] ?? '起動中'}`)
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: 'NORNを開く', click: focusWindow },
    { label: labels[voicePhase] ?? '起動中', enabled: false },
    { label: 'マイクを停止', enabled: voicePhase !== 'off', click: () => window?.webContents.send('norn:command', 'mute') },
    { type: 'separator' },
    { label: 'ログイン時に起動', type: 'checkbox', checked: Boolean(preferences.autoStart), click: (item) => {
      try { setAutoStart(item.checked); refreshTray() } catch (error) { log(`Autostart update failed: ${error.message}\n`) }
    } },
    { label: 'ログを表示', click: () => shell.showItemInFolder(logFile) },
    { type: 'separator' },
    { label: '終了', click: () => app.quit() },
  ]))
}

function trusted(url) {
  try { return new URL(url).origin === local?.origin } catch { return false }
}

function external(url) {
  try {
    const parsed = new URL(url)
    if (['https:', 'http:'].includes(parsed.protocol)) void shell.openExternal(parsed.toString())
  } catch {}
}

async function shutdown() {
  if (window && !window.isDestroyed()) window.destroy()
  tray?.destroy()
  tray = null
  await backend?.stop()
  await local?.close()
}

async function runSmokeTest() {
  const result = await window.webContents.executeJavaScript(`(async () => {
    const status = await (await fetch('/api/status')).json();
    const socketReady = await new Promise((resolve, reject) => {
      const socket = new WebSocket(location.origin.replace('http:', 'ws:') + '/api/wake/listen');
      const timeout = setTimeout(() => { socket.close(); reject(new Error('WebSocket startup timeout')); }, 20000);
      socket.onmessage = event => { const result = JSON.parse(event.data); clearTimeout(timeout); socket.close(); result.ready ? resolve(true) : reject(new Error('Wake worker failed')); };
      socket.onerror = () => { clearTimeout(timeout); reject(new Error('WebSocket connection failed')); };
    });
    return { bridge: window.nornDesktop?.platform === 'win32', api: status.stt.engine === 'faster-whisper', socketReady,
      title: document.title, microphone: document.querySelector('[aria-label="音声会話の状態"]')?.textContent };
  })()`)
  assert.equal(result.bridge, true)
  assert.equal(result.api, true)
  assert.equal(result.socketReady, true)
  assert.equal(window.webContents.getBackgroundThrottling(), false)
  if (fs.existsSync(path.join(__dirname, 'frontend/voice/desktop-smoke-norn.wav'))) {
    await window.webContents.executeJavaScript(`(async () => {
      const context = new AudioContext({ sampleRate: 48000 });
      const destination = context.createMediaStreamDestination();
      window.__nornSmokeAudio = { context, destination };
      navigator.mediaDevices.getUserMedia = async () => destination.stream.clone();
      const originalFetch = window.fetch;
      window.fetch = function(input, options) {
        const url = typeof input === 'string' ? input : input.url;
        if (url.endsWith('/api/transcribe')) return Promise.resolve(new Response(JSON.stringify({text:''}), {headers:{'Content-Type':'application/json'}}));
        if (url.endsWith('/api/chat')) return Promise.reject(new Error('No chat calls in smoke audio test'));
        return originalFetch.call(this, input, options);
      };
      document.querySelector('.handsfree-controls input').click();
      await new Promise((resolve, reject) => {
        const deadline = Date.now() + 20000;
        const check = () => {
          if (document.querySelector('[aria-label="音声会話の状態"]').textContent.includes('待機中')) return resolve();
          if (Date.now() > deadline) return reject(new Error('VAD initialization failed'));
          setTimeout(check, 50);
        };
        check();
      });
    })()`)
    window.close()
    assert.equal(window.isVisible(), false)
    await window.webContents.executeJavaScript(`(async () => {
      const { context, destination } = window.__nornSmokeAudio;
      await context.resume();
      const buffer = await context.decodeAudioData(await (await fetch('/voice/desktop-smoke-norn.wav')).arrayBuffer());
      const source = context.createBufferSource(); source.buffer = buffer; source.connect(destination); source.start();
      await new Promise((resolve, reject) => {
        const deadline = Date.now() + 15000;
        const check = () => {
          if (document.querySelector('[aria-label="音声会話の状態"]').textContent === '聞き取り中') return resolve();
          if (Date.now() > deadline) return reject(new Error('Hidden-window wake detection failed'));
          setTimeout(check, 50);
        };
        check();
      });
    })()`)
    result.hiddenWakeDetection = true
    await window.webContents.executeJavaScript(`window.__nornSmokeMuted = new Promise(resolve => {
      const remove = window.nornDesktop.onCommand(() => { remove(); resolve(true); });
    }); true`)
    window.webContents.send('norn:command', 'mute')
    await window.webContents.executeJavaScript('window.__nornSmokeMuted')
    result.trayMuteBridge = true
  }
  window.close()
  assert.equal(window.isDestroyed(), false)
  assert.equal(window.isVisible(), false)
  const secondInstance = new Promise((resolve) => app.once('second-instance', resolve))
  const duplicate = spawn(process.execPath, [app.getAppPath(), '--background'], { windowsHide: true, stdio: 'ignore' })
  await Promise.all([secondInstance, new Promise((resolve, reject) => {
    duplicate.once('error', reject)
    duplicate.once('exit', (code) => code === 0 ? resolve() : reject(new Error('Second instance failed')))
  })])
  assert.equal(window.isVisible(), true)
  result.singleInstance = true
  log('SMOKE PASS ' + JSON.stringify(result) + '\n')
  fs.writeFileSync(path.join(app.getPath('userData'), 'smoke-result.json'), JSON.stringify({
    ok: true, ...result, closeHidesWindow: true, backgroundThrottling: false,
  }, null, 2))
  app.quit()
}

async function start() {
  await app.whenReady()
  const config = JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8'))
  fs.mkdirSync(app.getPath('userData'), { recursive: true })
  logFile = path.join(app.getPath('userData'), 'desktop.log')
  if (fs.existsSync(logFile) && fs.statSync(logFile).size > 5 * 1024 * 1024) fs.renameSync(logFile, logFile + '.old')
  try { preferences = JSON.parse(fs.readFileSync(path.join(app.getPath('userData'), 'preferences.json'), 'utf8')) }
  catch { preferences = { autoStart: config.autoStart === true } }
  if (process.argv.includes('--disable-autostart')) { setAutoStart(false); app.quit(); return }
  setAutoStart(preferences.autoStart === true)
  if (smoke) config.testDatabase = '/tmp/norn-desktop-smoke.sqlite3'
  try {
    local = await createLocalServer(path.join(__dirname, 'frontend'), preferences.frontendPort ?? 0)
  } catch (error) {
    if (error.code !== 'EADDRINUSE') throw error
    local = await createLocalServer(path.join(__dirname, 'frontend'))
  }
  if (!smoke) {
    preferences.frontendPort = Number(new URL(local.origin).port)
    fs.writeFileSync(path.join(app.getPath('userData'), 'preferences.json'), JSON.stringify(preferences, null, 2))
  }
  const icon = nativeImage.createFromPath(path.join(__dirname, 'assets/norn.png'))
  if (icon.isEmpty()) throw new Error('NORN application icon is missing')
  tray = new Tray(icon.resize({ width: 20, height: 20 }))
  tray.on('double-click', focusWindow)
  refreshTray()
  session.defaultSession.setPermissionCheckHandler((contents, permission, origin, details) => {
    return contents === window?.webContents && trusted(origin) && permission === 'media' && details.mediaType === 'audio'
  })
  session.defaultSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(contents === window?.webContents && trusted(details.requestingUrl) && permission === 'media'
      && details.mediaTypes?.length > 0 && details.mediaTypes.every((type) => type === 'audio'))
  })
  session.defaultSession.webRequest.onBeforeRequest({ urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'] }, (details, callback) => {
    callback({ cancel: !trusted(details.url.replace(/^ws:/, 'http:').replace(/^wss:/, 'https:')) })
  })
  backend = launchBackend(config, local.origin, log)
  backend.exited.then((code) => {
    if (!quitting) {
      log(`Backend exited unexpectedly: ${code}\n`)
      window?.webContents.send('norn:command', 'mute')
      dialog.showErrorBox('NORN', 'WSLバックエンドが終了しました。トレイから終了して起動し直してください。')
    }
  })
  const ready = await backend.ready
  local.setBackend(ready.api_url)
  window = new BrowserWindow({
    title: 'NORN', width: 1180, height: 820, minWidth: 390, minHeight: 600, show: false, icon,
    autoHideMenuBar: true,
    webPreferences: { preload: path.join(__dirname, 'preload.cjs'), contextIsolation: true,
      nodeIntegration: false, sandbox: true, backgroundThrottling: false },
  })
  window.on('close', (event) => { if (!quitting) { event.preventDefault(); window.hide() } })
  window.webContents.setWindowOpenHandler(({ url }) => { external(url); return { action: 'deny' } })
  window.webContents.on('will-navigate', (event, url) => { if (!trusted(url)) { event.preventDefault(); external(url) } })
  ipcMain.on('norn:voice-state', (event, phase) => {
    if (!quitting && !window.isDestroyed() && event.sender === window.webContents && Object.hasOwn(labels, phase)) { voicePhase = phase; refreshTray() }
  })
  powerMonitor.on('suspend', () => window?.webContents.send('norn:command', 'mute'))
  powerMonitor.on('resume', () => window?.webContents.send('norn:command', 'mute'))
  await window.loadURL(local.origin)
  if (shouldShow || smoke) focusWindow()
  if (smoke) await runSmokeTest()
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', focusWindow)
  app.on('window-all-closed', () => {})
  app.on('activate', focusWindow)
  app.on('before-quit', (event) => {
    if (quitting) return
    event.preventDefault()
    quitting = true
    shutdown().catch((error) => log(`Shutdown failed: ${error.message}\n`)).finally(() => app.exit(process.exitCode ?? 0))
  })
  start().catch((error) => {
    log(`Startup failed: ${error.stack}\n`)
    if (!smoke) dialog.showErrorBox('NORNを起動できません', 'WSLと依存環境を確認してください。詳細はNORNのdesktop.logに保存されています。')
    process.exitCode = 1
    app.quit()
  })
}