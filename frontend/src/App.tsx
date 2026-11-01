/*
import heroImg from './assets/hero.png'
import reactLogo from './assets/react.svg'
import viteLogo from './assets/vite.svg'
import './App.css'
/*

function App() {
  const [count, setCount] = useState(0)

  return (
    <>
      <section id="center">
        <div className="hero">
          <img src={heroImg} className="base" width="170" height="179" alt="" />
          <img src={reactLogo} className="framework" alt="React logo" />
          <img src={viteLogo} className="vite" alt="Vite logo" />
        </div>
        <div>
          <h1>Get started</h1>
          <p>
            Edit <code>src/App.tsx</code> and save to test <code>HMR</code>
          </p>
        </div>
        <button
          type="button"
          className="counter"
          onClick={() => setCount((count) => count + 1)}
        >
          Count is {count}
        </button>
      </section>

      <div className="ticks"></div>

      <section id="next-steps">
        <div id="docs">
          <svg className="icon" role="presentation" aria-hidden="true">
            {/* import { useEffect, useRef, useState } from 'react'
            import {
              Activity,
              AudioLines,
              Check,
              ChevronRight,
              CircleAlert,
              Clock3,
              Mic,
              MicOff,
              Plus,
              Send,
              Settings2,
              Sparkles,
            } from 'lucide-react'
            import './App.css'

            type Message = {
              role: 'user' | 'assistant'
              content: string
              created_at?: string
            }

            type Conversation = {
              id: string
              title: string
              updated_at: string
            }

            type ApiStatus = {
              stt: {
                engine: string
                model: string
                device: string
                model_loaded: boolean
              }
              llm: {
                configured: boolean
                model: string
              }
            }

            type VoiceState = 'idle' | 'recording' | 'transcribing'

            function formatTime(seconds: number) {
              const minutes = Math.floor(seconds / 60).toString().padStart(2, '0')
              const remainder = (seconds % 60).toString().padStart(2, '0')
              return `${minutes}:${remainder}`
            }

            function formatDate(value: string) {
              return new Intl.DateTimeFormat('ja-JP', {
                month: 'numeric',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
              }).format(new Date(value))
            }

            async function responseError(response: Response) {
              const result = await response.json().catch(() => null)
              return typeof result?.detail === 'string'
                ? result.detail
                : `リクエストに失敗しました (${response.status})`
            }

            function App() {
              const [status, setStatus] = useState<ApiStatus | null>(null)
              const [conversations, setConversations] = useState<Conversation[]>([])
              const [activeConversationId, setActiveConversationId] = useState<string | null>(
                () => localStorage.getItem('norn.activeConversation'),
              )
              const [messages, setMessages] = useState<Message[]>([])
              const [draft, setDraft] = useState('')
              const [voiceState, setVoiceState] = useState<VoiceState>('idle')
              const [recordingSeconds, setRecordingSeconds] = useState(0)
              const [isSending, setIsSending] = useState(false)
              const [error, setError] = useState('')
              const [showSettings, setShowSettings] = useState(false)
              const recorderRef = useRef<MediaRecorder | null>(null)
              const streamRef = useRef<MediaStream | null>(null)
              const chunksRef = useRef<Blob[]>([])
              const recordingStartedAt = useRef(0)
              const messagesEndRef = useRef<HTMLDivElement | null>(null)

              useEffect(() => {
                let active = true
                fetch('/api/status')
                  .then((response) => {
                    if (!response.ok) throw new Error('バックエンドに接続できません')
                    return response.json() as Promise<ApiStatus>
                  })
                  .then((value) => {
                    if (active) setStatus(value)
                  })
                  .catch(() => {
                    if (active) setError('バックエンドが起動していません。READMEの手順で起動してください。')
                  })

                fetch('/api/conversations')
                  .then((response) => (response.ok ? response.json() : []))
                  .then((value: Conversation[]) => {
                    if (active) setConversations(value)
                        * /}
                  .catch(() => undefined)

                return () => {
                  active = false
                          * /}
              }, [])

              useEffect(() => {
                if (!activeConversationId) {
                  setMessages([])
                  return
                }

                let active = true
                fetch(`/api/conversations/${activeConversationId}/messages`)
                  .then(async (response) => {
                    if (!response.ok) throw new Error('会話を読み込めませんでした')
                    return (await response.json()) as Message[]
                  })
                  .then((value) => {
                    if (active) setMessages(value)
                  })
                  .catch(() => {
                    if (!active) return
                    localStorage.removeItem('norn.activeConversation')
                    setActiveConversationId(null)
                  })

                return () => {
                  active = false
                }
              }, [activeConversationId])

              useEffect(() => {
                messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
              }, [messages, voiceState, isSending])

              useEffect(() => {
                if (voiceState !== 'recording') return
                const timer = window.setInterval(() => {
                  setRecordingSeconds(Math.floor((Date.now() - recordingStartedAt.current) / 1000))
                }, 250)
                return () => window.clearInterval(timer)
              }, [voiceState])

              useEffect(
                () => () => {
                  if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
                  streamRef.current?.getTracks().forEach((track) => track.stop())
                },
                [],
              )

              async function refreshConversations() {
                const response = await fetch('/api/conversations')
                if (response.ok) setConversations((await response.json()) as Conversation[])
              }

              async function transcribeAudio(blob: Blob) {
                setVoiceState('transcribing')
                setError('')
                try {
                  const form = new FormData()
                  const extension = blob.type.includes('mp4') ? 'm4a' : 'webm'
                  form.append('audio', blob, `norn-recording.${extension}`)
                  const response = await fetch('/api/transcribe', { method: 'POST', body: form })
                  if (!response.ok) throw new Error(await responseError(response))
                  const result = (await response.json()) as { text: string }
                  if (!result.text.trim()) throw new Error('音声から言葉を認識できませんでした。もう一度お試しください。')
                  setDraft((current) => (current.trim() ? `${current.trim()} ${result.text}` : result.text))
                } catch (reason) {
                  setError(reason instanceof Error ? reason.message : '音声を文字起こしできませんでした。')
                } finally {
                  setVoiceState('idle')
                }
              }

              async function startRecording() {
                setError('')
                if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
                  setError('このブラウザーでは録音できません。最新のChromeまたはEdgeをお試しください。')
                  return
                }

                try {
                  const stream = await navigator.mediaDevices.getUserMedia({
                    audio: { echoCancellation: true, noiseSuppression: true },
                  })
                  streamRef.current = stream
                  chunksRef.current = []
                  const mimeType = ['audio/webm;codecs=opus', 'audio/mp4'].find((type) =>
                    MediaRecorder.isTypeSupported(type),
                  )
                  const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
                  recorderRef.current = recorder
                  recorder.ondataavailable = (event) => {
                    if (event.data.size > 0) chunksRef.current.push(event.data)
                  }
                  recorder.onstop = () => {
                    stream.getTracks().forEach((track) => track.stop())
                    streamRef.current = null
                    const recording = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
                    if (recording.size === 0) {
                      setVoiceState('idle')
                      setError('録音データがありません。少し長く話してから停止してください。')
                      return
                    }
                    void transcribeAudio(recording)
                  }
                  recorder.start(250)
                  recordingStartedAt.current = Date.now()
                  setRecordingSeconds(0)
                  setVoiceState('recording')
                } catch (reason) {
                  const denied = reason instanceof DOMException && reason.name === 'NotAllowedError'
                  setError(
                    denied
                      ? 'マイクへのアクセスが許可されていません。ブラウザーのサイト設定を確認してください。'
                      : 'マイクを開始できませんでした。接続とブラウザー設定を確認してください。',
                  )
                }
              }

              function stopRecording() {
                if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
              }

              async function sendMessage(event: React.FormEvent<HTMLFormElement>) {
                event.preventDefault()
                const content = draft.trim()
                if (!content || isSending || voiceState !== 'idle') return

                setError('')
                setIsSending(true)
                try {
                  const response = await fetch('/api/chat', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ conversation_id: activeConversationId, content }),
                  })
                  if (!response.ok) throw new Error(await responseError(response))
                  const result = (await response.json()) as {
                    conversation_id: string
                    user_message: Message
                    assistant_message: Message
                  }
                  localStorage.setItem('norn.activeConversation', result.conversation_id)
                  setActiveConversationId(result.conversation_id)
                  setMessages((current) => [...current, result.user_message, result.assistant_message])
                  setDraft('')
                  await refreshConversations()
                } catch (reason) {
                  setError(reason instanceof Error ? reason.message : '応答を取得できませんでした。')
                } finally {
                  setIsSending(false)
                }
              }
                            * /}
              function startNewConversation() {
                localStorage.removeItem('norn.activeConversation')
                setActiveConversationId(null)
                setMessages([])
                setDraft('')
                setError('')
              }

              const statusLabel =
                voiceState === 'recording'
                  ? '録音中'
  * /
                  : voiceState === 'transcribing'
                    ? '文字起こし中'
                    : '音声入力'

              return (
                <div className="app-shell">
                  <aside className="sidebar">
                    <div className="brand-lockup">
                      <div className="brand-mark" aria-hidden="true"><AudioLines size={22} /></div>
                      <div>
                        <div className="brand-name">NORN</div>
                        <div className="brand-subtitle">PERSONAL SYSTEM</div>
                      </div>
                    </div>

                    <button className="new-conversation" type="button" onClick={startNewConversation}>
                      <Plus size={17} />
                      <span>新しい会話</span>
                      <span className="shortcut">N</span>
                    </button>

                    <div className="sidebar-section-label">会話履歴</div>
                    <nav className="conversation-list" aria-label="会話履歴">
                      {conversations.length === 0 ? (
                        <div className="empty-history">会話はまだありません</div>
                      ) : (
                        conversations.map((conversation) => (
                          <button
                            className={`conversation-link ${activeConversationId === conversation.id ? 'is-active' : ''}`}
                            key={conversation.id}
                            type="button"
                            onClick={() => setActiveConversationId(conversation.id)}
                            title={conversation.title}
                          >
                            <span className="conversation-title">{conversation.title}</span>
                            <span className="conversation-date">{formatDate(conversation.updated_at)}</span>
                          </button>
                        ))
                      )}
                    </nav>

                    <div className="sidebar-bottom">
                      <div className="local-status">
                        <span className="status-indicator" />
                        <div>
                          <div className="local-status-title">ローカル音声認識</div>
                          <div className="local-status-detail">
                            {status ? `${status.stt.model} / ${status.stt.device}` : '接続確認中'}
                          </div>
                        </div>
                        <Activity size={15} className="status-activity" />
                      </div>
                      <button
                        className="settings-link"
                        type="button"
                        onClick={() => setShowSettings(true)}
                      >
                        <Settings2 size={16} />
                        <span>設定</span>
                        <ChevronRight size={15} />
                      </button>
                    </div>
                  </aside>

                  <main className="main-panel">
                    <header className="topbar">
                      <div>
                        <div className="eyebrow">NORN / VOICE INTERFACE</div>
                        <h1>会話</h1>
                      </div>
                      <div className="topbar-state">
                        <span className={`llm-dot ${status?.llm.configured ? 'is-ready' : ''}`} />
                        <span>{status?.llm.configured ? 'AI接続済み' : 'AI未設定'}</span>
                      </div>
                    </header>

                    <section className="conversation-stage" aria-label="会話">
                      {messages.length === 0 ? (
                        <div className="welcome-state">
                          <div className="welcome-orbit" aria-hidden="true">
                            <div className="orbit-ring orbit-ring-outer" />
                            <div className="orbit-ring orbit-ring-inner" />
                            <div className="orbit-core"><AudioLines size={28} /></div>
                            <span className="orbit-node orbit-node-one" />
                            <span className="orbit-node orbit-node-two" />
                          </div>
                          <div className="welcome-copy">
                            <div className="eyebrow">LISTENING SESSION / 01</div>
                            <h2>今日は、どんな一日でしたか。</h2>
                            <p>声で話しかけてください。予定や気づきを一緒に整理します。</p>
                          </div>
                          <div className="suggestion-row" aria-label="話題の候補">
                            {['今日の予定を確認したい', 'タスクを追加したい', '少し振り返りたい'].map((suggestion) => (
                              <button key={suggestion} type="button" onClick={() => setDraft(suggestion)}>
                                <Sparkles size={14} />
                                {suggestion}
                              </button>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <div className="message-list">
                          {messages.map((message, index) => (
                            <article className={`message-row ${message.role}`} key={`${message.created_at ?? index}-${index}`}>
                              <div className="message-avatar" aria-hidden="true">
                                {message.role === 'assistant' ? <AudioLines size={16} /> : 'YOU'}
                              </div>
                              <div className="message-body">
                                <div className="message-label">
                                  {message.role === 'assistant' ? 'NORN' : 'あなた'}
                                  {message.created_at && <time>{formatDate(message.created_at)}</time>}
                                </div>
                                <p>{message.content}</p>
                              </div>
                            </article>
                          ))}
                          {isSending && (
                            <div className="thinking-state">
                              <span className="thinking-pulse" />
                              NORNが考えています
                            </div>
                          )}
                          <div ref={messagesEndRef} />
                        </div>
                      )}
                    </section>

                    <section className="composer-wrap" aria-label="音声入力">
                      {error && (
                        <div className="error-banner" role="alert">
                          <CircleAlert size={16} />
                          <span>{error}</span>
                          <button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button>
                        </div>
                      )}
                      {status && !status.llm.configured && (
                        <div className="setup-notice">
                          <span>LLM接続が未設定です。設定から接続先を確認してください。</span>
                          <button type="button" onClick={() => setShowSettings(true)}>設定を見る</button>
                        </div>
                      )}
                      <form className="composer" onSubmit={sendMessage}>
                        <div className="composer-topline">
                          <div className={`voice-state-label ${voiceState}`}>
                            {voiceState === 'recording' ? <span className="recording-dot" /> : <AudioLines size={14} />}
                            <span>{statusLabel}</span>
                            {voiceState === 'recording' && <span className="recording-timer"><Clock3 size={13} />{formatTime(recordingSeconds)}</span>}
                          </div>
                          <span className="privacy-note"><Check size={13} /> 音声はローカルで処理</span>
                        </div>
                        <div className="composer-controls">
                          <button
                            className={`record-button ${voiceState === 'recording' ? 'is-recording' : ''}`}
                            type="button"
                            onClick={voiceState === 'recording' ? stopRecording : startRecording}
                            disabled={voiceState === 'transcribing' || isSending}
                            aria-label={voiceState === 'recording' ? '録音を停止' : '録音を開始'}
                            title={voiceState === 'recording' ? '録音を停止' : '録音を開始'}
                          >
                            {voiceState === 'recording' ? <MicOff size={20} /> : <Mic size={20} />}
                          </button>
                          <div className="draft-area">
                            <textarea
                              value={draft}
                              onChange={(event) => setDraft(event.target.value)}
                              placeholder={voiceState === 'transcribing' ? '音声を文字起こししています…' : '認識した文章を確認・修正できます'}
                              aria-label="送信するメッセージ"
                              rows={2}
                              maxLength={4000}
                              disabled={voiceState === 'transcribing' || isSending}
                            />
                            <div className="draft-meta">
                              <span>{voiceState === 'recording' ? '話し終えたら停止してください' : '音声のほか、ここに入力して送信できます'}</span>
                              <span>{draft.length}/4000</span>
                            </div>
                          </div>
                          <button
                            className="send-button"
                            type="submit"
                            disabled={!draft.trim() || isSending || voiceState !== 'idle'}
                            aria-label="メッセージを送信"
                            title="送信"
                          >
                            {isSending ? <span className="send-spinner" /> : <Send size={18} />}
                          </button>
                        </div>
                      </form>
                      <div className="composer-footnote">送信前に文字起こし内容を確認できます</div>
                    </section>
                  </main>

                  {showSettings && (
                    <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
                      if (event.target === event.currentTarget) setShowSettings(false)
                    }}>
                      <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
                        <div className="dialog-heading">
                          <div>
                            <div className="eyebrow">SYSTEM CONFIGURATION</div>
                            <h2 id="settings-title">接続設定</h2>
                          </div>
                          <button className="icon-button" type="button" onClick={() => setShowSettings(false)} aria-label="閉じる">×</button>
                        </div>
                        <div className="setting-row">
                          <span>音声認識</span>
                          <strong>ローカル / {status?.stt.model ?? '未接続'}</strong>
                        </div>
                        <div className="setting-row">
                          <span>LLM</span>
                          <strong>{status?.llm.configured ? status.llm.model : '未設定'}</strong>
                        </div>
                        <p className="settings-help">LLMの接続先とAPIキーは、NORN直下の <code>.env</code> で設定します。キーはブラウザーへ送られません。</p>
                        <button className="dialog-close" type="button" onClick={() => setShowSettings(false)}>閉じる</button>
                      </section>
                    </div>
                  )}
                </div>
              )
            }

            export default App
            * /}
*/
