import { useEffect, useEffectEvent, useRef, useState, type FormEvent } from 'react'
import { Check, CircleAlert, Mic, RotateCcw, Square, X } from 'lucide-react'

type Props = { onComplete: () => void; onDismiss: () => void }
type Phase = 'idle' | 'requesting' | 'recording' | 'review' | 'saving'

export default function WakeCalibrationDialog({ onComplete, onDismiss }: Props) {
  const [phase, setPhase] = useState<Phase>('idle')
  const [consent, setConsent] = useState(false)
  const [seconds, setSeconds] = useState(0)
  const [error, setError] = useState('')
  const [preview, setPreview] = useState('')
  const recorder = useRef<MediaRecorder | null>(null)
  const stream = useRef<MediaStream | null>(null)
  const clip = useRef<Blob | null>(null)
  const url = useRef<string | null>(null)
  const timeout = useRef<number | null>(null)
  const clock = useRef<number | null>(null)
  const request = useRef<AbortController | null>(null)
  const epoch = useRef(0)
  const player = useRef<HTMLAudioElement | null>(null)

  function releaseCapture() {
    if (timeout.current !== null) window.clearTimeout(timeout.current)
    if (clock.current !== null) window.clearInterval(clock.current)
    timeout.current = null
    clock.current = null
    stream.current?.getTracks().forEach((track) => track.stop())
    stream.current = null
  }

  const dispose = useEffectEvent(() => {
    epoch.current += 1
    request.current?.abort()
    if (recorder.current) {
      recorder.current.onstop = null
      if (recorder.current.state === 'recording') recorder.current.stop()
    }
    releaseCapture()
    player.current?.pause()
    if (url.current) URL.revokeObjectURL(url.current)
  })
  useEffect(() => () => { dispose() }, [])

  function stopRecording() {
    if (recorder.current?.state === 'recording') recorder.current.stop()
  }

  async function startRecording() {
    if (!consent || phase === 'recording' || phase === 'requesting' || phase === 'saving') return
    const ticket = ++epoch.current
    setError('')
    setPhase('requesting')
    setSeconds(0)
    clip.current = null
    player.current?.pause()
    if (url.current) URL.revokeObjectURL(url.current)
    url.current = null
    setPreview('')
    try {
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
        throw new Error('このブラウザーでは録音できません。')
      }
      const microphone = await navigator.mediaDevices.getUserMedia({ audio: {
        echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1,
      } })
      if (ticket !== epoch.current) { microphone.getTracks().forEach((track) => track.stop()); return }
      stream.current = microphone
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4'].find((type) => MediaRecorder.isTypeSupported(type))
      const recording = new MediaRecorder(microphone, mimeType ? { mimeType } : undefined)
      recorder.current = recording
      const chunks: Blob[] = []
      recording.ondataavailable = (event) => { if (event.data.size) chunks.push(event.data) }
      recording.onstop = () => {
        releaseCapture()
        if (ticket !== epoch.current) return
        const audio = new Blob(chunks, { type: recording.mimeType || 'audio/webm' })
        if (!audio.size) { setError('録音データがありません。もう一度録音してください。'); setPhase('idle'); return }
        clip.current = audio
        url.current = URL.createObjectURL(audio)
        setPreview(url.current)
        setPhase('review')
      }
      recording.onerror = () => {
        if (ticket !== epoch.current) return
        epoch.current += 1
        releaseCapture()
        if (recording.state === 'recording') recording.stop()
        setError('録音できませんでした。もう一度お試しください。')
        setPhase('idle')
      }
      recording.start()
      setPhase('recording')
      const started = Date.now()
      clock.current = window.setInterval(() => setSeconds(Math.min(15, Math.floor((Date.now() - started) / 1000))), 250)
      timeout.current = window.setTimeout(stopRecording, 15000)
    } catch (reason) {
      if (ticket !== epoch.current) return
      releaseCapture()
      setPhase('idle')
      setError(reason instanceof DOMException && reason.name === 'NotAllowedError'
        ? 'マイクへのアクセスが許可されていません。テキスト入力は引き続き利用できます。'
        : reason instanceof Error ? reason.message : '録音を開始できませんでした。')
    }
  }

  async function register(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!consent || !clip.current || phase !== 'review') return
    const ticket = epoch.current
    setPhase('saving')
    setError('')
    player.current?.pause()
    const controller = new AbortController()
    request.current = controller
    try {
      const form = new FormData()
      form.append('audio', clip.current, clip.current.type.includes('mp4') ? 'calibration.m4a' : 'calibration.webm')
      form.append('consent', 'true')
      const response = await fetch('/api/wake/calibration', { method: 'POST', body: form, signal: controller.signal })
      if (response.status === 409) { onComplete(); return }
      const result = await response.json().catch(() => null)
      if (!response.ok || !result?.calibrated) throw new Error(result?.detail ?? '登録できませんでした。もう一度お試しください。')
      if (!controller.signal.aborted && ticket === epoch.current) onComplete()
    } catch (reason) {
      if (controller.signal.aborted || ticket !== epoch.current) return
      setError(reason instanceof Error ? reason.message : '登録できませんでした。')
      setPhase('review')
    }
  }

  const busy = phase === 'saving' || phase === 'requesting'
  return (
    <div className="modal-backdrop">
      <form className="settings-dialog wake-calibration-dialog" role="dialog" aria-modal="true" aria-labelledby="wake-setup-title" onSubmit={register}>
        <div className="dialog-heading">
          <div><div className="eyebrow">VOICE SETUP</div><h2 id="wake-setup-title">初回音声セットアップ</h2></div>
          <button className="icon-button" type="button" title="後で設定する" aria-label="後で設定する" disabled={phase === 'saving'} onClick={onDismiss}><X size={17} /></button>
        </div>
        <p>いつもの声で「ねぇノルン」を4回、1秒程度ずつあけて話してください。</p>
        <p className="settings-help">このPC内だけで調整し、音声は外部へ送信しません。登録後は音響特徴のみを保存し、原音は削除します。</p>
        <label className="wake-calibration-consent"><input type="checkbox" checked={consent} disabled={busy || phase === 'recording'} onChange={(event) => setConsent(event.target.checked)} />音声特徴をこのPCに登録することに同意する</label>
        <div className="wake-calibration-recorder">
          <button className="add-event-button" type="button" disabled={!consent || busy || phase === 'recording'} onClick={() => void startRecording()}>{preview ? <RotateCcw size={16} /> : <Mic size={16} />}{preview ? '録り直す' : '録音開始'}</button>
          <button className="icon-button" type="button" title="録音終了" aria-label="録音終了" disabled={phase !== 'recording'} onClick={stopRecording}><Square size={16} /></button>
          <span role="status">{phase === 'recording' ? `録音中 ${seconds} / 15秒` : phase === 'requesting' ? 'マイク接続中' : phase === 'saving' ? '登録中' : preview ? '録音を確認して登録' : '未録音'}</span>
        </div>
        <progress className="wake-calibration-progress" aria-label="録音時間" max={15} value={seconds} />
        {preview && <audio ref={player} src={preview} controls className="wake-calibration-preview" />}
        {error && <div className="error-banner" role="alert"><CircleAlert size={16} /><span>{error}</span></div>}
        <div className="event-dialog-actions">
          <button className="cancel-button" type="button" disabled={phase === 'saving'} onClick={onDismiss}>後で</button>
          <button className="dialog-close" type="submit" disabled={!consent || phase !== 'review'}><Check size={16} />登録して完了</button>
        </div>
      </form>
    </div>
  )
}