const { contextBridge, ipcRenderer } = require('electron')

contextBridge.exposeInMainWorld('nornDesktop', {
  platform: 'win32',
  reportVoiceState(phase) {
    if (['off', 'starting', 'standby', 'listening', 'processing', 'playing'].includes(phase)) {
      ipcRenderer.send('norn:voice-state', phase)
    }
  },
  onCommand(handler) {
    const listener = (_event, command) => { if (command === 'mute') handler(command) }
    ipcRenderer.on('norn:command', listener)
    return () => ipcRenderer.removeListener('norn:command', listener)
  },
})