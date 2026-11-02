import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { UtteranceBuffer, VoiceSession, isConversationEnd, stripWakePhrase, type SessionPhase } from './voiceSession'
import type { MicVAD } from '@ricky0123/vad-web'

type Options = {
	processing: boolean
	playing: boolean
	onText: (text: string, signal: AbortSignal) => Promise<void>
	onInterrupt: () => void
}

export default function useHandsFree(options: Options) {
	const latest = useRef(options)
	useEffect(() => { latest.current = options }, [options])
	const session = useRef(new VoiceSession())
	const utterance = useRef(new UtteranceBuffer())
	const [phase, setPhase] = useState<SessionPhase>('off')
	const [error, setError] = useState('')
	const [phrase, setPhrase] = useState('ノルン')
	const vad = useRef<MicVAD | null>(null)
	const socket = useRef<WebSocket | null>(null)
	const context = useRef<AudioContext | null>(null)
	const stream = useRef<MediaStream | null>(null)
	const request = useRef<AbortController | null>(null)
	const timer = useRef<number | null>(null)
	const epoch = useRef(0)
	const inSpeech = useRef(false)
	const capture = useRef(false)
	const firstUtterance = useRef(false)
	const requestBusy = useRef(false)

	function publish() { setPhase(session.current.phase) }

	function endSession() {
		request.current?.abort()
		capture.current = false
		utterance.current.reset()
		firstUtterance.current = false
		latest.current.onInterrupt()
		if (session.current.phase === 'off' || session.current.phase === 'starting') return
		session.current.standby(Date.now())
		if (socket.current?.readyState === WebSocket.OPEN) socket.current.send('reset')
		publish()
	}

	async function disable() {
		epoch.current += 1
		request.current?.abort()
		latest.current.onInterrupt()
		const previous = vad.current
		vad.current = null
		const previousContext = context.current
		context.current = null
		socket.current?.close()
		socket.current = null
		if (timer.current !== null) window.clearInterval(timer.current)
		timer.current = null
		stream.current?.getTracks().forEach((track) => track.stop())
		stream.current = null
		session.current.phase = 'off'
		capture.current = false
		utterance.current.reset()
		inSpeech.current = false
		requestBusy.current = false
		publish()
		await previous?.destroy().catch(() => undefined)
		await previousContext?.close().catch(() => undefined)
	}

	async function enable() {
		await disable()
		const ticket = epoch.current
		setError('')
		session.current.phase = 'starting'
		publish()
		const startup = new AbortController()
		request.current = startup
		try {
			if (!navigator.mediaDevices?.getUserMedia || typeof AudioContext === 'undefined') throw new Error('このブラウザーでは連続音声入力を利用できません。')
			const audioContext = new AudioContext()
			context.current = audioContext
			await audioContext.resume()
			const response = await fetch('/api/wake/status', { signal: startup.signal })
			if (!response.ok) throw new Error('ウェイクワードの状態を取得できません。')
			const status = await response.json() as { available: boolean; phrase: string }
			if (!status.available) throw new Error('日本語ウェイクワードモデルを配置してください。')
			setPhrase(status.phrase)
			const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/wake/listen`)
			socket.current = ws
			await new Promise<void>((resolve, reject) => {
				let ready = false
				const timeout = window.setTimeout(() => reject(new Error('ウェイクワード検出の接続がタイムアウトしました。')), 15000)
				ws.onmessage = (event) => {
					if (epoch.current !== ticket) return
					const message = JSON.parse(event.data) as { ready?: boolean; wake?: boolean; error?: string }
					if (message.error) { window.clearTimeout(timeout); reject(new Error(message.error)); return }
					if (message.ready) { ready = true; window.clearTimeout(timeout); resolve() }
					if (message.wake && session.current.wake(Date.now())) {
						utterance.current.reset()
						capture.current = inSpeech.current
						if (capture.current) utterance.current.begin()
						firstUtterance.current = true
						const oscillator = audioContext.createOscillator()
						const gain = audioContext.createGain()
						gain.gain.value = 0.045
						oscillator.frequency.value = 880
						oscillator.connect(gain).connect(audioContext.destination)
						oscillator.start()
						oscillator.stop(audioContext.currentTime + 0.09)
						publish()
					}
				}
				ws.onerror = () => { window.clearTimeout(timeout); reject(new Error('ウェイクワード検出へ接続できません。')) }
				ws.onclose = () => {
					window.clearTimeout(timeout)
					if (!ready) reject(new Error('ウェイクワード検出との接続が終了しました。'))
					if (epoch.current === ticket) { setError('ウェイクワード検出との接続が終了しました。'); void disable() }
				}
			})
			const { MicVAD: VAD, utils } = await import('@ricky0123/vad-web')
			const microphone = await VAD.new({
				model: 'v5', startOnLoad: false, audioContext,
				baseAssetPath: '/voice/', onnxWASMBasePath: '/voice/',
				redemptionMs: 1200, minSpeechMs: 400, preSpeechPadMs: 300,
				positiveSpeechThreshold: 0.7, negativeSpeechThreshold: 0.4,
				getStream: async () => {
					const media = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } })
					if (epoch.current !== ticket) { media.getTracks().forEach((track) => track.stop()); throw new Error('Aborted') }
					stream.current = media
					return media
				},
				onFrameProcessed: (_probabilities, frame) => {
					if (epoch.current !== ticket) return
					if (session.current.phase === 'standby' && ws.readyState === WebSocket.OPEN) {
						if (ws.bufferedAmount > 65536) { setError('ウェイクワード処理が追いつきません。'); void disable(); return }
						ws.send(frame.slice().buffer)
					}
					if (!utterance.current.push(frame, session.current.active && !requestBusy.current && !latest.current.processing)) {
						capture.current = false
						setError('発話が30秒を超えました。短く区切って話してください。')
					}
					if (capture.current) session.current.activity(Date.now())
				},
				onSpeechRealStart: () => {
					if (epoch.current !== ticket) return
					inSpeech.current = true
					if (!session.current.active || requestBusy.current || latest.current.processing) return
					capture.current = true
					utterance.current.begin()
					session.current.activity(Date.now())
					if (latest.current.playing) latest.current.onInterrupt()
				},
				onVADMisfire: () => { if (epoch.current === ticket) { inSpeech.current = false; capture.current = false } },
				onSpeechEnd: async () => {
					if (epoch.current !== ticket) return
					inSpeech.current = false
					const accepted = capture.current
					capture.current = false
					const samples = utterance.current.take()
					if (!session.current.active || !accepted || !samples.length || requestBusy.current || latest.current.processing) return
					requestBusy.current = true
					session.current.phase = 'processing'
					publish()
					const controller = new AbortController()
					request.current = controller
					try {
						const form = new FormData()
						form.append('audio', new Blob([utils.encodeWAV(samples)], { type: 'audio/wav' }), 'utterance.wav')
						const response = await fetch('/api/transcribe', { method: 'POST', body: form, signal: controller.signal })
						if (!response.ok) throw new Error('音声を文字起こしできませんでした。')
						const result = await response.json() as { text: string }
						let text = result.text.trim()
						if (firstUtterance.current) text = stripWakePhrase(text)
						firstUtterance.current = false
						if (controller.signal.aborted || epoch.current !== ticket) return
						if (isConversationEnd(text)) { endSession(); return }
						if (text) await latest.current.onText(text, controller.signal)
					} catch (reason) {
						if (!controller.signal.aborted && epoch.current === ticket) setError(reason instanceof Error ? reason.message : '音声処理に失敗しました。')
					} finally {
						if (epoch.current === ticket) {
							requestBusy.current = false
							session.current.activity(Date.now())
							session.current.tick(Date.now(), latest.current.processing, latest.current.playing)
							publish()
						}
					}
				},
			})
			if (epoch.current !== ticket) { await microphone.destroy(); return }
			vad.current = microphone
			session.current.standby(Date.now())
			await microphone.start()
			if (epoch.current !== ticket) { await microphone.destroy(); return }
			publish()
			timer.current = window.setInterval(() => {
				const wasActive = session.current.active
				session.current.tick(Date.now(), requestBusy.current || latest.current.processing, latest.current.playing)
				if (wasActive && !session.current.active && ws.readyState === WebSocket.OPEN) ws.send('reset')
				publish()
			}, 150)
		} catch (reason) {
			if (epoch.current !== ticket) return
			setError(reason instanceof DOMException && reason.name === 'NotAllowedError'
				? 'マイクへのアクセスが許可されていません。ブラウザーのサイト設定を確認してください。'
				: reason instanceof Error ? reason.message : 'マイク待機を開始できませんでした。')
			await disable()
		}
	}

	const cleanup = useEffectEvent(() => { void disable() })
	useEffect(() => () => { cleanup() }, [])
	return { phase, error, phrase, enable, disable, endSession }
}
