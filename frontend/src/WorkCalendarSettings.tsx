import { useEffect, useState, type FormEvent } from 'react'

type ExceptionKind = 'paid_leave' | 'compensatory_leave' | 'other_day_off' | 'workday'
type ExceptionItem = {
  day: string
  group_id: string
  kind: ExceptionKind
  label: string
  start_time: string | null
  end_time: string | null
}
type CalendarConfig = {
  work_start: string
  work_end: string
  workdays: number[]
  holiday_note: string
  exceptions: ExceptionItem[]
}
type Draft = {
  start_date: string
  end_date: string
  kind: ExceptionKind
  label: string
  start_time: string
  end_time: string
}

const kindLabels: Record<ExceptionKind, string> = {
  paid_leave: '有給休暇',
  compensatory_leave: '代休',
  other_day_off: 'その他の休日',
  workday: '臨時勤務日',
}
const weekdays = ['月', '火', '水', '木', '金', '土', '日']

function todayString() {
  const now = new Date()
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
}

async function messageFor(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string' ? result.detail : `リクエストに失敗しました (${response.status})`
}

export default function WorkCalendarSettings() {
  const [calendar, setCalendar] = useState<CalendarConfig | null>(null)
  const [draft, setDraft] = useState<Draft>({
    start_date: todayString(),
    end_date: todayString(),
    kind: 'paid_leave',
    label: '有給休暇',
    start_time: '',
    end_time: '',
  })
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)

  async function load() {
    const response = await fetch('/api/work-calendar')
    if (!response.ok) throw new Error(await messageFor(response))
    setCalendar(await response.json() as CalendarConfig)
  }

  useEffect(() => {
    let active = true
    fetch('/api/work-calendar')
      .then(async (response) => {
        if (!response.ok) throw new Error(await messageFor(response))
        return await response.json() as CalendarConfig
      })
      .then((result) => { if (active) setCalendar(result) })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : '勤務日設定を取得できませんでした。') })
    return () => { active = false }
  }, [])

  async function save(event: FormEvent) {
    event.preventDefault()
    if (draft.kind === 'workday' && Boolean(draft.start_time) !== Boolean(draft.end_time)) {
      setError('勤務時間を上書きする場合は開始・終了の両方を入力してください。')
      return
    }
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const payload = {
        start_date: draft.start_date,
        end_date: draft.end_date,
        kind: draft.kind,
        label: draft.label,
        ...(draft.kind === 'workday' && draft.start_time && draft.end_time
          ? { start_time: draft.start_time, end_time: draft.end_time }
          : {}),
      }
      const response = await fetch('/api/work-calendar/exceptions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await messageFor(response))
      await load()
      setNotice('勤務日例外を登録しました。既存の予定は移動していません。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '勤務日例外を登録できませんでした。')
    } finally {
      setBusy(false)
    }
  }

  async function remove(groupId: string, label: string) {
    if (!window.confirm(`「${label}」の勤務日例外を削除しますか？`)) return
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const response = await fetch(`/api/work-calendar/exceptions/${groupId}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await messageFor(response))
      await load()
      setNotice('勤務日例外を削除しました。')
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '勤務日例外を削除できませんでした。')
    } finally {
      setBusy(false)
    }
  }

  const groups = new Map<string, ExceptionItem[]>()
  for (const item of calendar?.exceptions ?? []) {
    groups.set(item.group_id, [...(groups.get(item.group_id) ?? []), item])
  }

  return <section className="work-calendar-settings" aria-label="勤務日設定">
    <div className="calendar-settings-heading"><strong>勤務時間・休日</strong></div>
    {calendar && <>
      <div className="setting-row"><span>基本勤務時間</span><strong>{calendar.work_start}〜{calendar.work_end}</strong></div>
      <div className="setting-row"><span>基本勤務曜日</span><strong>{calendar.workdays.map((day) => weekdays[day]).join('・')}</strong></div>
      <p className="settings-help">土日と日本の祝日は標準で休みです。{calendar.holiday_note} 年末年始・お盆や個人の有給・代休は例外登録してください。タスクとルーティンの配置だけ勤務時間内に制限し、通常予定は時間外にも登録できます。</p>
    </>}
    {error && <div className="error-banner" role="alert">{error}</div>}
    {notice && <p className="goal-notice" role="status">{notice}</p>}
    <form className="routine-form" onSubmit={(event) => void save(event)}>
      <div className="task-form-grid">
        <label className="task-field">開始日<input type="date" required value={draft.start_date} onChange={(event) => setDraft({ ...draft, start_date: event.target.value })} /></label>
        <label className="task-field">終了日<input type="date" required min={draft.start_date} value={draft.end_date} onChange={(event) => setDraft({ ...draft, end_date: event.target.value })} /></label>
        <label className="task-field">例外<select value={draft.kind} onChange={(event) => {
          const kind = event.target.value as ExceptionKind
          setDraft({ ...draft, kind, label: kindLabels[kind] })
        }}>
          {Object.entries(kindLabels).map(([kind, label]) => <option key={kind} value={kind}>{label}</option>)}
        </select></label>
        <label className="task-field">メモ<input maxLength={120} value={draft.label} onChange={(event) => setDraft({ ...draft, label: event.target.value })} /></label>
        {draft.kind === 'workday' && <>
          <label className="task-field">勤務開始<input type="time" value={draft.start_time} onChange={(event) => setDraft({ ...draft, start_time: event.target.value })} /></label>
          <label className="task-field">勤務終了<input type="time" value={draft.end_time} onChange={(event) => setDraft({ ...draft, end_time: event.target.value })} /></label>
        </>}
      </div>
      <button className="add-event-button" type="submit" disabled={busy}>例外を登録</button>
    </form>
    <div className="work-calendar-exceptions">
      <strong>登録済みの例外</strong>
      {[...groups.entries()].map(([groupId, days]) => {
        const first = days[0]
        const last = days.at(-1)!
        return <div className="setting-row" key={groupId}>
          <span>{first.day === last.day ? first.day : `${first.day}〜${last.day}`} ・{kindLabels[first.kind]}{first.start_time ? ` ${first.start_time}〜${first.end_time}` : ''}{first.label && first.label !== kindLabels[first.kind] ? `（${first.label}）` : ''}</span>
          <button className="calendar-icon-button danger" type="button" disabled={busy} onClick={() => void remove(groupId, first.label)} aria-label={`${first.label}の例外を削除`}>×</button>
        </div>
      })}
      {calendar && groups.size === 0 && <p className="settings-help">登録された例外はありません。</p>}
    </div>
  </section>
}
