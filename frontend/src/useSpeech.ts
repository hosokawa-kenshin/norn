import { useEffect, useRef, useState } from 'react'

type SpeechState = 'idle' | 'loading' | 'generating' | 'playing' | 'blocked' | 'failed'

export default function useSpeech() {
  const [enabled, setEnabled] = useState(() => localStorage.getItem('norn.speechEnabled') !== '0')
  const [state, setState] = useState<SpeechState>('idle')
  const [error, setError] = useState('')
  const [hasAudio, setHasAudio] = useState(false)
  const audio = useRef<HTMLAudioElement | null>(null)
  const enabledRef = useRef(enabled)
  const request = useRef<AbortController | null>(null)
  const url = useRef<string | null>(null)

  useEffect(() => {
    const player = new Audio()
    audio.current = player
    player.onended = () => setState('idle')
    player.onerror = () => { setState('failed'); setError('音声を再生できませんでした。') }
    return () => {
      request.current?.abort()
      player.pause()
      player.removeAttribute('src')
      if (url.current) URL.revokeObjectURL(url.current)
    }
  }, [])

  function stop() {
    request.current?.abort()
    request.current = null
    audio.current?.pause()
    if (audio.current) audio.current.currentTime = 0
    setState('idle')
  }

  function toggle() {
    const next = !enabled
    localStorage.setItem('norn.speechEnabled', next ? '1' : '0')
    enabledRef.current = next
    setEnabled(next)
    if (!next) stop()
  }

  async function play() {
    if (!audio.current?.src) return
    try {
      await audio.current.play()
      setState('playing')
      setError('')
    } catch (reason) {
      setState(reason instanceof DOMException && reason.name === 'NotAllowedError' ? 'blocked' : 'failed')
      setError('再生ボタンを押して音声を再生してください。')
    }
  }

  async function speak(text: string) {
    stop()
    setError('')
    if (audio.current) audio.current.removeAttribute('src')
    if (url.current) URL.revokeObjectURL(url.current)
    url.current = null
    setHasAudio(false)
    if (!enabledRef.current) return
    const controller = new AbortController()
    request.current = controller
    setState('loading')
    try {
      const deadline = Date.now() + 15 * 60 * 1000
      while (true) {
        const statusResponse = await fetch('/api/speech/status', { signal: controller.signal })
        if (!statusResponse.ok) throw new Error('音声サービスに接続できません。')
        const status = await statusResponse.json() as { state: string; error?: string }
        if (status.state === 'ready') break
        if (status.state !== 'loading' || Date.now() > deadline) {
          throw new Error(status.error ?? '音声サービスを利用できません。')
        }
        await new Promise<void>((resolve, reject) => {
          const abort = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
          const timer = window.setTimeout(() => {
            controller.signal.removeEventListener('abort', abort)
            resolve()
          }, 1500)
          controller.signal.addEventListener('abort', abort, { once: true })
          if (controller.signal.aborted) abort()
        })
      }
      setState('generating')
      const response = await fetch('/api/speech', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text }), signal: controller.signal,
      })
      if (!response.ok) {
        const result = await response.json().catch(() => null)
        throw new Error(result?.detail ?? '音声生成に失敗しました。')
      }
      const blob = await response.blob()
      if (controller.signal.aborted || !audio.current) return
      url.current = URL.createObjectURL(blob)
      audio.current.src = url.current
      setHasAudio(true)
      await play()
    } catch (reason) {
      if (controller.signal.aborted) return
      setState('failed')
      setError(reason instanceof Error ? reason.message : '音声生成に失敗しました。')
    }
  }

  return { enabled, state, error, hasAudio, toggle, stop, play, speak }
}