import { useEffect, useState, type FormEvent } from 'react'
import { CircleAlert, Pencil, Plus, RefreshCw, Repeat, Trash2 } from 'lucide-react'

type RoutineType = 'achievement' | 'maintenance' | 'fixed'
type RoutineItem = {
  id: string
  title: string
  routine_type: RoutineType
  target_count: number
  duration_minutes: number
  preferred_period: string | null
  min_count: number
  max_count: number
  description?: string
}
type PlanItem = {
  id: string
  routine_id: string
  period_start: string
  period_end: string
  target_count: number
  status: string
  adjustment_reason: string
  adjustment_source: string
}
type OccurrenceItem = {
  id: string
  plan_id: string
  routine_id: string
  scheduled_start: string
  scheduled_end: string
  status: string
}
type Draft = {
  title: string
  routineType: RoutineType
  targetCount: number
  minCount: number
  maxCount: number
  durationMinutes: number
  preferredPeriod: string
}

const typeLabels: Record<RoutineType, string> = {
  maintenance: '維持', achievement: '達成', fixed: '固定',
}
const occurrenceLabels: Record<string, string> = {
  scheduled: '予定', completed: '完了', partial: '一部実施', skipped: '未実施', cancelled: '中止',
}
const periodLabels: Record<string, string> = {
  '': '指定なし', morning: '午前', afternoon: '午後', evening: '夜',
}

function emptyDraft(): Draft {
  return { title: '', routineType: 'maintenance', targetCount: 3, minCount: 0, maxCount: 3, durationMinutes: 30, preferredPeriod: '' }
}

function formatSlot(start: string, end: string) {
  const day = new Intl.DateTimeFormat('ja-JP', { month: 'numeric', day: 'numeric', weekday: 'short' }).format(new Date(start))
  const time = new Intl.DateTimeFormat('ja-JP', { hour: '2-digit', minute: '2-digit', hour12: false })
  return `${day} ${time.format(new Date(start))}–${time.format(new Date(end))}`
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
  return response.json() as Promise<T>
}

export default function RoutinesView() {
  const [routines, setRoutines] = useState<RoutineItem[]>([])
  const [plans, setPlans] = useState<PlanItem[]>([])
  const [occurrences, setOccurrences] = useState<OccurrenceItem[]>([])
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [isLoading, setIsLoading] = useState(true)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [showForm, setShowForm] = useState(false)
  const [editingRoutineId, setEditingRoutineId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft>(emptyDraft)

  async function load() {
    const [routineItems, planItems, occurrenceItems] = await Promise.all([
      fetchJson<RoutineItem[]>('/api/routines?active=true'),
      fetchJson<PlanItem[]>('/api/routine-plans'),
      fetchJson<OccurrenceItem[]>('/api/routine-occurrences'),
    ])
    setRoutines(routineItems)
    setPlans(planItems)
    setOccurrences(occurrenceItems)
  }

  useEffect(() => {
    let active = true
    Promise.all([
      fetchJson<RoutineItem[]>('/api/routines?active=true'),
      fetchJson<PlanItem[]>('/api/routine-plans'),
      fetchJson<OccurrenceItem[]>('/api/routine-occurrences'),
    ])
      .then(([routineItems, planItems, occurrenceItems]) => {
        if (!active) return
        setRoutines(routineItems)
        setPlans(planItems)
        setOccurrences(occurrenceItems)
      })
      .catch((reason) => {
        if (active) setError(reason instanceof Error ? reason.message : 'ルーティンを取得できませんでした。')
      })
      .finally(() => { if (active) setIsLoading(false) })
    return () => { active = false }
  }, [])

  async function run(id: string, action: () => Promise<string>) {
    setBusyId(id)
    setError('')
    setNotice('')
    try {
      setNotice(await action())
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作に失敗しました。')
    } finally {
      setBusyId(null)
    }
  }

  function planThisWeek(routine: RoutineItem) {
    return run(routine.id, async () => {
      const result = await fetchJson<{ generation: { created: unknown[]; shortfall: number } | null }>(
        `/api/routines/${routine.id}/weekly-plan`,
        { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' },
      )
      const generation = result.generation
      return `「${routine.title}」の今週分を${generation?.created.length ?? 0}回配置しました。${generation?.shortfall ? `空きが足りず${generation.shortfall}回分は配置できませんでした。` : ''}`
    })
  }

  function reevaluate(routine: RoutineItem, plan: PlanItem) {
    return run(plan.id, async () => {
      const result = await fetchJson<{
        missed: unknown[]; created: unknown[]; previous_target_count: number; target_count: number
        target_locked: boolean; shortfall: number; below_minimum: boolean
      }>(`/api/routine-plans/${plan.id}/reevaluate`, { method: 'POST' })
      let message = `「${routine.title}」: 未実施${result.missed.length}件を見直し、${result.created.length}回分を再配置しました。`
      if (result.target_count !== result.previous_target_count) message += `目標を${result.previous_target_count}回から${result.target_count}回へ下げました。`
      else if (result.target_locked) message += `固定型のため目標は変えず、${result.shortfall}回分が不足しています。`
      if (result.below_minimum) message += '最低回数を下回っています。'
      return message
    })
  }

  async function createRoutine(event: FormEvent) {
    event.preventDefault()
    const editingRoutine = editingRoutineId !== null
    const routineId = editingRoutineId
    setBusyId(routineId ?? 'new')
    setError('')
    try {
      const fixed = draft.routineType === 'fixed'
      await fetchJson(editingRoutine ? `/api/routines/${routineId}` : '/api/routines', {
        method: editingRoutine ? 'PUT' : 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: draft.title,
          routine_type: draft.routineType,
          target_count: draft.targetCount,
          min_count: editingRoutine ? draft.minCount : 0,
          max_count: editingRoutine ? draft.maxCount : Math.max(draft.targetCount, 1),
          duration_minutes: draft.durationMinutes,
          preferred_period: draft.preferredPeriod || null,
          adjustment_policy: fixed ? 'fixed' : 'flexible',
          flexible: !fixed,
        }),
      })
      setDraft(emptyDraft())
      setShowForm(false)
      setEditingRoutineId(null)
      setNotice(editingRoutine
        ? `「${draft.title}」を更新しました。現在の週間計画・配置済みの予定は変更していません。`
        : `「${draft.title}」を追加しました。`)
      await load()
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存に失敗しました。')
    } finally {
      setBusyId(null)
    }
  }

  function editRoutine(routine: RoutineItem) {
    setDraft({
      title: routine.title,
      routineType: routine.routine_type,
      targetCount: routine.target_count,
      minCount: routine.min_count,
      maxCount: routine.max_count,
      durationMinutes: routine.duration_minutes,
      preferredPeriod: routine.preferred_period ?? '',
    })
    setShowForm(false)
    setEditingRoutineId(routine.id)
    setError('')
    setNotice('')
  }

  function closeForm() {
    setShowForm(false)
    setEditingRoutineId(null)
    setDraft(emptyDraft())
  }

  function deleteRoutine(routine: RoutineItem) {
    const pendingCount = occurrences.filter(
      (item) => item.routine_id === routine.id && item.status === 'scheduled',
    ).length
    if (!window.confirm(
      `「${routine.title}」を削除しますか？${pendingCount > 0 ? ` 未実施の予定${pendingCount}件も予定画面から削除します。` : ''}実績履歴は保持されます。`,
    )) return
    return run(routine.id, async () => {
      const response = await fetch(`/api/routines/${routine.id}`, { method: 'DELETE' })
      if (!response.ok) throw new Error(await errorMessage(response))
      return `「${routine.title}」を削除しました。未実施の予定${pendingCount}件を予定画面から削除し、実績履歴は保持しました。`
    })
  }

  return (
    <section className="goal-stage" aria-label="ルーティン">
      <header className="goal-toolbar">
        <div>
          <div className="eyebrow">HABITS / ROUTINES</div>
          <h2>ルーティン</h2>
          <p>週ごとの回数を空き時間へ配置し、未実施は残り日数で再調整します。</p>
        </div>
        <div className="goal-toolbar-actions">
          <button className="add-event-button" type="button" onClick={() => {
            setEditingRoutineId(null)
            setDraft(emptyDraft())
            setShowForm((value) => !value)
          }}><Plus size={16} />ルーティンを追加</button>
        </div>
      </header>

      {error && <div className="error-banner goal-error" role="alert"><CircleAlert size={16} /><span>{error}</span><button type="button" aria-label="エラーを閉じる" onClick={() => setError('')}>×</button></div>}
      {notice && <div className="goal-notice" role="status" aria-live="polite">{notice}</div>}

      {(showForm || editingRoutineId !== null) && <form className="routine-form" onSubmit={createRoutine}>
        {editingRoutineId !== null && <p className="goal-description">基本設定の更新です。すでに作成した週間計画と配置済みの予定には影響しません。</p>}
        <label className="task-field">名前<input required maxLength={160} value={draft.title} onChange={(event) => setDraft({ ...draft, title: event.target.value })} placeholder="例: ギターの練習" /></label>
        <div className="task-form-grid">
          <label className="task-field">種類<select value={draft.routineType} onChange={(event) => setDraft({ ...draft, routineType: event.target.value as RoutineType })}>
            {Object.entries(typeLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
          <label className="task-field">週の回数<input type="number" min={1} max={31} value={draft.targetCount} onChange={(event) => setDraft({ ...draft, targetCount: Number(event.target.value) || 1 })} /></label>
          {editingRoutineId !== null && <>
            <label className="task-field">最低回数<input type="number" min={0} max={31} value={draft.minCount} onChange={(event) => setDraft({ ...draft, minCount: Number(event.target.value) || 0 })} /></label>
            <label className="task-field">最大回数<input type="number" min={1} max={31} value={draft.maxCount} onChange={(event) => setDraft({ ...draft, maxCount: Number(event.target.value) || 1 })} /></label>
          </>}
          <label className="task-field">1回の分数<input type="number" min={1} max={1440} value={draft.durationMinutes} onChange={(event) => setDraft({ ...draft, durationMinutes: Number(event.target.value) || 1 })} /></label>
          <label className="task-field">希望時間帯<select value={draft.preferredPeriod} onChange={(event) => setDraft({ ...draft, preferredPeriod: event.target.value })}>
            {Object.entries(periodLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select></label>
        </div>
        <div className="goal-toolbar-actions">
          <button className="today-button" type="button" onClick={closeForm}>キャンセル</button>
          <button className="add-event-button" type="submit" disabled={busyId !== null}>{editingRoutineId === null ? '追加' : '更新'}</button>
        </div>
      </form>}

      {isLoading ? <div className="goal-empty-state">読み込み中…</div> : routines.length === 0 ? (
        <div className="goal-empty-state"><Repeat size={19} /><span>ルーティンはまだありません</span></div>
      ) : <div className="goal-list">
        {routines.map((routine) => {
          const routinePlans = plans.filter((plan) => plan.routine_id === routine.id)
          const current = routinePlans.filter((plan) => plan.status === 'planned' || plan.status === 'active').at(-1)
          const planOccurrences = current ? occurrences.filter((item) => item.plan_id === current.id) : []
          return <article className="goal-row" key={routine.id}>
            <header className="goal-heading">
              <div className="goal-title-block">
                <Repeat size={16} aria-hidden="true" />
                <div><h3>{routine.title}</h3><span>{typeLabels[routine.routine_type]} ・週{routine.target_count}回 ・{routine.duration_minutes}分{routine.preferred_period ? ` ・${periodLabels[routine.preferred_period] ?? routine.preferred_period}` : ''}</span></div>
              </div>
              <div className="goal-controls">
                <button className="calendar-icon-button" type="button" disabled={busyId !== null} onClick={() => editRoutine(routine)} aria-label={`${routine.title}を編集`} title="ルーティンを編集"><Pencil size={15} /></button>
                <button className="calendar-icon-button danger" type="button" disabled={busyId !== null} onClick={() => void deleteRoutine(routine)} aria-label={`${routine.title}を削除`} title="ルーティンを削除"><Trash2 size={15} /></button>
                {current && <button className="calendar-icon-button" type="button" disabled={busyId === current.id} onClick={() => void reevaluate(routine, current)} aria-label={`${routine.title}を再評価`} title="未実施を再評価"><RefreshCw size={15} /></button>}
                {!current && <button className="today-button" type="button" disabled={busyId === routine.id} onClick={() => void planThisWeek(routine)}>今週を計画</button>}
              </div>
            </header>
            {current && <>
              <p className="goal-description">{current.period_start}〜{current.period_end} ・目標{current.target_count}回{current.adjustment_source === 'norn' && current.adjustment_reason ? ` ・${current.adjustment_reason.split('\n').at(-1)}` : ''}</p>
              <ul className="routine-occurrences">
                {planOccurrences.length === 0 ? <li>配置された実施予定はありません</li> : planOccurrences.map((item) => <li key={item.id}>
                  <span>{formatSlot(item.scheduled_start, item.scheduled_end)}</span><span className="routine-status">{occurrenceLabels[item.status] ?? item.status}</span>
                </li>)}
              </ul>
            </>}
          </article>
        })}
      </div>}
    </section>
  )
}
