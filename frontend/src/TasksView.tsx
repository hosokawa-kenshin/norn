import { useEffect, useState, type FormEvent } from 'react'
import { Check, CircleAlert, Clock3, Pencil, Plus, Trash2 } from 'lucide-react'

type TaskStatus = 'open' | 'in_progress' | 'done' | 'cancelled'
type TaskPriority = 'low' | 'normal' | 'high'
type TaskItem = {
  id: string
  title: string
  description: string
  due_at: string | null
  estimated_minutes: number | null
  priority: TaskPriority
  status: TaskStatus
  created_at: string
  updated_at: string
  completed_at: string | null
}
type TaskDraft = {
  title: string
  description: string
  dueAt: string
  estimatedMinutes: string
  priority: TaskPriority
}
type TaskFilter = 'active' | 'done' | 'all'

function emptyDraft(): TaskDraft {
  return { title: '', description: '', dueAt: '', estimatedMinutes: '', priority: 'normal' }
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

async function errorMessage(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string'
    ? result.detail
    : `リクエストに失敗しました (${response.status})`
}

export default function TasksView() {
  const [tasks, setTasks] = useState<TaskItem[]>([])
  const [filter, setFilter] = useState<TaskFilter>('active')
  const [error, setError] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [showEditor, setShowEditor] = useState(false)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draft, setDraft] = useState<TaskDraft>(emptyDraft)

  async function refreshTasks() {
    const response = await fetch('/api/tasks')
    if (!response.ok) throw new Error(await errorMessage(response))
    setTasks((await response.json()) as TaskItem[])
  }

  useEffect(() => {
    let active = true
    fetch('/api/tasks')
      .then(async (response) => {
        if (!response.ok) throw new Error(await errorMessage(response))
        return (await response.json()) as TaskItem[]
      })
      .then((value) => { if (active) setTasks(value) })
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
      description: task.description,
      dueAt: toLocalDateTime(task.due_at),
      estimatedMinutes: task.estimated_minutes?.toString() ?? '',
      priority: task.priority,
    })
    setError('')
    setShowEditor(true)
  }

  async function saveTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setIsSaving(true)
    setError('')
    const payload = {
      title: draft.title,
      description: draft.description,
      due_at: draft.dueAt ? new Date(draft.dueAt).toISOString() : null,
      estimated_minutes: draft.estimatedMinutes ? Number(draft.estimatedMinutes) : null,
      priority: draft.priority,
    }
    try {
      const response = await fetch(editingId ? `/api/tasks/${editingId}` : '/api/tasks', {
        method: editingId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!response.ok) throw new Error(await errorMessage(response))
      await refreshTasks()
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

  return (
    <section className="task-stage" aria-label="タスク管理">
      <div className="task-toolbar">
        <div>
          <div className="eyebrow">ACTIONS / TASKS</div>
          <h2>やること</h2>
          <p>予定は確定した時間、タスクは完了させる作業として管理します。</p>
        </div>
        <button className="add-event-button" type="button" onClick={openNewTask}><Plus size={16} />タスクを追加</button>
      </div>

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
              <span className={`task-priority ${task.priority}`}>優先度 {priorityLabel[task.priority]}</span>
              <span className={task.due_at ? 'task-due' : 'task-no-due'}>期限 {formatDue(task.due_at)}</span>
              {task.estimated_minutes && <span className="task-estimate"><Clock3 size={12} />{task.estimated_minutes}分</span>}
            </div>
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
          <label className="task-field">説明<textarea rows={3} maxLength={4000} value={draft.description} onChange={(event) => setDraft({ ...draft, description: event.target.value })} placeholder="作業内容や補足" /></label>
          <div className="task-form-grid">
            <label className="task-field">期限<input type="datetime-local" value={draft.dueAt} onChange={(event) => setDraft({ ...draft, dueAt: event.target.value })} /></label>
            <label className="task-field">見積時間（分）<input type="number" min="1" max="1440" value={draft.estimatedMinutes} onChange={(event) => setDraft({ ...draft, estimatedMinutes: event.target.value })} placeholder="例: 30" /></label>
          </div>
          <label className="task-field">優先度<select value={draft.priority} onChange={(event) => setDraft({ ...draft, priority: event.target.value as TaskPriority })}>
            <option value="low">低</option><option value="normal">通常</option><option value="high">高</option>
          </select></label>
          <div className="event-dialog-actions"><button className="cancel-button" type="button" onClick={() => setShowEditor(false)} disabled={isSaving}>キャンセル</button><button className="dialog-close" type="submit" disabled={isSaving}>{isSaving ? '保存中…' : editingId ? '変更を保存' : 'タスクを保存'}</button></div>
        </form>
      </div>}
    </section>
  )
}