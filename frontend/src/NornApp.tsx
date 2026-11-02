import { useEffect, useRef, useState, type FormEvent } from 'react'
import {
  Activity,
  AudioLines,
  CalendarDays,
  Check,
  ChevronRight,
  ChevronLeft,
  ClipboardList,
  CircleAlert,
  Clock3,
  Mic,
  MicOff,
  Plus,
  Pencil,
  Send,
  Settings2,
  Sparkles,
  Target,
  Trash2,
  Play,
  Square,
} from 'lucide-react'
import GoalsView from './GoalsView'
import TasksView from './TasksView'
import useSpeech from './useSpeech'

type Message = { role: 'user' | 'assistant'; content: string; created_at?: string }
type Conversation = { id: string; title: string; updated_at: string }
type ApiStatus = {
  stt: { engine: string; model: string; device: string; model_loaded: boolean }
  llm: { configured: boolean; model: string }
}
type VoiceState = 'idle' | 'recording' | 'transcribing'
type AppView = 'chat' | 'schedule' | 'tasks' | 'goals'
type CalendarMode = 'day' | 'week'
type EventItem = {
  id: string
  title: string
  description: string
  start_at: string
  end_at: string
  is_all_day: boolean
  recurrence: string[]
  series_id?: string | null
  occurrence_id?: string
  series_title?: string
  series_description?: string
  series_start_at?: string
  series_end_at?: string
  series_is_all_day?: boolean
  series_recurrence?: string[]
}
type CalendarConflict = {
  event_id: string
  title: string
  local: { title: string; start_at: string; end_at: string; description: string; deleted?: boolean }
  remote: { title?: string; start_at?: string; end_at?: string; description?: string; deleted?: boolean }
}
type GoogleCalendarStatus = {
  configured: boolean
  connected: boolean
  calendar_id: string
  sync_interval_seconds: number
  last_sync_at: string | null
  last_error: string | null
  conflicts: CalendarConflict[]
}
type GoogleCalendarChoice = { id: string; name: string; primary: boolean; time_zone: string }
type TaskAllocation = {
  id: string
  task_id: string
  title: string
  start_at: string
  end_at: string
  estimated_minutes: number
  status: string
}
type EventDraft = {
  title: string
  description: string
  date: string
  endDate: string
  startTime: string
  endTime: string
  isAllDay: boolean
  recurrence: string[]
  repeat: string
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
  return {
    title: '', description: '', date, endDate: date, startTime: '10:00', endTime: '11:00',
    isAllDay: false, recurrence: [], repeat: 'NONE',
  }
}

function recurrenceChoice(recurrence: string[]) {
  if (!recurrence.length) return 'NONE'
  if (recurrence.length !== 1) return 'CUSTOM'
  const rule = recurrence.find((line) => line.startsWith('RRULE:')) ?? ''
  if (rule === 'RRULE:FREQ=DAILY') return 'DAILY'
  if (rule === 'RRULE:FREQ=MONTHLY') return 'MONTHLY'
  if (/^RRULE:FREQ=WEEKLY;BYDAY=(SU|MO|TU|WE|TH|FR|SA)$/.test(rule)) return 'WEEKLY'
  return 'CUSTOM'
}

function nextLocalDate(value: string) {
  const date = new Date(`${value}T00:00:00`)
  date.setDate(date.getDate() + 1)
  return localDateKey(date)
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
  const speech = useSpeech()
  const [status, setStatus] = useState<ApiStatus | null>(null)
  const [conversations, setConversations] = useState<Conversation[]>([])
  const [activeId, setActiveId] = useState<string | null>(() => localStorage.getItem('norn.activeConversation'))
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [voiceState, setVoiceState] = useState<VoiceState>('idle')
  const [recordingSeconds, setRecordingSeconds] = useState(0)
  const [isSending, setIsSending] = useState(false)
  const [error, setError] = useState('')
  const [showSettings, setShowSettings] = useState(
    () => new URLSearchParams(window.location.search).get('googleCalendar') === 'connected',
  )
  const [view, setView] = useState<AppView>('chat')
  const [calendarMode, setCalendarMode] = useState<CalendarMode>('week')
  const [selectedDate, setSelectedDate] = useState(() => localDateKey(new Date()))
  const [events, setEvents] = useState<EventItem[]>([])
  const [taskAllocations, setTaskAllocations] = useState<TaskAllocation[]>([])
  const [eventError, setEventError] = useState('')
  const [showEventEditor, setShowEventEditor] = useState(false)
  const [editingEventId, setEditingEventId] = useState<string | null>(null)
  const [eventDraft, setEventDraft] = useState<EventDraft>(() => createEventDraft(localDateKey(new Date())))
  const [calendarRefresh, setCalendarRefresh] = useState(0)
  const [googleCalendar, setGoogleCalendar] = useState<GoogleCalendarStatus | null>(null)
  const [googleCalendars, setGoogleCalendars] = useState<GoogleCalendarChoice[]>([])
  const [googleCalendarError, setGoogleCalendarError] = useState('')
  const [googleCalendarBusy, setGoogleCalendarBusy] = useState(false)
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
    if (new URLSearchParams(window.location.search).get('googleCalendar') !== 'connected') return
    window.history.replaceState({}, '', window.location.pathname)
  }, [])

  useEffect(() => {
    let active = true
    const refresh = () => fetch('/api/google-calendar/status')
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response))
        return response.json() as Promise<GoogleCalendarStatus>
      })
      .then((value) => { if (active) setGoogleCalendar(value) })
      .catch((reason) => { if (active) setGoogleCalendarError(reason instanceof Error ? reason.message : 'Google Calendar状態を取得できません。') })
    void refresh()
    const timer = window.setInterval(() => { void refresh() }, 30000)
    return () => { active = false; window.clearInterval(timer) }
  }, [calendarRefresh])

  useEffect(() => {
    if (!showSettings || !googleCalendar?.connected) return
    let active = true
    fetch('/api/google-calendar/calendars')
      .then(async (response) => {
        if (!response.ok) throw new Error(await responseError(response))
        return response.json() as Promise<{ selected_calendar_id: string; calendars: GoogleCalendarChoice[] }>
      })
      .then((value) => { if (active) setGoogleCalendars(value.calendars) })
      .catch((reason) => { if (active) setGoogleCalendarError(reason instanceof Error ? reason.message : 'カレンダー一覧を取得できません。') })
    return () => { active = false }
  }, [showSettings, googleCalendar?.connected, calendarRefresh])

  useEffect(() => {
    if (view !== 'schedule') return
    const range = getDateRange(selectedDate, calendarMode)
    const params = new URLSearchParams({ start: range.start.toISOString(), end: range.end.toISOString() })
    let active = true
    Promise.all([fetch(`/api/events?${params}`), fetch(`/api/task-allocations?${params}`)])
      .then(async ([eventResponse, taskResponse]) => {
        if (!eventResponse.ok) throw new Error(await responseError(eventResponse))
        if (!taskResponse.ok) throw new Error(await responseError(taskResponse))
        return Promise.all([
          eventResponse.json() as Promise<EventItem[]>,
          taskResponse.json() as Promise<TaskAllocation[]>,
        ])
      })
      .then(([eventItems, allocations]) => {
        if (!active) return
        setEvents(eventItems)
        setTaskAllocations(allocations)
        setEventError('')
      })
      .catch((reason) => {
        if (active) setEventError(reason instanceof Error ? reason.message : '予定を取得できませんでした。')
      })
    return () => { active = false }
  }, [view, selectedDate, calendarMode, calendarRefresh])

  useEffect(() => {
    if (view !== 'schedule') return
    const timer = window.setInterval(() => setCalendarRefresh((current) => current + 1), 300000)
    return () => window.clearInterval(timer)
  }, [view])

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

  async function planProjectTasks(project: {
    id: string
    title: string
    description: string
    next_action: string
    goal_title: string | null
  }) {
    if (!status?.llm.configured) throw new Error('LLMが未設定です。設定後に会話からタスクを依頼してください。')
    if (isSending) throw new Error('別の会話処理が進行中です。')
    speech.stop()
    setError('')
    setIsSending(true)
    const content = [
      `新しく作成したプロジェクト「${project.title}」の初期タスクを考えて登録してください。`,
      `project_id: ${project.id}`,
      project.goal_title ? `関連する目標: ${project.goal_title}` : '',
      project.description ? `説明: ${project.description}` : '',
      project.next_action ? `次のアクション: ${project.next_action}` : '',
      'このproject_idで既存タスクを確認し、重複しない具体的な初期作業を3〜5件作成してください。期限・見積時間・優先度は指定されていないので設定しないでください。',
    ].filter(Boolean).join('\n')
    try {
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: activeId, content }),
      })
      if (!response.ok) throw new Error(await responseError(response))
      const result = await response.json() as {
        conversation_id: string
        user_message: Message
        assistant_message: Message
      }
      localStorage.setItem('norn.activeConversation', result.conversation_id)
      setActiveId(result.conversation_id)
      setMessages((current) => [...current, result.user_message, result.assistant_message])
      void speech.speak(result.assistant_message.content)
      await refreshConversations()
      return result.assistant_message.content
    } finally {
      setIsSending(false)
    }
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
    speech.stop()
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
    speech.stop()
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
      void speech.speak(result.assistant_message.content)
      await refreshConversations()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '応答を取得できませんでした。')
    } finally {
      setIsSending(false)
    }
  }

  function startNewConversation() {
    speech.stop()
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
    const startAt = event.series_start_at ?? event.start_at
    const endAt = event.series_end_at ?? event.end_at
    const isAllDay = event.series_is_all_day ?? event.is_all_day
    const recurrence = event.series_recurrence ?? event.recurrence
    const endDate = localDateKey(new Date(new Date(endAt).getTime() - (isAllDay ? 86400000 : 0)))
    setEditingEventId(event.series_id ?? event.id)
    setEventDraft({
      title: event.series_title ?? event.title,
      description: event.series_description ?? event.description,
      date: localDateKey(new Date(startAt)),
      endDate,
      startTime: timeInputValue(startAt),
      endTime: timeInputValue(endAt),
      isAllDay,
      recurrence,
      repeat: recurrenceChoice(recurrence),
    })
    setEventError('')
    setShowEventEditor(true)
  }

  async function saveEvent(formEvent: FormEvent<HTMLFormElement>) {
    formEvent.preventDefault()
    setEventError('')
    if (eventDraft.isAllDay && eventDraft.endDate < eventDraft.date) {
      setEventError('終了日は開始日以降を指定してください。')
      return
    }
    const repeat = eventDraft.repeat
    const weeklyDay = ['SU', 'MO', 'TU', 'WE', 'TH', 'FR', 'SA'][new Date(`${eventDraft.date}T00:00:00`).getDay()]
    const recurrence = repeat === 'CUSTOM'
      ? eventDraft.recurrence
      : repeat === 'NONE' ? [] : [`RRULE:FREQ=${repeat}${repeat === 'WEEKLY' ? `;BYDAY=${weeklyDay}` : ''}`]
    const endDate = eventDraft.isAllDay ? nextLocalDate(eventDraft.endDate) : eventDraft.date
    const response = await fetch(editingEventId ? `/api/events/${editingEventId}` : '/api/events', {
      method: editingEventId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: eventDraft.title,
        description: eventDraft.description,
        start_at: `${eventDraft.date}T${eventDraft.isAllDay ? '00:00' : eventDraft.startTime}:00+09:00`,
        end_at: `${endDate}T${eventDraft.isAllDay ? '00:00' : eventDraft.endTime}:00+09:00`,
        is_all_day: eventDraft.isAllDay,
        recurrence,
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

    setCalendarRefresh((current) => current + 1)
    setShowEventEditor(false)
  }

  async function deleteEvent(event: EventItem) {
    const eventId = event.series_id ?? event.id
    if (event.series_id && !window.confirm('この予定の繰り返しシリーズ全体を削除しますか？')) return
    const response = await fetch(`/api/events/${eventId}`, { method: 'DELETE' }).catch(() => null)
    if (!response) {
      setEventError('バックエンドに接続できません。')
      return
    }
    if (!response.ok) {
      setEventError(await responseError(response))
      return
    }
    setCalendarRefresh((current) => current + 1)
  }

  async function connectGoogleCalendar() {
    setGoogleCalendarBusy(true)
    setGoogleCalendarError('')
    try {
      const response = await fetch('/api/google-calendar/authorize', { method: 'POST' })
      if (!response.ok) throw new Error(await responseError(response))
      const result = await response.json() as { authorization_url: string }
      window.location.assign(result.authorization_url)
    } catch (reason) {
      setGoogleCalendarError(reason instanceof Error ? reason.message : 'Google認証を開始できません。')
      setGoogleCalendarBusy(false)
    }
  }

  async function syncGoogleCalendar() {
    setGoogleCalendarBusy(true)
    setGoogleCalendarError('')
    try {
      const response = await fetch('/api/google-calendar/sync', { method: 'POST' })
      if (!response.ok) throw new Error(await responseError(response))
      setCalendarRefresh((current) => current + 1)
    } catch (reason) {
      setGoogleCalendarError(reason instanceof Error ? reason.message : '同期に失敗しました。')
    } finally {
      setGoogleCalendarBusy(false)
    }
  }

  async function selectGoogleCalendar(calendarId: string) {
    setGoogleCalendarBusy(true)
    setGoogleCalendarError('')
    try {
      const response = await fetch('/api/google-calendar/calendar', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ calendar_id: calendarId }),
      })
      if (!response.ok) throw new Error(await responseError(response))
      setCalendarRefresh((current) => current + 1)
    } catch (reason) {
      setGoogleCalendarError(reason instanceof Error ? reason.message : '同期先カレンダーを変更できません。')
    } finally {
      setGoogleCalendarBusy(false)
    }
  }

  async function resolveGoogleConflict(eventId: string, choice: 'local' | 'google') {
    setGoogleCalendarBusy(true)
    setGoogleCalendarError('')
    try {
      const response = await fetch(`/api/google-calendar/conflicts/${eventId}/resolve`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ choice }),
      })
      if (!response.ok) throw new Error(await responseError(response))
      setCalendarRefresh((current) => current + 1)
    } catch (reason) {
      setGoogleCalendarError(reason instanceof Error ? reason.message : '競合を解決できません。')
    } finally {
      setGoogleCalendarBusy(false)
    }
  }

  async function unscheduleTask(task: TaskAllocation) {
    const response = await fetch(`/api/tasks/${task.task_id}/schedule`, { method: 'DELETE' }).catch(() => null)
    if (!response) {
      setEventError('バックエンドに接続できません。')
      return
    }
    if (!response.ok) {
      setEventError(await responseError(response))
      return
    }
    setTaskAllocations((current) => current.filter((allocation) => allocation.task_id !== task.task_id))
  }

  function requestTaskSchedule(task: { title: string; estimated_minutes: number | null; due_at: string | null }) {
    const estimate = task.estimated_minutes ? `見積時間は${task.estimated_minutes}分` : '見積時間は未設定'
    const due = task.due_at ? `期限は${formatDate(task.due_at)}` : '期限は未設定'
    setDraft(`「${task.title}」の作業時間を予定と重ならない空き枠に割り当ててください。${estimate}、${due}です。`)
    setView('chat')
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
              onClick={() => { speech.stop(); setActiveId(conversation.id) }}
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
              <Settings2 size={16} /><span>設定</span>{!!googleCalendar?.conflicts.length && <span className="settings-conflict-count">{googleCalendar.conflicts.length}</span>}<ChevronRight size={15} />
          </button>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div><div className="eyebrow">NORN / PERSONAL SYSTEM</div><h1>{view === 'chat' ? '会話' : view === 'schedule' ? '予定' : view === 'tasks' ? 'タスク' : '目標'}</h1></div>
          <div className="topbar-actions">
            <nav className="main-view-switch" aria-label="表示切替">
              <button className={view === 'chat' ? 'is-active' : ''} type="button" onClick={() => setView('chat')}><AudioLines size={15} />会話</button>
              <button className={view === 'schedule' ? 'is-active' : ''} type="button" onClick={() => setView('schedule')}><CalendarDays size={15} />予定</button>
              <button className={view === 'tasks' ? 'is-active' : ''} type="button" onClick={() => setView('tasks')}><ClipboardList size={15} />タスク</button>
              <button className={view === 'goals' ? 'is-active' : ''} type="button" onClick={() => setView('goals')}><Target size={15} />目標</button>
            </nav>
            <div className="topbar-state"><span className={`llm-dot ${status?.llm.configured ? 'is-ready' : ''}`} />
              <span>{status?.llm.configured ? 'AI接続済み' : 'AI未設定'}</span>
            </div>
            <button className={`icon-button topbar-settings ${googleCalendar?.conflicts.length ? 'has-conflict' : ''}`} type="button" onClick={() => setShowSettings(true)} aria-label={googleCalendar?.conflicts.length ? '同期競合あり、設定' : '設定'} title={googleCalendar?.conflicts.length ? '同期競合があります' : '設定'}><Settings2 size={16} />{!!googleCalendar?.conflicts.length && <span className="settings-alert-dot" />}</button>
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
        </section> : view === 'schedule' ? <section className="calendar-stage" aria-label="予定管理">
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
              const dayEvents = events.filter((event) => {
                const startKey = localDateKey(new Date(event.start_at))
                if (!event.is_all_day) return startKey === dayKey
                const exclusiveEndKey = localDateKey(new Date(new Date(event.end_at).getTime() - 1))
                return startKey <= dayKey && exclusiveEndKey >= dayKey
              })
              const dayTaskAllocations = taskAllocations.filter((task) => localDateKey(new Date(task.start_at)) === dayKey)
              return <section className="calendar-day" key={dayKey} aria-label={formatLongDate(day)}>
                <header className="calendar-day-heading">
                  <div><span className="calendar-day-name">{new Intl.DateTimeFormat('ja-JP', { weekday: 'long' }).format(day)}</span><strong>{new Intl.DateTimeFormat('ja-JP', { month: 'numeric', day: 'numeric' }).format(day)}</strong></div>
                  <span className="event-count">{dayEvents.length}予定 / {dayTaskAllocations.length}タスク</span>
                </header>
                {dayEvents.length === 0 && dayTaskAllocations.length === 0 ? <p className="day-empty">予定なし</p> : <div className="event-list">
                  {dayEvents.map((event) => <article className="event-row" key={event.occurrence_id ?? event.id}>
                    <div className="event-time"><strong>{event.is_all_day ? '終日' : formatEventTime(event.start_at)}</strong>{!event.is_all_day && <span>{formatEventTime(event.end_at)}</span>}</div>
                    <div className="event-color-mark" />
                    <div className="event-content"><h3>{event.title}</h3>{event.recurrence.length > 0 && <span className="event-repeat-label">繰り返し</span>}{event.description && <p>{event.description}</p>}</div>
                    <div className="event-controls">
                      <button className="calendar-icon-button" type="button" onClick={() => openEditEvent(event)} aria-label={`${event.title}を編集`} title="編集"><Pencil size={15} /></button>
                      <button className="calendar-icon-button danger" type="button" onClick={() => void deleteEvent(event)} aria-label={`${event.title}を削除`} title="削除"><Trash2 size={15} /></button>
                    </div>
                  </article>)}
                  {dayTaskAllocations.map((task) => <article className="event-row task-allocation-row" key={task.id}>
                    <div className="event-time"><strong>{formatEventTime(task.start_at)}</strong><span>{formatEventTime(task.end_at)}</span></div>
                    <div className="event-color-mark task-allocation-mark" />
                    <div className="event-content"><span className="task-slot-label">タスク枠</span><h3>{task.title}</h3><p>見積 {task.estimated_minutes}分</p></div>
                    <div className="event-controls"><button className="calendar-icon-button danger" type="button" onClick={() => void unscheduleTask(task)} aria-label={`${task.title}の時間枠を解除`} title="時間枠を解除"><Trash2 size={15} /></button></div>
                  </article>)}
                </div>}
              </section>
            })}
          </div>
          {events.length === 0 && taskAllocations.length === 0 && !eventError && <div className="calendar-empty-note"><CalendarDays size={18} /><span>この期間に予定はありません</span></div>}
        </section> : view === 'tasks' ? <TasksView onRequestSchedule={requestTaskSchedule} /> : (
          <GoalsView onPlanProject={planProjectTasks} />
        )}

        <section className="composer-wrap" aria-label="音声入力">
          <div className="speech-controls" aria-label="応答音声">
            <label><input type="checkbox" checked={speech.enabled} onChange={speech.toggle} />応答を読み上げる</label>
            <span role="status">{speech.state === 'loading' ? '音声モデル準備中' : speech.state === 'generating' ? '音声生成中' : speech.state === 'playing' ? '再生中' : ''}</span>
            <button className="icon-button" type="button" title="音声を再生" aria-label="音声を再生" disabled={!speech.hasAudio || speech.state === 'playing'} onClick={() => void speech.play()}><Play size={16} /></button>
            <button className="icon-button" type="button" title="音声を停止" aria-label="音声を停止" disabled={!['loading', 'generating', 'playing'].includes(speech.state)} onClick={speech.stop}><Square size={16} /></button>
          </div>
          {speech.error && <div className="error-banner" role="alert"><CircleAlert size={16} /><span>{speech.error}</span></div>}
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
        <section className="settings-dialog calendar-settings" role="dialog" aria-modal="true" aria-labelledby="settings-title">
          <div className="dialog-heading"><div><div className="eyebrow">SYSTEM CONFIGURATION</div><h2 id="settings-title">接続設定</h2></div>
            <button className="icon-button" type="button" onClick={() => setShowSettings(false)} aria-label="閉じる">×</button>
          </div>
          <div className="setting-row"><span>音声認識</span><strong>ローカル / {status?.stt.model ?? '未接続'}</strong></div>
          <div className="setting-row"><span>LLM</span><strong>{status?.llm.configured ? status.llm.model : '未設定'}</strong></div>
          <p className="settings-help">LLMの接続先とAPIキーは、NORN直下の <code>.env</code> で設定します。キーはブラウザーへ送られません。</p>
          <div className="calendar-settings-heading"><CalendarDays size={16} /><strong>Google Calendar</strong></div>
          <div className="setting-row"><span>接続状態</span><strong>{googleCalendar?.connected ? '接続済み' : googleCalendar?.configured ? '未接続' : '未設定'}</strong></div>
          <div className="setting-row"><span>自動同期</span><strong>{googleCalendar ? `${googleCalendar.sync_interval_seconds / 60}分ごと` : '確認中'}</strong></div>
          {googleCalendar?.last_sync_at && <p className="calendar-sync-time">最終同期 {formatDate(googleCalendar.last_sync_at)}</p>}
          {googleCalendar?.last_error && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{googleCalendar.last_error}</span></div>}
          {googleCalendarError && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{googleCalendarError}</span></div>}
          {googleCalendar?.connected && googleCalendars.length > 0 && <>
            <label className="event-field calendar-picker">同期するカレンダー
              <select value={googleCalendar.calendar_id} disabled={googleCalendarBusy} onChange={(event) => void selectGoogleCalendar(event.target.value)}>
                {googleCalendars.map((calendar) => <option key={calendar.id} value={calendar.id}>{calendar.name}{calendar.primary ? '（メイン）' : ''}</option>)}
              </select>
            </label>
            <p className="settings-help">選択した1つのカレンダーを双方向同期します。別のカレンダーへ切り替えても、以前同期した予定はNORNに残り、そのカレンダーを再選択すると同期を再開します。</p>
          </>}
          {!googleCalendar?.configured && <p className="settings-help">NORNの <code>.env</code> にGoogle OAuth client ID/secretとredirect URIを設定してください。tokenはGit管理外のdataフォルダーに保存されます。</p>}
          <div className="calendar-connect-actions">
            {!googleCalendar?.connected
              ? <button className="dialog-close" type="button" onClick={() => void connectGoogleCalendar()} disabled={!googleCalendar?.configured || googleCalendarBusy}>{googleCalendarBusy ? '接続中…' : 'Googleに接続'}</button>
              : <>
                <button className="dialog-close" type="button" onClick={() => void syncGoogleCalendar()} disabled={googleCalendarBusy}>{googleCalendarBusy ? '同期中…' : '今すぐ同期'}</button>
                <button className="calendar-reconnect-button" type="button" onClick={() => void connectGoogleCalendar()} disabled={googleCalendarBusy}>Googleの権限を更新 / 再接続</button>
              </>}
          </div>
          {!!googleCalendar?.conflicts.length && <section className="calendar-conflicts" aria-label="同期の競合">
            <h3>同期を保留中の予定 ({googleCalendar.conflicts.length})</h3>
            <p>両側で変更された予定です。どちらを残すか選ぶまで自動同期を停止しています。</p>
            {googleCalendar.conflicts.map((conflict) => <article className="calendar-conflict" key={conflict.event_id}>
              <strong>{conflict.title}</strong>
              <div className="conflict-values">
                <div><span>NORN</span><p>{conflict.local.deleted ? 'NORN側で削除' : <>{conflict.local.title}<br />{formatDate(conflict.local.start_at)} - {formatDate(conflict.local.end_at)}<br />{conflict.local.description}</>}</p></div>
                <div><span>Google</span><p>{conflict.remote.deleted ? 'Google側で削除' : <>{conflict.remote.title}<br />{conflict.remote.start_at ? formatDate(conflict.remote.start_at) : ''}{conflict.remote.end_at ? ` - ${formatDate(conflict.remote.end_at)}` : ''}<br />{conflict.remote.description}</>}</p></div>
              </div>
              <div className="conflict-actions">
                <button type="button" onClick={() => void resolveGoogleConflict(conflict.event_id, 'local')} disabled={googleCalendarBusy}>NORNを採用</button>
                <button type="button" onClick={() => void resolveGoogleConflict(conflict.event_id, 'google')} disabled={googleCalendarBusy}>Googleを採用</button>
              </div>
            </article>)}
          </section>}
          <button className="dialog-close" type="button" onClick={() => setShowSettings(false)}>閉じる</button>
        </section>
      </div>}

      {showEventEditor && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget) setShowEventEditor(false)
      }}>
        <form className="settings-dialog event-dialog" role="dialog" aria-modal="true" aria-labelledby="event-dialog-title" onSubmit={saveEvent}>
          <div className="dialog-heading"><div><div className="eyebrow">SCHEDULE / EVENT</div><h2 id="event-dialog-title">{editingEventId ? eventDraft.recurrence.length ? '繰り返しを編集' : '予定を編集' : '予定を追加'}</h2></div>
            <button className="icon-button" type="button" onClick={() => setShowEventEditor(false)} aria-label="閉じる">×</button>
          </div>
          {editingEventId && eventDraft.recurrence.length > 0 && <p className="event-series-note">変更は繰り返しシリーズ全体に適用されます。</p>}
          {eventError && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{eventError}</span></div>}
          <label className="event-field">予定名<input autoFocus required maxLength={120} value={eventDraft.title} onChange={(event) => setEventDraft({ ...eventDraft, title: event.target.value })} placeholder="例: 定例ミーティング" /></label>
          <label className="event-field">日付<input required type="date" value={eventDraft.date} onChange={(event) => setEventDraft({ ...eventDraft, date: event.target.value, endDate: eventDraft.isAllDay ? event.target.value : eventDraft.endDate })} /></label>
          <label className="event-toggle"><input type="checkbox" checked={eventDraft.isAllDay} onChange={(event) => setEventDraft({ ...eventDraft, isAllDay: event.target.checked, endDate: eventDraft.date })} />終日</label>
          {eventDraft.isAllDay
            ? <label className="event-field">終了日（含む）<input required type="date" min={eventDraft.date} value={eventDraft.endDate} onChange={(event) => setEventDraft({ ...eventDraft, endDate: event.target.value })} /></label>
            : <div className="event-time-fields">
              <label className="event-field">開始<input required type="time" value={eventDraft.startTime} onChange={(event) => setEventDraft({ ...eventDraft, startTime: event.target.value })} /></label>
              <label className="event-field">終了<input required type="time" value={eventDraft.endTime} onChange={(event) => setEventDraft({ ...eventDraft, endTime: event.target.value })} /></label>
            </div>}
          <label className="event-field">繰り返し<select value={eventDraft.repeat} onChange={(event) => setEventDraft({ ...eventDraft, repeat: event.target.value })}>
            <option value="NONE">なし</option><option value="DAILY">毎日</option><option value="WEEKLY">毎週</option><option value="MONTHLY">毎月</option>
            {eventDraft.repeat === 'CUSTOM' && <option value="CUSTOM">Googleの設定を維持</option>}
          </select></label>
          <label className="event-field">メモ<textarea maxLength={2000} rows={3} value={eventDraft.description} onChange={(event) => setEventDraft({ ...eventDraft, description: event.target.value })} placeholder="場所や準備事項" /></label>
          <div className="event-dialog-actions"><button className="cancel-button" type="button" onClick={() => setShowEventEditor(false)}>キャンセル</button><button className="dialog-close" type="submit">{editingEventId ? '変更を保存' : '予定を保存'}</button></div>
        </form>
      </div>}
    </div>
  )
}