import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Activity,
  AudioLines,
  CalendarDays,
  Check,
  ChevronRight,
  ChevronLeft,
  CircleAlert,
  Clock3,
  Mic,
  MicOff,
  Plus,
  Pencil,
  Send,
  Settings2,
  Sparkles,
  Trash2,
} from 'lucide-react'

type Message = { role: 'user' | 'assistant'; content: string; created_at?: string }
type Conversation = { id: string; title: string; updated_at: string }
type ApiStatus = {
  stt: { engine: string; model: string; device: string; model_loaded: boolean }
  llm: { configured: boolean; model: string }
}
type VoiceState = 'idle' | 'recording' | 'transcribing'
type AppView = 'chat' | 'schedule'
type CalendarMode = 'day' | 'week'
type EventItem = {
  id: string
  title: string
  description: string
  start_at: string
  end_at: string
}
type EventDraft = {
  title: string
  description: string
  date: string
  startTime: string
  endTime: string
}

function localDateKey(date: Date) {
  const year = date.getFullYear()
  const month = (date.getMonth() + 1).toString().padStart(2, '0')
  const day = date.getDate().toString().padStart(2, '0')
  return `${year}-${month}-${day}`
}

function getDateRange(value: string, mode: CalendarMode) {
  const selected = new Date(`${value}T00:00:00`)
  const start = new Date(selected)
  if (mode === 'week') start.setDate(start.getDate() - ((start.getDay() + 6) % 7))
  const end = new Date(start)
  end.setDate(end.getDate() + (mode === 'week' ? 7 : 1))
  return { start, end }
}

function formatLongDate(date: Date) {
  return new Intl.DateTimeFormat('ja-JP', {
    year: 'numeric', month: 'long', day: 'numeric', weekday: 'long',
  }).format(date)
}

function timeInputValue(value: string) {
  const date = new Date(value)
  return `${date.getHours().toString().padStart(2, '0')}:${date.getMinutes().toString().padStart(2, '0')}`
}

function formatEventTime(value: string) {
  return new Intl.DateTimeFormat('ja-JP', { hour: '2-digit', minute: '2-digit' }).format(new Date(value))
}

function createEventDraft(date: string): EventDraft {
  return { title: '', description: '', date, startTime: '10:00', endTime: '11:00' }
}

function formatTime(seconds: number) {
  return `${Math.floor(seconds / 60).toString().padStart(2, '0')}:${(seconds % 60).toString().padStart(2, '0')}`
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat('ja-JP', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

async function responseError(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string'
    ? result.detail
    : `リクエストに失敗しました (${response.status})`
}

export default function NornApp() {
  const [status, setStatus] = useState<ApiStatus | null>(null)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(() => localStorage.getItem('norn.activeConversation'))
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [recordingSeconds, setRecordingSeconds] = useState(0)
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState('')
  const [showSettings, setShowSettings] = useState(false)
  const [view, setView] = useState<AppView>('chat')
  const [calendarMode, setCalendarMode] = useState<CalendarMode>('week')
  const [selectedDate, setSelectedDate] = useState(() => localDateKey(new Date()))
  const [events, setEvents] = useState<EventItem[]>([])
  const [eventError, setEventError] = useState('')
  const [showEventEditor, setShowEventEditor] = useState(false)
  const [editingEventId, setEditingEventId] = useState<string | null>(null)
  const [eventDraft, setEventDraft] = useState<EventDraft>(() => createEventDraft(localDateKey(new Date())))
  const recorderRef = useRef<MediaRecorder | null>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const chunksRef = useRef<Blob[]>([])
  const recordingStartedAt = useRef(0)
  const messagesEndRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let active = true
    fetch('/api/status')
      .then((response) => {
        if (!response.ok) throw new Error()
        return response.json() as Promise<ApiStatus>
      })
      .then((value) => { if (active) setStatus(value) })
      .catch(() => { if (active) setError('バックエンドが起動していません。READMEの手順で起動してください。') })
    fetch('/api/conversations')
      .then((response) => (response.ok ? response.json() : []))
      .then((value: Conversation[]) => { if (active) setConversations(value) })
      .catch(() => undefined)
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (view !== 'schedule') return
    const range = getDateRange(selectedDate, calendarMode)
    const params = new URLSearchParams({ start: range.start.toISOString(), end: range.end.toISOString() })
    let active = true
    fetch(`/api/events?${params}`)
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response))
        return (await response.json()) as EventItem[]
      })
      .then((value) => {
        if (!active) return
        setEvents(value)
        setEventError('')
      })
      .catch((reason) => {
        if (active) setEventError(reason instanceof Error ? reason.message : '予定を取得できませんでした。')
      })
    return () => { active = false }
  }, [view, selectedDate, calendarMode])

  useEffect(() => {
    if (!activeId) {
      return
    }
    let active = true
    fetch(`/api/conversations/${activeId}/messages`)
      .then(async (response) => {
        if (!response.ok) throw new Error()
        return (await response.json()) as Message[]
      })
      .then((value) => { if (active) setMessages(value) })
      .catch(() => {
        if (!active) return
        localStorage.removeItem('norn.activeConversation')
        setActiveId(null)
      })
    return () => { active = false }
  }, [activeId])

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

  useEffect(() => () => {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
    streamRef.current?.getTracks().forEach((track) => track.stop())
  }, [])

  async function refreshConversations() {
    const response = await fetch('/api/conversations')
    if (response.ok) setConversations((await response.json()) as Conversation[])
  }

  async function transcribeAudio(blob: Blob) {
    setVoiceState('transcribing')
    setError('')
    try {
      const form = new FormData()
      form.append('audio', blob, `norn-recording.${blob.type.includes('mp4') ? 'm4a' : 'webm'}`)
      const response = await fetch('/api/transcribe', { method: 'POST', body: form })
      if (!response.ok) throw new Error(await responseError(response))
      const result = (await response.json()) as { text: string }
      if (!result.text.trim()) throw new Error('音声から言葉を認識できませんでした。もう一度お試しください。')
      setDraft((current) => current.trim() ? `${current.trim()} ${result.text}` : result.text)
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
      const mimeType = ['audio/webm;codecs=opus', 'audio/mp4'].find((type) => MediaRecorder.isTypeSupported(type))
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
      recorderRef.current = recorder
      recorder.ondataavailable = (event) => { if (event.data.size) chunksRef.current.push(event.data) }
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop())
        streamRef.current = null
        const recording = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' })
        if (!recording.size) {
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
      setError(denied
        ? 'マイクへのアクセスが許可されていません。ブラウザーのサイト設定を確認してください。'
        : 'マイクを開始できませんでした。接続とブラウザー設定を確認してください。')
    }
  }

  function stopRecording() {
    if (recorderRef.current?.state === 'recording') recorderRef.current.stop()
  }

  async function sendMessage(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const content = draft.trim()
    if (!content || isSending || voiceState !== 'idle') return
    setError('')
    setIsSending(true)
    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: activeId, content }),
      })
      if (!response.ok) throw new Error(await responseError(response))
      const result = (await response.json()) as {
        conversation_id: string
        user_message: Message
        assistant_message: Message
      }
      localStorage.setItem('norn.activeConversation', result.conversation_id)
      setActiveId(result.conversation_id)
      setMessages((current) => [...current, result.user_message, result.assistant_message])
      setDraft('')
      await refreshConversations()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '応答を取得できませんでした。')
    } finally {
      setIsSending(false)
    }
  }

  function startNewConversation() {
    localStorage.removeItem('norn.activeConversation')
    setActiveId(null)
    setMessages([])
    setDraft('')
    setError('')
  }

  function openNewEvent() {
    setEditingEventId(null)
    setEventDraft(createEventDraft(selectedDate))
    setEventError('')
    setShowEventEditor(true)
  }

  function openEditEvent(event: EventItem) {
    setEditingEventId(event.id)
    setEventDraft({
      title: event.title,
      description: event.description,
      date: localDateKey(new Date(event.start_at)),
      startTime: timeInputValue(event.start_at),
      endTime: timeInputValue(event.end_at),
    })
    setEventError('')
    setShowEventEditor(true)
  }

  async function saveEvent(formEvent: FormEvent<HTMLFormElement>) {
    formEvent.preventDefault()
    setEventError('')
    const response = await fetch(editingEventId ? `/api/events/${editingEventId}` : '/api/events', {
      method: editingEventId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: eventDraft.title,
        description: eventDraft.description,
        start_at: `${eventDraft.date}T${eventDraft.startTime}:00+09:00`,
        end_at: `${eventDraft.date}T${eventDraft.endTime}:00+09:00`,
      }),
    }).catch(() => null)

    if (!response) {
      setEventError('バックエンドに接続できません。')
      return
    }
    if (!response.ok) {
      setEventError(await responseError(response))
      return
    }

    const saved = (await response.json()) as EventItem
    setEvents((current) => {
      const next = editingEventId
        ? current.map((event) => event.id === saved.id ? saved : event)
        : [...current, saved]
      return next.sort((left, right) => left.start_at.localeCompare(right.start_at))
    })
    setShowEventEditor(false)
  }

  async function deleteEvent(event: EventItem) {
    if (!window.confirm(`「${event.title}」を削除しますか？`)) return
    const response = await fetch(`/api/events/${event.id}`, { method: 'DELETE' }).catch(() => null)
    if (!response) {
      setEventError('バックエンドに接続できません。')
      return
    }
    if (!response.ok) {
      setEventError(await responseError(response))
      return
    }
    setEvents((current) => current.filter((item) => item.id !== event.id))
  }

  function moveCalendar(offset: number) {
    const date = new Date(`${selectedDate}T00:00:00`)
    date.setDate(date.getDate() + offset * (calendarMode === 'week' ? 7 : 1))
    setSelectedDate(localDateKey(date))
  }

  const calendarRange = getDateRange(selectedDate, calendarMode)
  const calendarDays = Array.from({ length: calendarMode === 'week' ? 7 : 1 }, (_, index) => {
    const day = new Date(calendarRange.start)
    day.setDate(day.getDate() + index)
    return day
  })
  const rangeLabel = calendarMode === 'day'
    ? formatLongDate(calendarRange.start)
    : `${new Intl.DateTimeFormat('ja-JP', { month: 'long', day: 'numeric' }).format(calendarRange.start)} - ${new Intl.DateTimeFormat('ja-JP', { month: 'long', day: 'numeric' }).format(new Date(calendarRange.end.getTime() - 86400000))}`

  const stateLabel = voiceState === 'recording' ? '録音中' : voiceState === 'transcribing' ? '文字起こし中' : '音声入力'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand-lockup">
          <div className="brand-mark" aria-hidden="true"><AudioLines size={22} /></div>
          <div><div className="brand-name">NORN</div><div className="brand-subtitle">PERSONAL SYSTEM</div></div>
        </div>
        <button className="new-conversation" type="button" onClick={startNewConversation}>
          <Plus size={17} /><span>新しい会話</span><span className="shortcut">N</span>
        </button>
        <div className="sidebar-section-label">会話履歴</div>
        <nav className="conversation-list" aria-label="会話履歴">
          {conversations.length === 0 ? <div className="empty-history">会話はまだありません</div> : conversations.map((conversation) => (
            <button
              className={`conversation-link ${activeId === conversation.id ? 'is-active' : ''}`}
              key={conversation.id}
              type="button"
              onClick={() => setActiveId(conversation.id)}
              title={conversation.title}
            >
              <span className="conversation-title">{conversation.title}</span>
              <span className="conversation-date">{formatDate(conversation.updated_at)}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-bottom">
          <div className="local-status">
            <span className="status-indicator" />
            <div>
              <div className="local-status-title">ローカル音声認識</div>
              <div className="local-status-detail">{status ? `${status.stt.model} / ${status.stt.device}` : '接続確認中'}</div>
            </div>
            <Activity size={15} className="status-activity" />
          </div>
          <button className="settings-link" type="button" onClick={() => setShowSettings(true)}>
            <Settings2 size={16} /><span>設定</span><ChevronRight size={15} />
          </button>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div><div className="eyebrow">NORN / PERSONAL SYSTEM</div><h1>{view === 'chat' ? '会話' : '予定'}</h1></div>
          <div className="topbar-actions">
            <nav className="main-view-switch" aria-label="表示切替">
              <button className={view === 'chat' ? 'is-active' : ''} type="button" onClick={() => setView('chat')}><AudioLines size={15} />会話</button>
              <button className={view === 'schedule' ? 'is-active' : ''} type="button" onClick={() => setView('schedule')}><CalendarDays size={15} />予定</button>
            </nav>
            <div className="topbar-state"><span className={`llm-dot ${status?.llm.configured ? 'is-ready' : ''}`} />
              <span>{status?.llm.configured ? 'AI接続済み' : 'AI未設定'}</span>
            </div>
          </div>
        </header>

        {view === 'chat' ? <section className="conversation-stage" aria-label="会話">
          {messages.length === 0 ? (
            <div className="welcome-state">
              <div className="welcome-orbit" aria-hidden="true">
                <div className="orbit-ring orbit-ring-outer" /><div className="orbit-ring orbit-ring-inner" />
                <div className="orbit-core"><AudioLines size={28} /></div>
                <span className="orbit-node orbit-node-one" /><span className="orbit-node orbit-node-two" />
              </div>
              <div className="welcome-copy">
                <div className="eyebrow">LISTENING SESSION / 01</div>
                <h2>今日は、どんな一日でしたか。</h2>
                <p>声で話しかけてください。予定や気づきを一緒に整理します。</p>
              </div>
              <div className="suggestion-row" aria-label="話題の候補">
                {['今日の予定を確認したい', 'タスクを追加したい', '少し振り返りたい'].map((suggestion) => (
                  <button key={suggestion} type="button" onClick={() => setDraft(suggestion)}><Sparkles size={14} />{suggestion}</button>
                ))}
              </div>
            </div>
          ) : (
            <div className="message-list">
              {messages.map((message, index) => (
                <article className={`message-row ${message.role}`} key={`${message.created_at ?? index}-${index}`}>
                  <div className="message-avatar" aria-hidden="true">{message.role === 'assistant' ? <AudioLines size={16} /> : 'YOU'}</div>
                  <div className="message-body">
                    <div className="message-label">{message.role === 'assistant' ? 'NORN' : 'あなた'}{message.created_at && <time>{formatDate(message.created_at)}</time>}</div>
                    <p>{message.content}</p>
                  </div>
                </article>
              ))}
              {isSending && <div className="thinking-state"><span className="thinking-pulse" />NORNが考えています</div>}
              <div ref={messagesEndRef} />
            </div>
          )}
        </section> : <section className="calendar-stage" aria-label="予定管理">
          <div className="calendar-toolbar">
            <div className="calendar-range-controls">
              <button className="calendar-icon-button" type="button" onClick={() => moveCalendar(-1)} aria-label="前の期間" title="前の期間"><ChevronLeft size={17} /></button>
              <button className="today-button" type="button" onClick={() => setSelectedDate(localDateKey(new Date()))}>今日</button>
              <button className="calendar-icon-button" type="button" onClick={() => moveCalendar(1)} aria-label="次の期間" title="次の期間"><ChevronRight size={17} /></button>
              <h2>{rangeLabel}</h2>
            </div>
            <div className="calendar-actions">
              <div className="calendar-mode-switch" aria-label="表示期間">
                <button className={calendarMode === 'day' ? 'is-active' : ''} type="button" onClick={() => setCalendarMode('day')}>日</button>
                <button className={calendarMode === 'week' ? 'is-active' : ''} type="button" onClick={() => setCalendarMode('week')}>週</button>
              </div>
              <button className="add-event-button" type="button" onClick={openNewEvent}><Plus size={16} />予定を追加</button>
            </div>
          </div>
          {eventError && <div className="error-banner calendar-error" role="alert"><CircleAlert size={16} /><span>{eventError}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setEventError('')}>×</button></div>}
          <div className={`calendar-days ${calendarMode}`}>
            {calendarDays.map((day) => {
              const dayKey = localDateKey(day)
              const dayEvents = events.filter((event) => localDateKey(new Date(event.start_at)) === dayKey)
              return <section className="calendar-day" key={dayKey} aria-label={formatLongDate(day)}>
                <header className="calendar-day-heading">
                  <div><span className="calendar-day-name">{new Intl.DateTimeFormat('ja-JP', { weekday: 'long' }).format(day)}</span><strong>{new Intl.DateTimeFormat('ja-JP', { month: 'numeric', day: 'numeric' }).format(day)}</strong></div>
                  <span className="event-count">{dayEvents.length}件</span>
                </header>
                {dayEvents.length === 0 ? <p className="day-empty">予定なし</p> : <div className="event-list">
                  {dayEvents.map((event) => <article className="event-row" key={event.id}>
                    <div className="event-time"><strong>{formatEventTime(event.start_at)}</strong><span>{formatEventTime(event.end_at)}</span></div>
                    <div className="event-color-mark" />
                    <div className="event-content"><h3>{event.title}</h3>{event.description && <p>{event.description}</p>}</div>
                    <div className="event-controls">
                      <button className="calendar-icon-button" type="button" onClick={() => openEditEvent(event)} aria-label={`${event.title}を編集`} title="編集"><Pencil size={15} /></button>
                      <button className="calendar-icon-button danger" type="button" onClick={() => void deleteEvent(event)} aria-label={`${event.title}を削除`} title="削除"><Trash2 size={15} /></button>
                    </div>
                  </article>)}
                </div>}
              </section>
            })}
          </div>
          {events.length === 0 && !eventError && <div className="calendar-empty-note"><CalendarDays size={18} /><span>この期間に予定はありません</span></div>}
        </section>}

        <section className="composer-wrap" aria-label="音声入力">
          {error && <div className="error-banner" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button></div>}
          {status && !status.llm.configured && <div className="setup-notice"><span>LLM接続が未設定です。設定から接続先を確認してください。</span><button type="button" onClick={() => setShowSettings(true)}>設定を見る</button></div>}
          <form className="composer" onSubmit={sendMessage}>
            <div className="composer-topline">
              <div className={`voice-state-label ${voiceState}`}>
                {voiceState === 'recording' ? <span className="recording-dot" /> : <AudioLines size={14} />}
                <span>{stateLabel}</span>
                {voiceState === 'recording' && <span className="recording-timer"><Clock3 size={13} />{formatTime(recordingSeconds)}</span>}
              </div>
              <span className="privacy-note"><Check size={13} />音声はローカルで処理</span>
            </div>
            <div className="composer-controls">
              <button
                className={`record-button ${voiceState === 'recording' ? 'is-recording' : ''}`}
                type="button"
                onClick={voiceState === 'recording' ? stopRecording : startRecording}
                disabled={voiceState === 'transcribing' || isSending}
                aria-label={voiceState === 'recording' ? '録音を停止' : '録音を開始'}
                title={voiceState === 'recording' ? '録音を停止' : '録音を開始'}
              >{voiceState === 'recording' ? <MicOff size={20} /> : <Mic size={20} />}</button>
              <div className="draft-area">
                <textarea value={draft} onChange={(event) => setDraft(event.target.value)}
                  placeholder={voiceState === 'transcribing' ? '音声を文字起こししています…' : '認識した文章を確認・修正できます'}
                  aria-label="送信するメッセージ" rows={2} maxLength={4000}
                  disabled={voiceState === 'transcribing' || isSending} />
                <div className="draft-meta"><span>{voiceState === 'recording' ? '話し終えたら停止してください' : '音声のほか、ここに入力して送信できます'}</span><span>{draft.length}/4000</span></div>
              </div>
              <button className="send-button" type="submit" disabled={!draft.trim() || isSending || voiceState !== 'idle'} aria-label="メッセージを送信" title="送信">
                {isSending ? <span className="send-spinner" /> : <Send size={18} />}
              </button>
            </div>
          </form>
          <div className="composer-footnote">送信前に文字起こし内容を確認できます</div>
        </section>
      </main>

      {showSettings && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget) setShowSettings(false)
      }}>
        <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
          <div className="dialog-heading"><div><div className="eyebrow">SYSTEM CONFIGURATION</div><h2 id="settings-title">接続設定</h2></div>
            <button className="icon-button" type="button" onClick={() => setShowSettings(false)} aria-label="閉じる">×</button>
          </div>
          <div className="setting-row"><span>音声認識</span><strong>ローカル / {status?.stt.model ?? '未接続'}</strong></div>
          <div className="setting-row"><span>LLM</span><strong>{status?.llm.configured ? status.llm.model : '未設定'}</strong></div>
          <p className="settings-help">LLMの接続先とAPIキーは、NORN直下の <code>.env</code> で設定します。キーはブラウザーへ送られません。</p>
          <button className="dialog-close" type="button" onClick={() => setShowSettings(false)}>閉じる</button>
        </section>
      </div>}

      {showEventEditor && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget) setShowEventEditor(false)
      }}>
        <form className="settings-dialog event-dialog" role="dialog" aria-modal="true" aria-labelledby="event-dialog-title" onSubmit={saveEvent}>
          <div className="dialog-heading"><div><div className="eyebrow">SCHEDULE / EVENT</div><h2 id="event-dialog-title">{editingEventId ? '予定を編集' : '予定を追加'}</h2></div>
            <button className="icon-button" type="button" onClick={() => setShowEventEditor(false)} aria-label="閉じる">×</button>
          </div>
          {eventError && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{eventError}</span></div>}
          <label className="event-field">予定名<input autoFocus required maxLength={120} value={eventDraft.title} onChange={(event) => setEventDraft({ ...eventDraft, title: event.target.value })} placeholder="例: 定例ミーティング" /></label>
          <label className="event-field">日付<input required type="date" value={eventDraft.date} onChange={(event) => setEventDraft({ ...eventDraft, date: event.target.value })} /></label>
          <div className="event-time-fields">
            <label className="event-field">開始<input required type="time" value={eventDraft.startTime} onChange={(event) => setEventDraft({ ...eventDraft, startTime: event.target.value })} /></label>
            <label className="event-field">終了<input required type="time" value={eventDraft.endTime} onChange={(event) => setEventDraft({ ...eventDraft, endTime: event.target.value })} /></label>
          </div>
          <label className="event-field">メモ<textarea maxLength={2000} rows={3} value={eventDraft.description} onChange={(event) => setEventDraft({ ...eventDraft, description: event.target.value })} placeholder="場所や準備事項" /></label>
          <div className="event-dialog-actions"><button className="cancel-button" type="button" onClick={() => setShowEventEditor(false)}>キャンセル</button><button className="dialog-close" type="submit">{editingEventId ? '変更を保存' : '予定を保存'}</button></div>
        </form>
      </div>}
    </div>
  )
}