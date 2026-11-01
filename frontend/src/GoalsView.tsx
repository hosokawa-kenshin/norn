import { useEffect, useState, type FormEvent } from 'react'
import { CircleAlert, Flag, FolderKanban, Pencil, Plus, Trash2 } from 'lucide-react'

type LifecycleStatus = 'active' | 'paused' | 'completed' | 'cancelled'
type ProjectItem = {
  id: string
  goal_id: string | null
  goal_title: string | null
  title: string
  description: string
  status: LifecycleStatus
  target_date: string | null
  next_action: string
  task_count: number
  completed_task_count: number
  progress: number
}
type GoalItem = {
  id: string
  title: string
  description: string
  status: LifecycleStatus
  target_date: string | null
  progress: number
  project_count: number
  completed_project_count: number
  projects: ProjectItem[]
}
type GoalDraft = {
  title: string
  description: string
  targetDate: string
  status: LifecycleStatus
}
type ProjectDraft = {
  title: string
  goalId: string
  description: string
  targetDate: string
  nextAction: string
  status: LifecycleStatus
}
type EditorKind = 'goal' | 'project' | null
type GoalsViewProps = {
  onPlanProject: (project: ProjectItem) => Promise<string>
}

const statusLabels: Record<LifecycleStatus, string> = {
  active: '進行中', paused: '保留', completed: '完了', cancelled: '中止',
}

function emptyGoalDraft(): GoalDraft {
  return { title: '', description: '', targetDate: '', status: 'active' }
}

function emptyProjectDraft(goalId = ''): ProjectDraft {
  return { title: '', goalId, description: '', targetDate: '', nextAction: '', status: 'active' }
}

function formatDate(value: string | null) {
  if (!value) return '期限なし'
  return new Intl.DateTimeFormat('ja-JP', { year: 'numeric', month: 'numeric', day: 'numeric' })
    .format(new Date(`${value.slice(0, 10)}T00:00:00`))
}

async function errorMessage(response: Response) {
  const result = await response.json().catch(() => null)
  return typeof result?.detail === 'string'
    ? result.detail
    : `リクエストに失敗しました (${response.status})`
}

export default function GoalsView({ onPlanProject }: GoalsViewProps) {
  const [goals, setGoals] = useState<GoalItem[]>([])
  const [looseProjects, setLooseProjects] = useState<ProjectItem[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [isSaving, setIsSaving] = useState(false)
  const [editorKind, setEditorKind] = useState<EditorKind>(null)
  const [editingGoalId, setEditingGoalId] = useState<string | null>(null)
  const [editingProjectId, setEditingProjectId] = useState<string | null>(null)
  const [goalDraft, setGoalDraft] = useState<GoalDraft>(emptyGoalDraft)
  const [projectDraft, setProjectDraft] = useState<ProjectDraft>(emptyProjectDraft)

  async function refresh() {
    const [goalResponse, projectResponse] = await Promise.all([
      fetch('/api/goals'),
      fetch('/api/projects'),
    ])
    if (!goalResponse.ok) throw new Error(await errorMessage(goalResponse))
    if (!projectResponse.ok) throw new Error(await errorMessage(projectResponse))
    const [goalItems, projectItems] = await Promise.all([
      goalResponse.json() as Promise<GoalItem[]>,
      projectResponse.json() as Promise<ProjectItem[]>,
    ])
    setGoals(goalItems)
    setLooseProjects(projectItems.filter((project) => !project.goal_id))
  }

  useEffect(() => {
    let active = true
    Promise.all([fetch('/api/goals'), fetch('/api/projects')])
      .then(async ([goalResponse, projectResponse]) => {
        if (!goalResponse.ok) throw new Error(await errorMessage(goalResponse))
        if (!projectResponse.ok) throw new Error(await errorMessage(projectResponse))
        return Promise.all([
          goalResponse.json() as Promise<GoalItem[]>,
          projectResponse.json() as Promise<ProjectItem[]>,
        ])
      })
      .then(([goalItems, projectItems]) => {
        if (!active) return
        setGoals(goalItems)
        setLooseProjects(projectItems.filter((project) => !project.goal_id))
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : '目標を取得できませんでした。')
      })
      .finally(() => { if (active) setIsLoading(false) })
    return () => { active = false }
  }, [])

  function openNewGoal() {
    setEditingGoalId(null)
    setGoalDraft(emptyGoalDraft())
    setEditorKind('goal')
    setError('')
  }

  function openEditGoal(goal: GoalItem) {
    setEditingGoalId(goal.id)
    setGoalDraft({
      title: goal.title,
      description: goal.description,
      targetDate: goal.target_date ?? '',
      status: goal.status,
    })
    setEditorKind('goal')
    setError('')
  }

  function openNewProject(goalId = '') {
    setEditingProjectId(null)
    setProjectDraft(emptyProjectDraft(goalId))
    setEditorKind('project')
    setError('')
  }

  function openEditProject(project: ProjectItem) {
    setEditingProjectId(project.id)
    setProjectDraft({
      title: project.title,
      goalId: project.goal_id ?? '',
      description: project.description,
      targetDate: project.target_date ?? '',
      nextAction: project.next_action,
      status: project.status,
    })
    setEditorKind('project')
    setError('')
  }

  async function saveGoal(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setIsSaving(true)
    setError('')
    try {
      const response = await fetch(editingGoalId ? `/api/goals/${editingGoalId}` : '/api/goals', {
        method: editingGoalId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: goalDraft.title,
          description: goalDraft.description,
          target_date: goalDraft.targetDate || null,
          status: goalDraft.status,
        }),
      })
      if (!response.ok) throw new Error(await errorMessage(response))
      await refresh()
      setEditorKind(null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '目標を保存できませんでした。')
    } finally {
      setIsSaving(false)
    }
  }

  async function saveProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setIsSaving(true)
    setError('')
    try {
      const response = await fetch(editingProjectId ? `/api/projects/${editingProjectId}` : '/api/projects', {
        method: editingProjectId ? 'PATCH' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: projectDraft.title,
          goal_id: projectDraft.goalId || null,
          description: projectDraft.description,
          target_date: projectDraft.targetDate || null,
          next_action: projectDraft.nextAction,
          status: projectDraft.status,
        }),
      })
      if (!response.ok) throw new Error(await errorMessage(response))
      const savedProject = (await response.json()) as ProjectItem
      await refresh()
      setEditorKind(null)
      if (!editingProjectId && savedProject.status === 'active') {
        setNotice('NORNが初期タスクを考えています…')
        try {
          const assistantMessage = await onPlanProject(savedProject)
          setNotice(assistantMessage || '初期タスクを追加しました。')
          await refresh()
        } catch (reason) {
          setNotice(`プロジェクトは保存しましたが、タスク生成に失敗しました: ${reason instanceof Error ? reason.message : '不明なエラー'}`)
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'プロジェクトを保存できませんでした。')
    } finally {
      setIsSaving(false)
    }
  }

  async function deleteGoal(goal: GoalItem) {
    setError('')
    try {
      const response = await fetch(`/api/goals/${goal.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await errorMessage(response))
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '目標を削除できませんでした。')
    }
  }

  async function deleteProject(project: ProjectItem) {
    setError('')
    try {
      const response = await fetch(`/api/projects/${project.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await errorMessage(response))
      await refresh()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'プロジェクトを削除できませんでした。')
    }
  }

  return (
    <section className="goal-stage" aria-label="目標とプロジェクト">
      <header className="goal-toolbar">
        <div>
          <div className="eyebrow">DIRECTION / PROJECTS</div>
          <h2>目指すこと</h2>
          <p>目標にプロジェクトをまとめ、次の一歩とタスクの進み具合を見ます。</p>
        </div>
        <div className="goal-toolbar-actions">
          <button className="today-button" type="button" onClick={() => openNewProject()}>プロジェクトを追加</button>
          <button className="add-event-button" type="button" onClick={openNewGoal}><Plus size={16} />目標を追加</button>
        </div>
      </header>

      {error && <div className="error-banner goal-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button></div>}
      {notice && <div className="goal-notice" role="status" aria-live="polite">{notice}</div>}

      {isLoading ? <div className="goal-empty-state">読み込み中…</div> : goals.length === 0 && looseProjects.length === 0 ? (
        <div className="goal-empty-state"><Flag size={19} /><span>目標もプロジェクトもありません</span></div>
      ) : <div className="goal-list">
        {goals.map((goal) => <article className="goal-row" key={goal.id}>
          <header className="goal-heading">
            <div className="goal-title-block">
              <Flag size={16} aria-hidden="true" />
              <div><h3>{goal.title}</h3><span>{statusLabels[goal.status]} ・期限 {formatDate(goal.target_date)}</span></div>
            </div>
            <div className="goal-controls">
              <button className="calendar-icon-button" type="button" onClick={() => openNewProject(goal.id)} aria-label={`${goal.title}にプロジェクトを追加`} title="プロジェクトを追加"><Plus size={15} /></button>
              <button className="calendar-icon-button" type="button" onClick={() => openEditGoal(goal)} aria-label={`${goal.title}を編集`} title="目標を編集"><Pencil size={15} /></button>
              <button className="calendar-icon-button danger" type="button" onClick={() => void deleteGoal(goal)} aria-label={`${goal.title}を削除`} title="目標を削除"><Trash2 size={15} /></button>
            </div>
          </header>
          {goal.description && <p className="goal-description">{goal.description}</p>}
          <div className="progress-line" aria-label={`目標の進捗 ${goal.progress}%`}>
            <div className="progress-track"><span style={{ width: `${goal.progress}%` }} /></div>
            <span>{goal.progress}%</span>
          </div>
          {goal.projects.length > 0 ? <div className="goal-project-list">
            {goal.projects.map((project) => <ProjectRow key={project.id} project={project} onEdit={openEditProject} onDelete={deleteProject} />)}
          </div> : <p className="goal-no-projects">プロジェクトはまだありません</p>}
        </article>)}

        {looseProjects.length > 0 && <section className="loose-projects" aria-label="目標未設定のプロジェクト">
          <h3>目標未設定</h3>
          {looseProjects.map((project) => <ProjectRow key={project.id} project={project} onEdit={openEditProject} onDelete={deleteProject} />)}
        </section>}
      </div>}

      {editorKind && <div className="modal-backdrop" role="presentation" onMouseDown={(event) => {
        if (event.target === event.currentTarget && !isSaving) setEditorKind(null)
      }}>
        {editorKind === 'goal' ? (
          <form className="settings-dialog goal-dialog" role="dialog" aria-modal="true" aria-labelledby="goal-dialog-title" onSubmit={saveGoal}>
            <div className="dialog-heading"><div><div className="eyebrow">DIRECTION / GOAL</div><h2 id="goal-dialog-title">{editingGoalId ? '目標を編集' : '目標を追加'}</h2></div>
              <button className="icon-button" type="button" onClick={() => setEditorKind(null)} aria-label="閉じる">×</button>
            </div>
            {error && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{error}</span></div>}
            <label className="task-field">目標名<input autoFocus required maxLength={160} value={goalDraft.title} onChange={(event) => setGoalDraft({ ...goalDraft, title: event.target.value })} placeholder="例: 来年までに自作曲を完成させる" /></label>
            <label className="task-field">説明<textarea rows={3} maxLength={4000} value={goalDraft.description} onChange={(event) => setGoalDraft({ ...goalDraft, description: event.target.value })} placeholder="達成したい状態" /></label>
            <div className="task-form-grid">
              <label className="task-field">期限<input type="date" value={goalDraft.targetDate} onChange={(event) => setGoalDraft({ ...goalDraft, targetDate: event.target.value })} /></label>
              <label className="task-field">状態<select value={goalDraft.status} onChange={(event) => setGoalDraft({ ...goalDraft, status: event.target.value as LifecycleStatus })}><LifecycleOptions /></select></label>
            </div>
            <DialogActions isSaving={isSaving} isEditing={Boolean(editingGoalId)} onCancel={() => setEditorKind(null)} saveLabel="目標" />
          </form>
        ) : (
          <form className="settings-dialog goal-dialog" role="dialog" aria-modal="true" aria-labelledby="project-dialog-title" onSubmit={saveProject}>
            <div className="dialog-heading"><div><div className="eyebrow">DIRECTION / PROJECT</div><h2 id="project-dialog-title">{editingProjectId ? 'プロジェクトを編集' : 'プロジェクトを追加'}</h2></div>
              <button className="icon-button" type="button" onClick={() => setEditorKind(null)} aria-label="閉じる">×</button>
            </div>
            {error && <div className="error-banner" role="alert"><CircleAlert size={15} /><span>{error}</span></div>}
            <label className="task-field">プロジェクト名<input autoFocus required maxLength={160} value={projectDraft.title} onChange={(event) => setProjectDraft({ ...projectDraft, title: event.target.value })} placeholder="例: アルバム制作" /></label>
            <label className="task-field">関連する目標<select value={projectDraft.goalId} onChange={(event) => setProjectDraft({ ...projectDraft, goalId: event.target.value })}>
              <option value="">目標に紐付けない</option>
              {goals.map((goal) => <option key={goal.id} value={goal.id}>{goal.title}</option>)}
            </select></label>
            <label className="task-field">説明<textarea rows={2} maxLength={4000} value={projectDraft.description} onChange={(event) => setProjectDraft({ ...projectDraft, description: event.target.value })} placeholder="取り組みの内容" /></label>
            <label className="task-field">次のアクション<input maxLength={500} value={projectDraft.nextAction} onChange={(event) => setProjectDraft({ ...projectDraft, nextAction: event.target.value })} placeholder="例: 歌詞の下書きを作る" /></label>
            <div className="task-form-grid">
              <label className="task-field">期限<input type="date" value={projectDraft.targetDate} onChange={(event) => setProjectDraft({ ...projectDraft, targetDate: event.target.value })} /></label>
              <label className="task-field">状態<select value={projectDraft.status} onChange={(event) => setProjectDraft({ ...projectDraft, status: event.target.value as LifecycleStatus })}><LifecycleOptions /></select></label>
            </div>
            <DialogActions isSaving={isSaving} isEditing={Boolean(editingProjectId)} onCancel={() => setEditorKind(null)} saveLabel="プロジェクト" />
          </form>
        )}
      </div>}
    </section>
  )
}

function ProjectRow({
  project,
  onEdit,
  onDelete,
}: {
  project: ProjectItem
  onEdit: (project: ProjectItem) => void
  onDelete: (project: ProjectItem) => void
}) {
  return <article className="project-row">
    <FolderKanban size={15} aria-hidden="true" />
    <div className="project-main">
      <div className="project-title-line"><h4>{project.title}</h4><span className={`project-status ${project.status}`}>{statusLabels[project.status]}</span><span className="project-deadline">期限 {formatDate(project.target_date)}</span></div>
      {project.description && <p>{project.description}</p>}
      <div className="project-progress-line">
        <div className="progress-track"><span style={{ width: `${project.progress}%` }} /></div>
        <span>{project.progress}% ・タスク {project.completed_task_count}/{project.task_count}</span>
      </div>
      {project.next_action && <div className="project-next-action"><strong>次</strong><span>{project.next_action}</span></div>}
    </div>
    <div className="goal-controls">
      <button className="calendar-icon-button" type="button" onClick={() => onEdit(project)} aria-label={`${project.title}を編集`} title="プロジェクトを編集"><Pencil size={14} /></button>
      <button className="calendar-icon-button danger" type="button" onClick={() => void onDelete(project)} aria-label={`${project.title}を削除`} title="プロジェクトを削除"><Trash2 size={14} /></button>
    </div>
  </article>
}

function LifecycleOptions() {
  return <>
    <option value="active">進行中</option>
    <option value="paused">保留</option>
    <option value="completed">完了</option>
    <option value="cancelled">中止</option>
  </>
}

function DialogActions({
  isSaving,
  isEditing,
  onCancel,
  saveLabel,
}: {
  isSaving: boolean
  isEditing: boolean
  onCancel: () => void
  saveLabel: string
}) {
  return <div className="event-dialog-actions">
    <button className="cancel-button" type="button" onClick={onCancel} disabled={isSaving}>キャンセル</button>
    <button className="dialog-close" type="submit" disabled={isSaving}>{isSaving ? '保存中…' : isEditing ? '変更を保存' : `${saveLabel}を保存`}</button>
  </div>
}
