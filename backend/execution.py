import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from database import now_iso, open_database

router = APIRouter()
LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
ExecutionStatus = Literal['completed', 'partial', 'skipped', 'cancelled']


class ExecutionLogPayload(BaseModel):
    schedule_id: str | None = None
    task_id: str | None = None
    routine_id: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    actual_minutes: int | None = Field(default=None, ge=0, le=1440)
    status: ExecutionStatus
    note: str = Field(default='', max_length=4000)
    resume_point: str = Field(default='', max_length=2000)
    recorded_source: Literal['user', 'norn'] = 'user'

    @field_validator('schedule_id', 'task_id', 'routine_id')
    @classmethod
    def validate_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError('関連IDが不正です。') from error

    @field_validator('started_at', 'ended_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)

    @model_validator(mode='after')
    def validate_execution(self):
        if not (self.schedule_id or self.task_id or self.routine_id):
            raise ValueError('予定・タスク・ルーティンのいずれかを指定してください。')
        if self.started_at and self.ended_at and self.ended_at <= self.started_at:
            raise ValueError('終了時刻は開始時刻より後にしてください。')
        if self.status not in {'skipped', 'cancelled'} and not (self.started_at or self.ended_at):
            raise ValueError('実行開始または終了時刻を指定してください。')
        return self


def _execution_row(connection: Any, log_id: str) -> dict[str, Any]:
    row = connection.execute(
        'SELECT * FROM execution_logs WHERE id = ?', (log_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail='実績記録が見つかりません。')
    return dict(row)


@router.get('/api/execution-logs')
def list_execution_logs(
    task_id: str | None = None,
    routine_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail='期間の開始と終了を両方指定してください。')
    conditions = []
    parameters: list[Any] = []
    for column, value in (('task_id', task_id), ('routine_id', routine_id)):
        if value is not None:
            try:
                value = str(uuid.UUID(value))
            except ValueError as error:
                raise HTTPException(status_code=422, detail='関連IDが不正です。') from error
            conditions.append(f'{column} = ?')
            parameters.append(value)
    if start is not None and end is not None:
        if end <= start:
            raise HTTPException(status_code=422, detail='期間の終了は開始より後にしてください。')
        start = start.replace(tzinfo=LOCAL_TIMEZONE) if start.tzinfo is None else start.astimezone(LOCAL_TIMEZONE)
        end = end.replace(tzinfo=LOCAL_TIMEZONE) if end.tzinfo is None else end.astimezone(LOCAL_TIMEZONE)
        conditions.append(
            "datetime(COALESCE(started_at, ended_at, created_at)) >= datetime(?) "
            "AND datetime(COALESCE(started_at, ended_at, created_at)) < datetime(?)"
        )
        parameters.extend((start.isoformat(), end.isoformat()))
    query = 'SELECT * FROM execution_logs'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY created_at DESC, id LIMIT 500'
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


@router.post('/api/execution-logs', status_code=201)
def create_execution_log(payload: ExecutionLogPayload) -> dict[str, Any]:
    with closing(open_database()) as connection:
        for table, record_id, message in (
            ('events', payload.schedule_id, '関連する予定が見つかりません。'),
            ('tasks', payload.task_id, '関連するタスクが見つかりません。'),
            ('routines', payload.routine_id, '関連するルーティンが見つかりません。'),
        ):
            if record_id is not None and not connection.execute(
                f'SELECT id FROM {table} WHERE id = ?', (record_id,),
            ).fetchone():
                raise HTTPException(status_code=404, detail=message)
        actual_minutes = payload.actual_minutes
        if actual_minutes is None and payload.started_at and payload.ended_at:
            actual_minutes = int((payload.ended_at - payload.started_at).total_seconds() // 60)
        log_id = str(uuid.uuid4())
        connection.execute(
            '''INSERT INTO execution_logs (
                id, schedule_id, task_id, routine_id, started_at, ended_at, actual_minutes,
                status, note, resume_point, recorded_source, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                log_id, payload.schedule_id, payload.task_id, payload.routine_id,
                payload.started_at.isoformat() if payload.started_at else None,
                payload.ended_at.isoformat() if payload.ended_at else None,
                actual_minutes, payload.status, payload.note.strip(), payload.resume_point.strip(),
                payload.recorded_source, now_iso(),
            ),
        )
        connection.commit()
        return _execution_row(connection, log_id)
