import { useEffect, useState, type FormEvent } from 'react'
import { CircleAlert, ClipboardCheck, Play, Square } from 'lucide-react'

type ExecutionStatus = 'completed' | 'partial' | 'skipped' | 'cancelled'
type TaskOption = { id: string; title: string; status: string }
type OccurrenceOption = { id: string; routine_id: string; scheduled_start: string; status: string }
type RoutineOption = { id: string; title: string }
type ActiveSession = {
  id: string
  title: string | null
  started_at: string
  resume_point: string
}
type SummaryRow = {
  task_id?: string
  routine_id?: string
  title: string | null
  estimated_minutes?: number | null
  planned_minutes: number
  actual_minutes: number
  completed: number
  partial: number
  skipped: number
  cancelled: number
  resume_point: string
}
type Summary = {
  start: string
  end: string
  totals: SummaryRow & { achievement_rate: number | null; time_difference_minutes: number }
  tasks: SummaryRow[]
  routines: SummaryRow[]
}

const statusLabels: Record<ExecutionStatus, string> = {
  completed: '完了', partial: '一部実施', skipped: '未実施', cancelled: '中止',
}

async function errorMessage(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string'
    ? result.detail
    : `リクエストに失敗しました (${response.status})`
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) throw new Error(await errorMessage(response))
  return response.status === 204 ? (null as T) : (response.json() as Promise<T>)
}

function postJson(body: unknown): RequestInit {
  return { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }
}

function elapsedMinutes(startedAt: string, now: number) {
  return Math.max(0, Math.floor((now - new Date(startedAt).getTime()) / 60000))
}

function slotLabel(start: string) {
  return new Intl.DateTimeFormat('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(start))
}

export default function ActualsView() {
  const [active, setActive] = useState<ActiveSession | null>(null)
  const [tasks, setTasks] = useState<TaskOption[]>([])
  const [occurrences, setOccurrences] = useState<OccurrenceOption[]>([])
  const [routines, setRoutines] = useState<RoutineOption[]>([])
  const [summary, setSummary] = useState<Summary | null>(null)
  const [target, setTarget] = useState('')
  const [status, setStatus] = useState<ExecutionStatus>('completed')
  const [note, setNote] = useState('')
  const [resumePoint, setResumePoint] = useState('')
  const [interruption, setInterruption] = useState('')
  const [progress, setProgress] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [isBusy, setIsBusy] = useState(false)
  const [now, setNow] = useState(() => Date.now())

  async function load() {
    const [session, taskItems, occurrenceItems, routineItems, summaryItem] = await Promise.all([
      fetchJson<ActiveSession | null>('/api/work-sessions/active'),
      fetchJson<TaskOption[]>('/api/tasks'),
      fetchJson<OccurrenceOption[]>('/api/routine-occurrences'),
      fetchJson<RoutineOption[]>('/api/routines?active=true'),
      fetchJson<Summary>('/api/execution-summary'),
    ])
    setActive(session)
    setTasks(taskItems.filter((task) => task.status === 'open'))
    setOccurrences(occurrenceItems.filter((item) => item.status === 'scheduled'))
    setRoutines(routineItems)
    setSummary(summaryItem)
  }

  useEffect(() => {
    let alive = true
    Promise.all([
      fetchJson<ActiveSession | null>('/api/work-sessions/active'),
      fetchJson<TaskOption[]>('/api/tasks'),
      fetchJson<OccurrenceOption[]>('/api/routine-occurrences'),
      fetchJson<RoutineOption[]>('/api/routines?active=true'),
      fetchJson<Summary>('/api/execution-summary'),
    ])
      .then(([session, taskItems, occurrenceItems, routineItems, summaryItem]) => {
        if (!alive) return
        setActive(session)
        setTasks(taskItems.filter((task) => task.status === 'open'))
        setOccurrences(occurrenceItems.filter((item) => item.status === 'scheduled'))
        setRoutines(routineItems)
        setSummary(summaryItem)
      })
      .catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : '実績を取得できませんでした。') })
      .finally(() => { if (alive) setIsLoading(false) })
    const timer = window.setInterval(() => setNow(Date.now()), 30000)
    return () => { alive = false; window.clearInterval(timer) }
  }, [])

  async function act(action: () => Promise<string>) {
    setIsBusy(true)
    setError('')
    setNotice('')
    try {
      setNotice(await action())
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作に失敗しました。')
    } finally {
      setIsBusy(false)
    }
  }

  function start(event: FormEvent) {
    event.preventDefault()
    const [kind, id] = target.split(':')
    if (!id) return
    void act(async () => {
      await fetchJson('/api/work-sessions/start', postJson({ [kind]: id }))
      setTarget('')
      return '作業を開始しました。'
    })
  }

  function finish(event: FormEvent) {
    event.preventDefault()
    if (!active) return
    void act(async () => {
      await fetchJson(`/api/work-sessions/${active.id}/finish`, postJson({
        status,
        note,
        resume_point: resumePoint,
        interruption_reason: interruption,
        progress_percent: progress === '' ? null : Number(progress),
      }))
      setStatus('completed')
      setNote('')
      setResumePoint('')
      setInterruption('')
      setProgress('')
      return '実績を記録しました。'
    })
  }

  function discard() {
    if (!active) return
    void act(async () => {
      await fetchJson(`/api/work-sessions/${active.id}`, { method: 'DELETE' })
      return '作業を破棄しました（実績は記録していません）。'
    })
  }

  const routineTitle = (id: string) => routines.find((routine) => routine.id === id)?.title ?? 'ルーティン'
  const rows = summary ? [...summary.tasks, ...summary.routines] : []

  return (
    <section className="goal-stage" aria-label="実績">
      <header className="goal-toolbar">
        <div>
          <div className="eyebrow">ACTUALS / EXECUTION</div>
          <h2>実績</h2>
          <p>作業の開始と終了を記録し、予定との差や再開地点を見ます。</p>
        </div>
      </header>

      {error && <div className="error-banner goal-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button></div>}
      {notice && <div className="goal-notice" role="status" aria-live="polite">{notice}</div>}

      {isLoading ? <div className="goal-empty-state">読み込み中…</div> : <>
        {active ? <form className="routine-form" onSubmit={finish}>
          <h3>作業中: {active.title ?? '作業'}（{elapsedMinutes(active.started_at, now)}分経過）</h3>
          {active.resume_point && <p className="goal-description">前回の再開地点: {active.resume_point}</p>}
          <div className="task-form-grid">
            <label className="task-field">結果<select value={status} onChange={(event) => setStatus(event.target.value as ExecutionStatus)}>
              {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
            </select></label>
            <label className="task-field">進捗(%)<input type="number" min={0} max={100} value={progress} onChange={(event) => setProgress(event.target.value)} /></label>
          </div>
          <label className="task-field">次回の再開地点<input maxLength={2000} value={resumePoint} onChange={(event) => setResumePoint(event.target.value)} placeholder="例: Bメロの歌詞から" /></label>
          {status !== 'completed' && <label className="task-field">中断・未実施の理由<input maxLength={2000} value={interruption} onChange={(event) => setInterruption(event.target.value)} /></label>}
          <label className="task-field">メモ<input maxLength={4000} value={note} onChange={(event) => setNote(event.target.value)} /></label>
          <div className="goal-toolbar-actions">
            <button className="today-button" type="button" disabled={isBusy} onClick={discard}>破棄</button>
            <button className="add-event-button" type="submit" disabled={isBusy}><Square size={15} />終了して記録</button>
          </div>
        </form> : <form className="routine-form" onSubmit={start}>
          <label className="task-field">開始する作業<select value={target} onChange={(event) => setTarget(event.target.value)}>
            <option value="">選択してください</option>
            {tasks.length > 0 && <optgroup label="タスク">{tasks.map((task) => <option key={task.id} value={`task_id:${task.id}`}>{task.title}</option>)}</optgroup>}
            {occurrences.length > 0 && <optgroup label="ルーティンの予定">{occurrences.map((item) => <option key={item.id} value={`occurrence_id:${item.id}`}>{routineTitle(item.routine_id)}（{slotLabel(item.scheduled_start)}）</option>)}</optgroup>}
          </select></label>
          <div className="goal-toolbar-actions">
            <button className="add-event-button" type="submit" disabled={isBusy || !target}><Play size={15} />作業を開始</button>
          </div>
        </form>}

        <h3 className="actuals-heading">今週の集計（{summary?.start}〜{summary?.end}）</h3>
        {summary && rows.length === 0 ? <div className="goal-empty-state"><ClipboardCheck size={19} /><span>この期間の実績・予定はありません</span></div> : summary && <>
          <p className="goal-description">予定{summary.totals.planned_minutes}分 / 実績{summary.totals.actual_minutes}分（差 {summary.totals.time_difference_minutes >= 0 ? '+' : ''}{summary.totals.time_difference_minutes}分）・達成率 {summary.totals.achievement_rate === null ? '—' : `${Math.round(summary.totals.achievement_rate * 100)}%`}</p>
          <ul className="routine-occurrences">
            {rows.map((row) => <li key={row.task_id ?? row.routine_id}>
              <span>{row.title ?? '（削除済み）'}{row.estimated_minutes ? `（見積${row.estimated_minutes}分）` : ''}{row.resume_point ? ` ・再開: ${row.resume_point}` : ''}</span>
              <span className="routine-status">予定{row.planned_minutes}分 / 実績{row.actual_minutes}分 ・完了{row.completed} 一部{row.partial} 未実施{row.skipped}</span>
            </li>)}
          </ul>
        </>}
      </>}
    </section>
  )
}
