const http = require('node:http')
const fs = require('node:fs/promises')
const path = require('node:path')
const { spawn } = require('node:child_process')
const { createInterface } = require('node:readline')

const types = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.mjs': 'text/javascript',
  '.css': 'text/css', '.json': 'application/json', '.wasm': 'application/wasm',
  '.onnx': 'application/octet-stream', '.png': 'image/png', '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2', '.wav': 'audio/wav',
}

async function createLocalServer(directory, port = 0) {
  const root = path.resolve(directory)
  let target = null
  let origin = ''
  const sockets = new Set()
  const server = http.createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://127.0.0.1')
      if (url.pathname === '/api' || url.pathname.startsWith('/api/')) {
        if (!target) { response.writeHead(503); response.end('Backend is starting'); return }
        const upstream = http.request(new URL(url.pathname + url.search, target), {
          method: request.method, headers: { ...request.headers, host: target.host },
        }, (result) => {
          response.writeHead(result.statusCode, result.headers)
          result.pipe(response)
        })
        upstream.on('error', () => {
          if (!response.headersSent) response.writeHead(502)
          response.end('Backend unavailable')
        })
        request.on('aborted', () => upstream.destroy())
        request.pipe(upstream)
        return
      }
      if (!['GET', 'HEAD'].includes(request.method)) { response.writeHead(405); response.end(); return }
      const filename = path.resolve(root, '.' + decodeURIComponent(url.pathname))
      if (filename !== root && !filename.startsWith(root + path.sep)) {
        response.writeHead(403); response.end(); return
      }
      const candidate = filename === root || url.pathname === '/' ? path.join(root, 'index.html') : filename
      let contents
      let served = candidate
      try { contents = await fs.readFile(candidate) } catch (error) {
        if (error.code !== 'ENOENT' || path.extname(candidate)) throw error
        served = path.join(root, 'index.html')
        contents = await fs.readFile(served)
      }
      response.writeHead(200, {
        'Content-Type': types[path.extname(served)] ?? 'application/octet-stream',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
        'Content-Security-Policy': "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; style-src 'self' 'unsafe-inline'; connect-src 'self'; worker-src 'self' blob:; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'",
      })
      response.end(request.method === 'HEAD' ? undefined : contents)
    } catch (error) {
      response.writeHead(error.code === 'ENOENT' ? 404 : 400)
      response.end('Resource unavailable')
    }
  })
  server.on('connection', (socket) => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)) })
  server.on('upgrade', (request, client, head) => {
    const url = new URL(request.url, 'http://127.0.0.1')
    if (!target || !url.pathname.startsWith('/api/') || request.headers.origin !== origin) {
      client.end('HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n')
      return
    }
    const upstream = http.request(new URL(url.pathname + url.search, target), {
      headers: { ...request.headers, host: target.host },
    })
    upstream.on('upgrade', (result, socket, upstreamHead) => {
      const headers = []
      for (let index = 0; index < result.rawHeaders.length; index += 2) headers.push(`${result.rawHeaders[index]}: ${result.rawHeaders[index + 1]}`)
      client.write(`HTTP/1.1 101 Switching Protocols\r\n${headers.join('\r\n')}\r\n\r\n`)
      if (head.length) socket.write(head)
      if (upstreamHead.length) client.write(upstreamHead)
      client.pipe(socket).pipe(client)
      client.on('close', () => socket.destroy())
      socket.on('error', () => client.destroy())
    })
    upstream.on('response', () => client.destroy())
    upstream.on('error', () => client.destroy())
    client.on('error', () => upstream.destroy())
    upstream.end()
  })
  await new Promise((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', resolve)
  })
  origin = `http://127.0.0.1:${server.address().port}`
  return {
    origin,
    setBackend(address) {
      const url = new URL(address)
      if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) throw new Error('Backend must be loopback HTTP')
      target = url
    },
    async close() {
      for (const socket of sockets) socket.destroy()
      await new Promise((resolve) => server.close(resolve))
    },
  }
}

function launchBackend(config, frontendOrigin, log = () => {}) {
  if (!config.distribution || !config.projectRoot?.startsWith('/')) throw new Error('Invalid WSL configuration')
  const environment = []
  if (config.testDatabase) environment.push(`NORN_DB_PATH=${config.testDatabase}`, 'NORN_TTS_ENABLED=0')
  const child = spawn('wsl.exe', [
    '-d', config.distribution, '--cd', config.projectRoot, '--', 'env', ...environment,
    '.venv/bin/python', '-u', 'backend/desktop_service.py', '--frontend-origin', frontendOrigin,
  ], { windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'] })
  let readyData = null
  const exited = new Promise((resolve) => child.once('exit', (code) => resolve(code)))
  const ready = new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new Error('WSL backend startup timed out')), 90000)
    const lines = createInterface({ input: child.stdout })
    lines.on('line', (line) => {
      if (!line.startsWith('NORN_DESKTOP_READY ')) { log(line + '\n'); return }
      try {
        const data = JSON.parse(line.slice('NORN_DESKTOP_READY '.length))
        const address = new URL(data.api_url)
        if (address.protocol !== 'http:' || address.hostname !== '127.0.0.1' || !Number.isInteger(data.pid)) throw new Error('Invalid backend startup message')
        readyData = data
        clearTimeout(timeout)
        resolve(data)
      } catch (error) { clearTimeout(timeout); reject(error) }
    })
    child.once('error', (error) => { clearTimeout(timeout); reject(error) })
    child.once('exit', () => { clearTimeout(timeout); if (!readyData) reject(new Error('WSL backend exited before startup')) })
  })
  child.stderr.on('data', (data) => log(data.toString()))
  child.stdin.on('error', () => {})
  return {
    child, ready, exited,
    async stop() {
      if (child.exitCode !== null) return
      child.stdin.end('shutdown\n')
      let timeout
      const clean = await Promise.race([exited.then(() => true), new Promise((resolve) => { timeout = setTimeout(() => resolve(false), 22000) })])
      clearTimeout(timeout)
      if (!clean && readyData) {
        spawn('wsl.exe', ['-d', config.distribution, '--', 'kill', '-TERM', String(readyData.pid)], { windowsHide: true, stdio: 'ignore' })
      }
      if (!clean) child.kill()
    },
  }
}

module.exports = { createLocalServer, launchBackend }