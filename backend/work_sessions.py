import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

import execution
from database import database_transaction, now_iso, open_database
from execution import ExecutionLogPayload, ExecutionStatus, LOCAL_TIMEZONE

router = APIRouter()


class WorkSessionStart(BaseModel):
    task_id: str | None = None
    routine_id: str | None = None
    occurrence_id: str | None = None
    schedule_id: str | None = None

    @model_validator(mode='after')
    def validate_target(self):
        if not (self.task_id or self.routine_id or self.occurrence_id):
            raise ValueError('タスク・ルーティン・実施予定のいずれかを指定してください。')
        for value in (self.task_id, self.routine_id, self.occurrence_id, self.schedule_id):
            if value is not None:
                try:
                    uuid.UUID(value)
                except ValueError as error:
                    raise ValueError('関連IDが不正です。') from error
        return self


class WorkSessionFinish(BaseModel):
    status: ExecutionStatus = 'completed'
    note: str = Field(default='', max_length=4000)
    resume_point: str = Field(default='', max_length=2000)
    interruption_reason: str = Field(default='', max_length=2000)
    progress_percent: int | None = Field(default=None, ge=0, le=100)


def _latest_resume_point(connection: Any, task_id: str | None, routine_id: str | None) -> str:
    column, value = ('task_id', task_id) if task_id else ('routine_id', routine_id)
    if not value:
        return ''
    row = connection.execute(
        f"SELECT resume_point FROM execution_logs WHERE {column} = ? AND resume_point != '' ORDER BY created_at DESC LIMIT 1",
        (value,),
    ).fetchone()
    return row['resume_point'] if row else ''


def _session_view(connection: Any, row: Any) -> dict[str, Any]:
    session = dict(row)
    title = None
    if session['task_id']:
        found = connection.execute('SELECT title FROM tasks WHERE id = ?', (session['task_id'],)).fetchone()
        title = found['title'] if found else None
    elif session['routine_id']:
        found = connection.execute('SELECT title FROM routines WHERE id = ?', (session['routine_id'],)).fetchone()
        title = found['title'] if found else None
    session['title'] = title
    session['resume_point'] = _latest_resume_point(connection, session['task_id'], session['routine_id'])
    return session


@router.get('/api/work-sessions/active')
def get_active_work_session() -> dict[str, Any] | None:
    with closing(open_database()) as connection:
        row = connection.execute('SELECT * FROM work_sessions ORDER BY started_at LIMIT 1').fetchone()
        return _session_view(connection, row) if row else None


@router.post('/api/work-sessions/start', status_code=201)
def start_work_session(payload: WorkSessionStart) -> dict[str, Any]:
    with database_transaction() as connection:
        if connection.execute('SELECT id FROM work_sessions LIMIT 1').fetchone():
            raise HTTPException(status_code=409, detail='進行中の作業があります。先に終了してください。')
        routine_id, schedule_id = payload.routine_id, payload.schedule_id
        if payload.occurrence_id:
            occurrence = connection.execute(
                'SELECT routine_id, schedule_id FROM routine_occurrences WHERE id = ?', (payload.occurrence_id,),
            ).fetchone()
            if occurrence is None:
                raise HTTPException(status_code=404, detail='関連するルーティン実施予定が見つかりません。')
            routine_id = routine_id or occurrence['routine_id']
            schedule_id = schedule_id or occurrence['schedule_id']
        for table, record_id, message in (
            ('tasks', payload.task_id, '関連するタスクが見つかりません。'),
            ('routines', routine_id, '関連するルーティンが見つかりません。'),
            ('events', schedule_id, '関連する予定が見つかりません。'),
        ):
            if record_id and not connection.execute(f'SELECT id FROM {table} WHERE id = ?', (record_id,)).fetchone():
                raise HTTPException(status_code=404, detail=message)
        session_id = str(uuid.uuid4())
        timestamp = datetime.now(LOCAL_TIMEZONE).isoformat(timespec='seconds')
        connection.execute(
            '''INSERT INTO work_sessions (id, task_id, routine_id, occurrence_id, schedule_id, started_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (session_id, payload.task_id, routine_id, payload.occurrence_id, schedule_id, timestamp, now_iso()),
        )
        return _session_view(connection, connection.execute('SELECT * FROM work_sessions WHERE id = ?', (session_id,)).fetchone())


@router.post('/api/work-sessions/{session_id}/finish')
def finish_work_session(session_id: str, payload: WorkSessionFinish) -> dict[str, Any]:
    with database_transaction() as connection:
        session = connection.execute('SELECT * FROM work_sessions WHERE id = ?', (session_id,)).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail='進行中の作業が見つかりません。')
        started = datetime.fromisoformat(session['started_at'])
        ended = max(datetime.now(LOCAL_TIMEZONE), started + timedelta(seconds=1))
        log = execution.create_execution_log(ExecutionLogPayload(
            schedule_id=session['schedule_id'], task_id=session['task_id'], routine_id=session['routine_id'],
            occurrence_id=session['occurrence_id'], started_at=started, ended_at=ended,
            status=payload.status, note=payload.note, resume_point=payload.resume_point,
            interruption_reason=payload.interruption_reason, progress_percent=payload.progress_percent,
        ))
        connection.execute('DELETE FROM work_sessions WHERE id = ?', (session_id,))
        return log


@router.delete('/api/work-sessions/{session_id}', status_code=204)
def discard_work_session(session_id: str) -> None:
    with closing(open_database()) as connection:
        deleted = connection.execute('DELETE FROM work_sessions WHERE id = ?', (session_id,)).rowcount
        connection.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail='進行中の作業が見つかりません。')


def build_execution_summary(start: date, end: date) -> dict[str, Any]:
    window_start = datetime.combine(start, time.min, LOCAL_TIMEZONE).isoformat()
    window_end = datetime.combine(end + timedelta(days=1), time.min, LOCAL_TIMEZONE).isoformat()
    with closing(open_database()) as connection:
        logs = [dict(row) for row in connection.execute(
            '''SELECT * FROM execution_logs
               WHERE datetime(COALESCE(started_at, ended_at, created_at)) >= datetime(?)
                 AND datetime(COALESCE(started_at, ended_at, created_at)) < datetime(?)''',
            (window_start, window_end),
        ).fetchall()]
        allocations = [dict(row) for row in connection.execute(
            'SELECT task_id, start_at, end_at FROM task_allocations WHERE datetime(start_at) >= datetime(?) AND datetime(start_at) < datetime(?)',
            (window_start, window_end),
        ).fetchall()]
        occurrences = [dict(row) for row in connection.execute(
            'SELECT routine_id, scheduled_start, scheduled_end, status FROM routine_occurrences WHERE datetime(scheduled_start) >= datetime(?) AND datetime(scheduled_start) < datetime(?)',
            (window_start, window_end),
        ).fetchall()]
        tasks = {row['id']: dict(row) for row in connection.execute('SELECT id, title, estimated_minutes FROM tasks').fetchall()}
        routines = {row['id']: dict(row) for row in connection.execute('SELECT id, title FROM routines').fetchall()}
        resume = {
            ('task_id', row['task_id']): row['resume_point'] for row in connection.execute(
                "SELECT task_id, resume_point FROM execution_logs WHERE task_id IS NOT NULL AND resume_point != '' ORDER BY created_at",
            ).fetchall()
        }
        resume.update({
            ('routine_id', row['routine_id']): row['resume_point'] for row in connection.execute(
                "SELECT routine_id, resume_point FROM execution_logs WHERE routine_id IS NOT NULL AND resume_point != '' ORDER BY created_at",
            ).fetchall()
        })

    def minutes(start_at: str, end_at: str) -> int:
        return int((datetime.fromisoformat(end_at) - datetime.fromisoformat(start_at)).total_seconds() // 60)

    def bucket() -> dict[str, Any]:
        return {'planned_minutes': 0, 'actual_minutes': 0, 'completed': 0, 'partial': 0, 'skipped': 0, 'cancelled': 0}

    task_rows: dict[str, dict[str, Any]] = {}
    routine_rows: dict[str, dict[str, Any]] = {}

    def task_row(task_id: str) -> dict[str, Any]:
        task = tasks.get(task_id, {})
        return task_rows.setdefault(task_id, {
            'task_id': task_id, 'title': task.get('title'), 'estimated_minutes': task.get('estimated_minutes'),
            'resume_point': resume.get(('task_id', task_id), ''), **bucket(),
        })

    def routine_row(routine_id: str) -> dict[str, Any]:
        return routine_rows.setdefault(routine_id, {
            'routine_id': routine_id, 'title': routines.get(routine_id, {}).get('title'),
            'resume_point': resume.get(('routine_id', routine_id), ''), **bucket(),
        })

    for allocation in allocations:
        task_row(allocation['task_id'])['planned_minutes'] += minutes(allocation['start_at'], allocation['end_at'])
    for occurrence in occurrences:
        if occurrence['status'] != 'cancelled':
            routine_row(occurrence['routine_id'])['planned_minutes'] += minutes(occurrence['scheduled_start'], occurrence['scheduled_end'])
    for log in logs:
        row = task_row(log['task_id']) if log['task_id'] else routine_row(log['routine_id']) if log['routine_id'] else None
        if row is None:
            continue
        row['actual_minutes'] += log['actual_minutes'] or 0
        row[log['status']] += 1

    totals = bucket()
    for row in [*task_rows.values(), *routine_rows.values()]:
        for key in totals:
            totals[key] += row[key]
    attempted = totals['completed'] + totals['partial'] + totals['skipped']
    totals['achievement_rate'] = round((totals['completed'] + totals['partial']) / attempted, 2) if attempted else None
    totals['time_difference_minutes'] = totals['actual_minutes'] - totals['planned_minutes']
    return {
        'start': start.isoformat(), 'end': end.isoformat(), 'totals': totals,
        'tasks': sorted(task_rows.values(), key=lambda item: item['title'] or ''),
        'routines': sorted(routine_rows.values(), key=lambda item: item['title'] or ''),
    }


@router.get('/api/execution-summary')
def execution_summary(start: date | None = None, end: date | None = None) -> dict[str, Any]:
    today = datetime.now(LOCAL_TIMEZONE).date()
    start = start or today - timedelta(days=today.weekday())
    end = end or start + timedelta(days=6)
    if end < start or (end - start).days > 92:
        raise HTTPException(status_code=422, detail='集計期間は開始日以降で93日以内にしてください。')
    return build_execution_summary(start, end)
