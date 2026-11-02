export {}

declare global {
  interface Window {
    nornDesktop?: {
      platform: 'win32'
      reportVoiceState(phase: string): void
      onCommand(handler: (command: 'mute') => void): () => void
    }
  }
}