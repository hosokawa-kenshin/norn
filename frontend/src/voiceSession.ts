export type SessionPhase = 'off' | 'starting' | 'standby' | 'listening' | 'processing' | 'playing'

export class UtteranceBuffer {
	private preroll: Float32Array[] = []
	private frames: Float32Array[] = []
	private size = 0
	private recording = false

	reset() { this.preroll = []; this.frames = []; this.size = 0; this.recording = false }

	begin() {
		if (this.recording) return
		this.frames = this.preroll
		this.preroll = []
		this.size = this.frames.reduce((total, frame) => total + frame.length, 0)
		this.recording = true
	}

	push(frame: Float32Array, active: boolean) {
		if (!active) { this.reset(); return true }
		if (!this.recording) {
			this.preroll.push(frame.slice())
			if (this.preroll.length > 20) this.preroll.shift()
			return true
		}
		if (this.size + frame.length > 16000 * 30) { this.reset(); return false }
		this.frames.push(frame.slice())
		this.size += frame.length
		return true
	}

	take() {
		const audio = new Float32Array(this.size)
		let offset = 0
		for (const frame of this.frames) { audio.set(frame, offset); offset += frame.length }
		this.reset()
		return audio
	}
}

export class VoiceSession {
	phase: SessionPhase = 'off'
	lastActivity = 0
	idleMs = 60000

	get active() { return ['listening', 'processing', 'playing'].includes(this.phase) }
	standby(now: number) { this.phase = 'standby'; this.lastActivity = now }

	wake(now: number) {
		if (this.phase !== 'standby') return false
		this.phase = 'listening'
		this.lastActivity = now
		return true
	}

	activity(now: number) { if (this.active) this.lastActivity = now }

	tick(now: number, processing: boolean, playing: boolean) {
		if (!this.active) return
		if (processing || playing) {
			this.phase = playing ? 'playing' : 'processing'
			this.lastActivity = now
		} else if (now - this.lastActivity >= this.idleMs) {
			this.standby(now)
		} else {
			this.phase = 'listening'
		}
	}
}

export function isConversationEnd(text: string) {
	return /^(またね|待機して|会話を終了|会話を終わりにして|おやすみ)$/.test(text.replace(/[\s。、！？!?.,]/g, ''))
}

export function stripWakePhrase(text: string) {
	return text.replace(/^(?:ノルン|のるん|norn)[、,。\s]*/i, '').trim()
}
