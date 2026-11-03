import { useEffect, useState, type FormEvent } from 'react'
import { CalendarClock, Check, CircleAlert, Clock3, Pencil, Plus, Trash2 } from 'lucide-react'

type TaskStatus = 'open' | 'in_progress' | 'done' | 'cancelled'
type TaskPriority = 'low' | 'normal' | 'high'
type TaskItem = {
  id: string
  project_id: string | null
  project_title: string | null
  title: string
  description: string
  due_at: string | null
  estimated_minutes: number | null
  priority: TaskPriority
  status: TaskStatus
  created_at: string
  updated_at: string
  completed_at: string | null
  scheduled_start: string | null
  scheduled_end: string | null
  remaining_minutes: number | null
  focus_level: number | null
  splittable: number | boolean
  min_session_minutes: number | null
  max_session_minutes: number | null
  depends_on: string[]
}
type Allocation = { id: string; task_id: string; start_at: string; end_at: string }
type PlanTask = {
  task_id: string
  title: string
  priority_score: number
  allocated_minutes: number
  unallocated_minutes: number
  allocations: { start_at: string; end_at: string; break_after_minutes?: number }[]
}
type WeekPlan = {
  tasks: PlanTask[]
  skipped: { task_id: string; title?: string; reason: string }[]
  days: { date: string; available_minutes: number; planned_minutes: number; ratio: number }[]
}
type TaskDraft = {
  title: string
  projectId: string
  description: string
  dueAt: string
  estimatedMinutes: string
  priority: TaskPriority
  remainingMinutes: string
  focusLevel: string
  splittable: boolean
  minSession: string
  maxSession: string
  dependsOn: string[]
}
type TaskFilter = 'active' | 'done' | 'all'
type ProjectOption = { id: string; title: string }
type TasksViewProps = {
  onRequestSchedule: (task: Pick<TaskItem, 'title' | 'estimated_minutes' | 'due_at'>) => void
  onRequestWeekPlan: () => void
}

function emptyDraft(): TaskDraft {
  return {
    title: '', projectId: '', description: '', dueAt: '', estimatedMinutes: '', priority: 'normal',
    remainingMinutes: '', focusLevel: '', splittable: true, minSession: '', maxSession: '', dependsOn: [],
  }
}

function toLocalDateTime(value: string | null) {
  if (!value) return ''
  const date = new Date(value)
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000)
  return local.toISOString().slice(0, 16)
}

function formatDue(value: string | null) {
  if (!value) return '期限なし'
  return new Intl.DateTimeFormat('ja-JP', {
    month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
  }).format(new Date(value))
}

function formatScheduledRange(start: string, end: string) {
  const date = new Intl.DateTimeFormat('ja-JP', { month: 'numeric', day: 'numeric' }).format(new Date(start))
  const startTime = new Intl.DateTimeFormat('ja-JP', { hour: '2-digit', minute: '2-digit' }).format(new Date(start))
  const endTime = new Intl.DateTimeFormat('ja-JP', { hour: '2-digit', minute: '2-digit' }).format(new Date(end))
  return `${date} ${startTime}-${endTime}`
}

async function errorMessage(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string'
    ? result.detail
    : `リクエストに失敗しました (${response.status})`
}

export default function TasksView({ onRequestSchedule, onRequestWeekPlan }: TasksViewProps) {
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [allocations, setAllocations] = useState<Allocation[]>([])
  const [plan, setPlan] = useState<WeekPlan | null>(null)
  const [isPlanLoading, setIsPlanLoading] = useState(false)
  const [projects, setProjects] = useState<ProjectOption[]>([])
  const [filter, setFilter] = useState<TaskFilter>('active')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [showEditor, setShowEditor] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState<TaskDraft>(emptyDraft)

  function allocationRange() {
    const start = new Date()
    const end = new Date(start.getTime() + 60 * 86400000)
    return `start=${encodeURIComponent(start.toISOString())}&end=${encodeURIComponent(end.toISOString())}`
  }

  async function refreshAllocations() {
    const response = await fetch(`/api/task-allocations?${allocationRange()}`)
    if (response.ok) setAllocations((await response.json()) as Allocation[])
  }

  async function loadPlan() {
    setIsPlanLoading(true)
    setError('')
    try {
      const from = new Date()
      const through = new Date(from.getTime() + 7 * 86400000)
      const response = await fetch(`/api/task-allocation-plan?from_at=${encodeURIComponent(from.toISOString())}&through_at=${encodeURIComponent(through.toISOString())}`)
      if (!response.ok) throw new Error(await errorMessage(response))
      setPlan((await response.json()) as WeekPlan)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '週間計画を取得できませんでした。')
    } finally {
      setIsPlanLoading(false)
    }
  }

  async function deleteAllocation(allocation: Allocation) {
    setError('')
    try {
      const response = await fetch(`/api/tasks/${allocation.task_id}/allocations/${allocation.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await errorMessage(response))
      await Promise.all([refreshTasks(), refreshAllocations()])
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '作業枠を削除できませんでした。')
    }
  }

  async function refreshTasks() {
    const [taskResponse, projectResponse] = await Promise.all([
      fetch('/api/tasks'),
      fetch('/api/projects'),
    ])
    if (!taskResponse.ok) throw new Error(await errorMessage(taskResponse))
    if (!projectResponse.ok) throw new Error(await errorMessage(projectResponse))
    const [taskItems, projectItems] = await Promise.all([
      taskResponse.json() as Promise<TaskItem[]>,
      projectResponse.json() as Promise<ProjectOption[]>,
    ])
    setTasks(taskItems)
    setProjects(projectItems)
  }

  useEffect(() => {
    let active = true
    Promise.all([fetch('/api/tasks'), fetch('/api/projects'), fetch(`/api/task-allocations?${allocationRange()}`)])
      .then(async ([taskResponse, projectResponse, allocationResponse]) => {
        if (!taskResponse.ok) throw new Error(await errorMessage(taskResponse))
        if (!projectResponse.ok) throw new Error(await errorMessage(projectResponse))
        return Promise.all([
          taskResponse.json() as Promise<TaskItem[]>,
          projectResponse.json() as Promise<ProjectOption[]>,
          allocationResponse.ok ? allocationResponse.json() as Promise<Allocation[]> : Promise.resolve([] as Allocation[]),
        ])
      })
      .then(([taskItems, projectItems, allocationItems]) => {
        if (!active) return
        setTasks(taskItems)
        setProjects(projectItems)
        setAllocations(allocationItems)
      })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : 'タスクを取得できませんでした。') })
      .finally(() => { if (active) setIsLoading(false) })
    return () => { active = false }
  }, [])

  function openNewTask() {
    setEditingId(null)
    setDraft(emptyDraft())
    setError('')
    setShowEditor(true)
  }

  function openEditTask(task: TaskItem) {
    setEditingId(task.id)
    setDraft({
      title: task.title,
      projectId: task.project_id ?? '',
      description: task.description,
      dueAt: toLocalDateTime(task.due_at),
      estimatedMinutes: task.estimated_minutes?.toString() ?? '',
      priority: task.priority,
      remainingMinutes: task.remaining_minutes?.toString() ?? '',
      focusLevel: task.focus_level?.toString() ?? '',
      splittable: Boolean(task.splittable),
      minSession: task.min_session_minutes?.toString() ?? '',
      maxSession: task.max_session_minutes?.toString() ?? '',
      dependsOn: task.depends_on ?? [],
    })
    setError('')
    setShowEditor(true)
  }

  async function saveTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setIsSaving(true)
    setError('')
    const optionalNumber = (value: string) => value ? Number(value) : null
    const payload = {
      title: draft.title,
      project_id: draft.projectId || null,
      description: draft.description,
      due_at: draft.dueAt ? new Date(draft.dueAt).toISOString() : null,
      estimated_minutes: optionalNumber(draft.estimatedMinutes),
      remaining_minutes: optionalNumber(draft.remainingMinutes),
      focus_level: optionalNumber(draft.focusLevel),
      splittable: draft.splittable,
      min_session_minutes: optionalNumber(draft.minSession),
      max_session_minutes: optionalNumber(draft.maxSession),
      priority: draft.priority,
    }
    try {
      const response = await fetch(editingId ? `/api/tasks/${editingId}` : '/api/tasks', {
        method: editingId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await errorMessage(response))
      const saved = (await response.json()) as TaskItem
      const dependencyResponse = await fetch(`/api/tasks/${saved.id}/dependencies`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ depends_on: draft.dependsOn }),
      })
      if (!dependencyResponse.ok) throw new Error(await errorMessage(dependencyResponse))
      await Promise.all([refreshTasks(), refreshAllocations()])
      setShowEditor(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'タスクを保存できませんでした。')
    } finally {
      setIsSaving(false)
    }
  }

  async function setTaskStatus(task: TaskItem, status: TaskStatus) {
    setError('')
    try {
      const response = await fetch(`/api/tasks/${task.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      if (!response.ok) throw new Error(await errorMessage(response))
      const updated = (await response.json()) as TaskItem
      setTasks((current) => current.map((item) => item.id === task.id ? updated : item))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'タスクの状態を更新できませんでした。')
    }
  }

  async function deleteTask(task: TaskItem) {
    setError('')
    try {
      const response = await fetch(`/api/tasks/${task.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await errorMessage(response))
      setTasks((current) => current.filter((item) => item.id !== task.id))
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'タスクを削除できませんでした。')
    }
  }

  const visibleTasks = tasks.filter((task) => {
    if (filter === 'done') return task.status === 'done'
    if (filter === 'active') return task.status === 'open' || task.status === 'in_progress'
    return true
  })
  const activeCount = tasks.filter((task) => task.status === 'open' || task.status === 'in_progress').length
  const doneCount = tasks.filter((task) => task.status === 'done').length
  const statusLabel: Record<TaskStatus, string> = {
    open: '未着手', in_progress: '進行中', done: '完了', cancelled: '中止',
  }
  const priorityLabel: Record<TaskPriority, string> = { low: '低', normal: '通常', high: '高' }
  const skipReason: Record<string, string> = {
    missing_estimate: '見積なし', deadline_passed: '期限切れ', prerequisite_unplanned: '前提タスク未配置',
  }
  const allocationsByTask = new Map<string, Allocation[]>()
  for (const allocation of allocations) allocationsByTask.set(allocation.task_id, [...(allocationsByTask.get(allocation.task_id) ?? []), allocation])
  const titleById = new Map(tasks.map((task) => [task.id, task.title]))

  return (
    <section className="task-stage" aria-label="タスク管理">
      <div className="task-toolbar">
        <div>
          <div className="eyebrow">ACTIONS / TASKS</div>
          <h2>やること</h2>
          <p>予定は確定した時間、タスクは完了させる作業として管理します。</p>
        </div>
        <div className="task-toolbar-actions">
          <button className="cancel-button" type="button" onClick={() => void loadPlan()} disabled={isPlanLoading}><CalendarClock size={15} />{isPlanLoading ? '計算中…' : '今週の計画を見る'}</button>
          <button className="add-event-button" type="button" onClick={openNewTask}><Plus size={16} />タスクを追加</button>
        </div>
      </div>

      {plan && <section className="week-plan-panel" aria-label="今週の配置案">
        <div className="week-plan-heading">
          <h3>今週の配置案（未保存）</h3>
          <div>
            <button className="dialog-close" type="button" onClick={onRequestWeekPlan}>チャットで案を作ってもらう</button>
            <button className="icon-button" type="button" aria-label="閉じる" onClick={() => setPlan(null)}>×</button>
          </div>
        </div>
        {plan.tasks.length === 0 ? <p>配置できるタスクはありません。</p> : <ol className="week-plan-list">
          {plan.tasks.map((item) => <li key={item.task_id}>
            <strong>{item.title}</strong> <span>スコア {item.priority_score}</span>
            {item.allocations.length > 0 && <ul>{item.allocations.map((slot) => <li key={slot.start_at}>{formatScheduledRange(slot.start_at, slot.end_at)}{slot.break_after_minutes ? `（休憩${slot.break_after_minutes}分）` : ''}</li>)}</ul>}
            {item.unallocated_minutes > 0 && <em>未配置 {item.unallocated_minutes}分</em>}
          </li>)}
        </ol>}
        {plan.skipped.length > 0 && <p className="week-plan-skipped">対象外: {plan.skipped.map((item) => `${item.title ?? item.task_id}（${skipReason[item.reason] ?? item.reason}）`).join('、')}</p>}
        <div className="week-plan-days">{plan.days.filter((day) => day.available_minutes > 0).map((day) => <span key={day.date}>{day.date.slice(5)} {day.planned_minutes}/{day.available_minutes}分（{Math.round(day.ratio * 100)}%）</span>)}</div>
      </section>}

      {error && <div className="error-banner task-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button></div>}

      <div className="task-list-toolbar">
        <div className="task-filter-switch" aria-label="タスクの絞り込み">
          <button className={filter === 'active' ? 'is-active' : ''} type="button" onClick={() => setFilter('active')}>未完了 <span>{activeCount}</span></button>
          <button className={filter === 'done' ? 'is-active' : ''} type="button" onClick={() => setFilter('done')}>完了 <span>{doneCount}</span></button>
          <button className={filter === 'all' ? 'is-active' : ''} type="button" onClick={() => setFilter('all')}>すべて <span>{tasks.length}</span></button>
        </div>
        <span className="task-sort-label">優先度・期限順</span>
      </div>

      {isLoading ? <div className="task-empty-state">読み込み中…</div> : visibleTasks.length === 0 ? (
        <div className="task-empty-state"><Check size={18} /><span>{filter === 'done' ? '完了したタスクはありません' : 'タスクはありません'}</span></div>
      ) : <div className="task-list">
        {visibleTasks.map((task) => <article className={`task-row ${task.status}`} key={task.id}>
          <button
            className={`task-complete-button ${task.status === 'done' ? 'is-done' : ''}`}
            type="button"
            onClick={() => void setTaskStatus(task, task.status === 'done' ? 'open' : 'done')}
            aria-label={task.status === 'done' ? `タスク「${task.title}」を未完了に戻す` : `タスク「${task.title}」を完了にする`}
            title={task.status === 'done' ? '未完了に戻す' : '完了にする'}
          >{task.status === 'done' ? <Check size={15} /> : null}</button>
          <div className="task-main">
            <div className="task-title-line"><h3>{task.title}</h3><span className={`task-status ${task.status}`}>{statusLabel[task.status]}</span></div>
            {task.description && <p className="task-description">{task.description}</p>}
            <div className="task-meta">
              {task.project_title && <span className="task-project">{task.project_title}</span>}
              <span className={`task-priority ${task.priority}`}>優先度 {priorityLabel[task.priority]}</span>
              <span className={task.due_at ? 'task-due' : 'task-no-due'}>期限 {formatDue(task.due_at)}</span>
              {task.estimated_minutes && <span className="task-estimate"><Clock3 size={12} />{task.estimated_minutes}分</span>}
              {task.depends_on?.length > 0 && <span className="task-dependency">前提: {task.depends_on.map((id) => titleById.get(id) ?? '不明').join('、')}</span>}
              {!task.splittable && <span className="task-dependency">分割しない</span>}
              {task.scheduled_start && task.scheduled_end
                ? <span className="task-scheduled"><CalendarClock size={12} />{formatScheduledRange(task.scheduled_start, task.scheduled_end)}</span>
                : task.status !== 'done' && task.status !== 'cancelled' && <button className="task-slot-button" type="button" onClick={() => onRequestSchedule(task)}><CalendarClock size={13} />時間を割り当て</button>}
            </div>
            {(allocationsByTask.get(task.id)?.length ?? 0) > 0 && <ul className="task-allocation-list">
              {allocationsByTask.get(task.id)!.map((allocation) => <li key={allocation.id}>
                <CalendarClock size={12} />{formatScheduledRange(allocation.start_at, allocation.end_at)}
                <button type="button" aria-label={`${task.title}の作業枠${formatScheduledRange(allocation.start_at, allocation.end_at)}を削除`} onClick={() => void deleteAllocation(allocation)}>×</button>
              </li>)}
            </ul>}
          </div>
          <div className="task-controls">
            <button className="calendar-icon-button" type="button" onClick={() => openEditTask(task)} aria-label={`${task.title}を編集`} title="編集"><Pencil size={15} /></button>
            <button className="calendar-icon-button danger" type="button" onClick={() => void deleteTask(task)} aria-label={`${task.title}を削除`} title="削除"><Trash2 size={15} /></button>
          </div>
        </article>)}
      </div>}

      {showEditor && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isSaving) setShowEditor(false)
      }}>
        <form className="settings-dialog task-dialog" role="dialog" aria-modal="true" aria-labelledby="task-dialog-title" onSubmit={saveTask}>
          <div className="dialog-heading"><div><div className="eyebrow">ACTIONS / TASK</div><h2 id="task-dialog-title">{editingId ? 'タスクを編集' : 'タスクを追加'}</h2></div>
            <button className="icon-button" type="button" onClick={() => setShowEditor(false)} aria-label="閉じる">×</button>
          </div>
          {error && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{error}</span></div>}
          <label className="task-field">タスク名<input autoFocus required maxLength={160} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} placeholder="例: 会議資料を準備する" /></label>
          <label className="task-field">プロジェクト<select value={draft.projectId} onChange={(event) => setDraft({ ...draft, projectId: event.target.value })}>
            <option value="">未分類</option>
            {projects.map((project) => <option key={project.id} value={project.id}>{project.title}</option>)}
          </select></label>
          <label className="task-field">説明<textarea rows={3} maxLength={4000} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="作業内容や補足" /></label>
          <div className="task-form-grid">
            <label className="task-field">期限<input type="datetime-local" value={draft.dueAt} onChange={(event) => setDraft({ ...draft, dueAt: event.target.value })} /></label>
            <label className="task-field">見積時間（分）<input type="number" min="1" max="1440" value={draft.estimatedMinutes} onChange={(event) => setDraft({ ...draft, estimatedMinutes: event.target.value })} placeholder="例: 30" /></label>
          </div>
          <label className="task-field">優先度<select value={draft.priority} onChange={(event) => setDraft({ ...draft, priority: event.target.value as TaskPriority })}>
            <option value="low">低</option><option value="normal">通常</option><option value="high">高</option>
          </select></label>
          <div className="task-form-grid">
            <label className="task-field">残り時間（分）<input type="number" min="0" max="1440" value={draft.remainingMinutes} onChange={(event) => setDraft({ ...draft, remainingMinutes: event.target.value })} /></label>
            <label className="task-field">集中度（1-5）<select value={draft.focusLevel} onChange={(event) => setDraft({ ...draft, focusLevel: event.target.value })}>
              <option value="">未設定</option>{[1, 2, 3, 4, 5].map((level) => <option key={level} value={level}>{level}</option>)}
            </select></label>
          </div>
          <label className="task-check"><input type="checkbox" checked={draft.splittable} onChange={(event) => setDraft({ ...draft, splittable: event.target.checked })} />複数の枠に分割してよい</label>
          <div className="task-form-grid">
            <label className="task-field">最低作業時間（分）<input type="number" min="1" max="1440" value={draft.minSession} onChange={(event) => setDraft({ ...draft, minSession: event.target.value })} /></label>
            <label className="task-field">最大作業時間（分）<input type="number" min="1" max="1440" value={draft.maxSession} onChange={(event) => setDraft({ ...draft, maxSession: event.target.value })} /></label>
          </div>
          {tasks.filter((task) => task.id !== editingId && (task.status === 'open' || task.status === 'in_progress')).length > 0 && <fieldset className="task-dependency-field">
            <legend>前提タスク（先に終えるもの）</legend>
            {tasks.filter((task) => task.id !== editingId && (task.status === 'open' || task.status === 'in_progress')).map((task) => <label className="task-check" key={task.id}>
              <input type="checkbox" checked={draft.dependsOn.includes(task.id)} onChange={(event) => setDraft({
                ...draft,
                dependsOn: event.target.checked ? [...draft.dependsOn, task.id] : draft.dependsOn.filter((id) => id !== task.id),
              })} />{task.title}
            </label>)}
          </fieldset>}
          <div className="event-dialog-actions"><button className="cancel-button" type="button" onClick={() => setShowEditor(false)} disabled={isSaving}>キャンセル</button><button className="dialog-close" type="submit" disabled={isSaving}>{isSaving ? '保存中…' : editingId ? '変更を保存' : 'タスクを保存'}</button></div>
        </form>
      </div>}
    </section>
  )
}