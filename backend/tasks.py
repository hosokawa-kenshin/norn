import uuid
from contextlib import closing
from datetime import datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from database import now_iso, open_database
from settings import PLANNING_DAY_END, PLANNING_DAY_START, PLANNING_WORKDAYS

LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
TaskStatus = Literal['open', 'in_progress', 'done', 'cancelled']
TaskPriority = Literal['low', 'normal', 'high']
router = APIRouter()


class TaskPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    project_id: str | None = None
    description: str = Field(default='', max_length=4000)
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    priority: TaskPriority = 'normal'

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('タスク名を入力してください。')
        return value

    @field_validator('project_id')
    @classmethod
    def clean_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError('プロジェクトIDが不正です。') from error

    @field_validator('due_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    project_id: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    priority: TaskPriority | None = None
    status: TaskStatus | None = None

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('タスク名を空にはできません。')
        return value

    @field_validator('project_id')
    @classmethod
    def clean_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError('プロジェクトIDが不正です。') from error

    @field_validator('due_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)


class TaskSchedulePayload(BaseModel):
    start_at: datetime

    @field_validator('start_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)


TASK_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_tasks',
            'description': 'NORNのタスクを検索する。状態、期限、プロジェクトで絞り込める。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['open', 'in_progress', 'done', 'cancelled']},
                    'due_before': {'type': 'string', 'format': 'date-time'},
                    'project_id': {'type': 'string'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_task',
            'description': 'ユーザーの明確な依頼、または本人の「しなきゃ」「する必要がある」など具体的な未完了作業の実行意思からタスクを追加する。作成前にlist_tasksで重複を確認する。期限、見積時間、優先度は明示された場合だけ指定し、時間未定でも登録する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'project_id': {'type': 'string'},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'due_at': {'type': 'string', 'format': 'date-time'},
                    'estimated_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
                    'priority': {'type': 'string', 'enum': ['low', 'normal', 'high']},
                },
                'required': ['title'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_task',
            'description': '特定済みのNORNタスクをユーザー依頼に従って更新する。自動登録済みの作業が後の会話で具体化した場合も、重複作成せず更新する。task_idは検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'project_id': {'type': 'string'},
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'due_at': {'type': 'string', 'format': 'date-time'},
                    'estimated_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
                    'priority': {'type': 'string', 'enum': ['low', 'normal', 'high']},
                    'status': {'type': 'string', 'enum': ['open', 'in_progress', 'done', 'cancelled']},
                },
                'required': ['task_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'complete_task',
            'description': '特定済みのNORNタスクを完了にする。task_idは検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_task',
            'description': 'ユーザーが依頼した特定のNORNタスクを削除する。task_idは検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'find_task_slots',
            'description': '指定期間からタスクに合う空き時間を検索する。予定や他のタスク時間枠との重複を避け、作業時間内の候補だけを返す。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'from_at': {'type': 'string', 'format': 'date-time'},
                    'through_at': {'type': 'string', 'format': 'date-time'},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                },
                'required': ['task_id', 'from_at', 'through_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'schedule_task',
            'description': '空き時間検索結果から選んだ開始時刻にタスクを割り当てる。所要時間はタスクの見積時間を使う。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'start_at': {'type': 'string', 'format': 'date-time'},
                },
                'required': ['task_id', 'start_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'unschedule_task',
            'description': 'タスクに割り当てた時間枠を解除する。タスク本体は削除しない。',
            'parameters': {
                'type': 'object',
                'properties': {'task_id': {'type': 'string'}},
                'required': ['task_id'],
                'additionalProperties': False,
            },
        },
    },
]


def task_datetime_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE).isoformat(timespec='seconds')


def configured_time(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError('作業時間の設定はHH:MM形式で指定してください。') from error


def ceil_to_quarter_hour(value: datetime) -> datetime:
    if value.second or value.microsecond:
        value += timedelta(minutes=1)
    rounded_minute = ((value.minute + 14) // 15) * 15
    if rounded_minute == 60:
        return (value + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return value.replace(minute=rounded_minute, second=0, microsecond=0)


def find_task_slots(
    task_id: str,
    from_at: datetime,
    through_at: datetime,
    limit: int = 5,
) -> dict[str, Any]:
    task_id = str(uuid.UUID(task_id))
    range_start = task_datetime_iso(from_at)
    range_end = task_datetime_iso(through_at)
    if range_start is None or range_end is None or range_end <= range_start:
        raise HTTPException(status_code=422, detail='検索期間が正しくありません。')
    local_start = datetime.fromisoformat(range_start)
    local_end = datetime.fromisoformat(range_end)
    local_start = max(local_start, datetime.now(LOCAL_TIMEZONE))
    if local_end - local_start > timedelta(days=31):
        raise HTTPException(status_code=422, detail='空き時間の検索期間は31日以内にしてください。')

    with closing(open_database()) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        if task['status'] in {'done', 'cancelled'}:
            raise HTTPException(status_code=409, detail='完了または中止したタスクには時間枠を割り当てられません。')
        if task['estimated_minutes'] is None:
            raise HTTPException(status_code=409, detail='時間枠の検索には見積時間が必要です。')
        if task['due_at']:
            local_end = min(local_end, datetime.fromisoformat(task['due_at']))
        if local_end <= local_start:
            return {'task_id': task_id, 'title': task['title'], 'slots': []}

        busy_events = connection.execute(
            'SELECT start_at, end_at FROM events WHERE start_at < ? AND end_at > ?',
            (task_datetime_iso(local_end), task_datetime_iso(local_start)),
        ).fetchall()
        busy_tasks = connection.execute(
            '''
            SELECT start_at, end_at FROM task_allocations
            WHERE task_id != ? AND start_at < ? AND end_at > ?
            ''',
            (task_id, task_datetime_iso(local_end), task_datetime_iso(local_start)),
        ).fetchall()

    busy_periods = sorted(
        [(datetime.fromisoformat(row['start_at']), datetime.fromisoformat(row['end_at']))
         for row in [*busy_events, *busy_tasks]],
        key=lambda period: period[0],
    )
    work_start = configured_time(PLANNING_DAY_START)
    work_end = configured_time(PLANNING_DAY_END)
    if work_end <= work_start or not PLANNING_WORKDAYS or any(day < 0 or day > 6 for day in PLANNING_WORKDAYS):
        raise HTTPException(status_code=500, detail='作業時間・曜日の設定が正しくありません。')

    duration = timedelta(minutes=task['estimated_minutes'])
    maximum = max(1, min(limit, 10))
    slots: list[dict[str, str]] = []
    current_day = local_start.date()
    while current_day <= local_end.date() and len(slots) < maximum:
        if current_day.weekday() in PLANNING_WORKDAYS:
            day_start = datetime.combine(current_day, work_start, LOCAL_TIMEZONE)
            day_end = datetime.combine(current_day, work_end, LOCAL_TIMEZONE)
            cursor = max(day_start, local_start)
            day_limit = min(day_end, local_end)
            for busy_start, busy_end in busy_periods:
                if busy_end <= cursor or busy_start >= day_limit:
                    continue
                gap_end = min(busy_start, day_limit)
                candidate = ceil_to_quarter_hour(cursor)
                while candidate + duration <= gap_end and len(slots) < maximum:
                    slots.append({
                        'start_at': task_datetime_iso(candidate) or '',
                        'end_at': task_datetime_iso(candidate + duration) or '',
                    })
                    candidate += timedelta(minutes=15)
                cursor = max(cursor, busy_end)
                if cursor >= day_limit:
                    break
            candidate = ceil_to_quarter_hour(cursor)
            while candidate + duration <= day_limit and len(slots) < maximum:
                slots.append({
                    'start_at': task_datetime_iso(candidate) or '',
                    'end_at': task_datetime_iso(candidate + duration) or '',
                })
                candidate += timedelta(minutes=15)
        current_day += timedelta(days=1)

    return {
        'task_id': task_id,
        'title': task['title'],
        'estimated_minutes': task['estimated_minutes'],
        'slots': slots,
    }


def schedule_task_record(task_id: str, start_at: datetime) -> dict[str, Any]:
    task_id = str(uuid.UUID(task_id))
    start = datetime.fromisoformat(task_datetime_iso(start_at) or '')
    timestamp = now_iso()
    with closing(open_database()) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        if task['status'] in {'done', 'cancelled'}:
            raise HTTPException(status_code=409, detail='完了または中止したタスクには時間枠を割り当てられません。')
        if task['estimated_minutes'] is None:
            raise HTTPException(status_code=409, detail='時間枠の割り当てには見積時間が必要です。')

        end = start + timedelta(minutes=task['estimated_minutes'])
        work_start = configured_time(PLANNING_DAY_START)
        work_end = configured_time(PLANNING_DAY_END)
        if start.minute % 15 != 0 or start.second or start.microsecond:
            raise HTTPException(status_code=409, detail='時間枠の開始時刻は15分刻みにしてください。')
        if start < datetime.now(LOCAL_TIMEZONE):
            raise HTTPException(status_code=409, detail='過去の時間にはタスクを割り当てられません。')
        if start.weekday() not in PLANNING_WORKDAYS:
            raise HTTPException(status_code=409, detail='設定された作業曜日以外には割り当てられません。')
        if start.time() < work_start or end.date() != start.date() or end.time() > work_end:
            raise HTTPException(status_code=409, detail='設定された作業時間内に収まりません。')
        if task['due_at'] and end > datetime.fromisoformat(task['due_at']):
            raise HTTPException(status_code=409, detail='タスクの期限を過ぎる時間枠です。')

        start_iso = task_datetime_iso(start)
        end_iso = task_datetime_iso(end)
        conflicts = connection.execute(
            'SELECT title FROM events WHERE start_at < ? AND end_at > ?', (end_iso, start_iso)
        ).fetchall()
        conflicts += connection.execute(
            '''
            SELECT tasks.title FROM task_allocations
            JOIN tasks ON tasks.id = task_allocations.task_id
            WHERE task_allocations.task_id != ?
                AND task_allocations.start_at < ? AND task_allocations.end_at > ?
            ''',
            (task_id, end_iso, start_iso),
        ).fetchall()
        if conflicts:
            titles = '、'.join(row['title'] for row in conflicts[:3])
            raise HTTPException(status_code=409, detail=f'時間が重複する予定またはタスクがあります: {titles}')

        connection.execute(
            '''
            INSERT INTO task_allocations (id, task_id, start_at, end_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                start_at = excluded.start_at, end_at = excluded.end_at, updated_at = excluded.updated_at
            ''',
            (str(uuid.uuid4()), task_id, start_iso, end_iso, timestamp, timestamp),
        )
        connection.commit()
        allocation = connection.execute(
            '''
            SELECT task_allocations.*, tasks.title, tasks.estimated_minutes
            FROM task_allocations JOIN tasks ON tasks.id = task_allocations.task_id
            WHERE task_allocations.task_id = ?
            ''',
            (task_id,),
        ).fetchone()
    return dict(allocation)


def unschedule_task_record(task_id: str) -> bool:
    task_id = str(uuid.UUID(task_id))
    with closing(open_database()) as connection:
        exists = connection.execute('SELECT id FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        result = connection.execute('DELETE FROM task_allocations WHERE task_id = ?', (task_id,))
        connection.commit()
    return result.rowcount > 0
def query_tasks(
    status: TaskStatus | None = None,
    due_before: datetime | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[str] = []
    if status is not None:
        conditions.append('tasks.status = ?')
        parameters.append(status)
    if due_before is not None:
        conditions.append('tasks.due_at IS NOT NULL AND tasks.due_at <= ?')
        parameters.append(task_datetime_iso(due_before) or '')
    if project_id is not None:
        project_id = str(uuid.UUID(project_id))
        conditions.append('tasks.project_id = ?')
        parameters.append(project_id)
    query = '''
        SELECT tasks.*, projects.title AS project_title,
            task_allocations.start_at AS scheduled_start,
            task_allocations.end_at AS scheduled_end
        FROM tasks
        LEFT JOIN projects ON projects.id = tasks.project_id
        LEFT JOIN task_allocations ON task_allocations.task_id = tasks.id
    '''
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY CASE tasks.priority WHEN \'high\' THEN 0 WHEN \'normal\' THEN 1 ELSE 2 END, tasks.due_at IS NULL, tasks.due_at, tasks.created_at DESC'
    with closing(open_database()) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def create_task_record(payload: TaskPayload) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        if payload.project_id and not connection.execute(
            'SELECT id FROM projects WHERE id = ?', (payload.project_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail='関連付けるプロジェクトが見つかりません。')
        connection.execute(
            '''
            INSERT INTO tasks (
                id, project_id, title, description, due_at, estimated_minutes, priority,
                status, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL)
            ''',
            (
                task_id,
                payload.project_id,
                payload.title,
                payload.description.strip(),
                task_datetime_iso(payload.due_at),
                payload.estimated_minutes,
                payload.priority,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            '''
            SELECT tasks.*, task_allocations.start_at AS scheduled_start,
                task_allocations.end_at AS scheduled_end, projects.title AS project_title
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            LEFT JOIN task_allocations ON task_allocations.task_id = tasks.id
            WHERE tasks.id = ?
            ''',
            (task_id,),
        ).fetchone()
    return dict(row)


def update_task_record(task_id: str, payload: TaskUpdate) -> dict[str, Any]:
    with closing(open_database()) as connection:
        row = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        values: dict[str, Any] = dict(row)
        for field in payload.model_fields_set:
            value = getattr(payload, field)
            values[field] = task_datetime_iso(value) if field == 'due_at' else value
        if 'project_id' in payload.model_fields_set and values['project_id'] and not connection.execute(
            'SELECT id FROM projects WHERE id = ?', (values['project_id'],)
        ).fetchone():
            raise HTTPException(status_code=404, detail='関連付けるプロジェクトが見つかりません。')
        if 'description' in payload.model_fields_set:
            values['description'] = (values['description'] or '').strip()
        timestamp = now_iso()
        completed_at = timestamp if values['status'] == 'done' else None
        if values['status'] in {'done', 'cancelled'}:
            connection.execute('DELETE FROM task_allocations WHERE task_id = ?', (task_id,))
        elif {'due_at', 'estimated_minutes'} & payload.model_fields_set:
            connection.execute('DELETE FROM task_allocations WHERE task_id = ?', (task_id,))
        connection.execute(
            '''
            UPDATE tasks SET title = ?, project_id = ?, description = ?, due_at = ?, estimated_minutes = ?,
                priority = ?, status = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            ''',
            (
                values['title'], values['project_id'], values['description'], values['due_at'], values['estimated_minutes'],
                values['priority'], values['status'], timestamp, completed_at, task_id,
            ),
        )
        connection.commit()
        updated = connection.execute(
            '''
            SELECT tasks.*, task_allocations.start_at AS scheduled_start,
                task_allocations.end_at AS scheduled_end, projects.title AS project_title
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            LEFT JOIN task_allocations ON task_allocations.task_id = tasks.id
            WHERE tasks.id = ?
            ''',
            (task_id,),
        ).fetchone()
    return dict(updated)


def delete_task_record(task_id: str) -> str:
    with closing(open_database()) as connection:
        row = connection.execute('SELECT title FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        connection.execute('DELETE FROM tasks WHERE id = ?', (task_id,))
        connection.commit()
    return row['title']


@router.get('/api/tasks')
def list_tasks(
    status: TaskStatus | None = None,
    due_before: datetime | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    return query_tasks(status, due_before, project_id)


@router.post('/api/tasks', status_code=201)
def create_task(payload: TaskPayload) -> dict[str, Any]:
    return create_task_record(payload)


@router.patch('/api/tasks/{task_id}')
def update_task(task_id: str, payload: TaskUpdate) -> dict[str, Any]:
    return update_task_record(task_id, payload)


@router.delete('/api/tasks/{task_id}', status_code=204)
def delete_task(task_id: str) -> None:
    delete_task_record(task_id)


@router.get('/api/task-allocations')
def list_task_allocations(start: datetime, end: datetime) -> list[dict[str, Any]]:
    range_start = task_datetime_iso(start)
    range_end = task_datetime_iso(end)
    if not range_start or not range_end or range_start >= range_end:
        raise HTTPException(status_code=422, detail='期間の開始と終了を正しく指定してください。')
    with closing(open_database()) as connection:
        rows = connection.execute(
            '''
            SELECT task_allocations.*, tasks.title, tasks.estimated_minutes, tasks.status
            FROM task_allocations JOIN tasks ON tasks.id = task_allocations.task_id
            WHERE task_allocations.start_at < ? AND task_allocations.end_at > ?
            ORDER BY task_allocations.start_at
            ''',
            (range_end, range_start),
        ).fetchall()
    return [dict(row) for row in rows]


@router.get('/api/tasks/{task_id}/available-slots')
def get_task_slots(
    task_id: str,
    from_at: datetime,
    through_at: datetime,
    limit: int = Query(default=5, ge=1, le=10),
) -> dict[str, Any]:
    return find_task_slots(task_id, from_at, through_at, limit)


@router.put('/api/tasks/{task_id}/schedule')
def schedule_task(task_id: str, payload: TaskSchedulePayload) -> dict[str, Any]:
    return schedule_task_record(task_id, payload.start_at)


@router.delete('/api/tasks/{task_id}/schedule', status_code=204)
def unschedule_task(task_id: str) -> None:
    unschedule_task_record(task_id)


def execute_task_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == 'list_tasks':
            if not set(arguments) <= {'status', 'due_before', 'project_id'}:
                raise ValueError('許可されていない検索条件です。')
            due_before = datetime.fromisoformat(arguments['due_before']) if arguments.get('due_before') else None
            return {
                'ok': True,
                'tasks': query_tasks(arguments.get('status'), due_before, arguments.get('project_id')),
            }

        if name == 'create_task':
            if not set(arguments) <= {
                'title', 'project_id', 'description', 'due_at', 'estimated_minutes', 'priority'
            }:
                raise ValueError('許可されていないタスク項目が含まれています。')
            task = create_task_record(TaskPayload(**arguments))
            due_text = f'、期限は{task["due_at"]}' if task['due_at'] else ''
            return {
                'ok': True,
                'task': task,
                'summary': f'タスク「{task["title"]}」を追加しました{due_text}。',
            }

        if name == 'find_task_slots':
            if not set(arguments) <= {'task_id', 'from_at', 'through_at', 'limit'}:
                raise ValueError('許可されていない検索条件です。')
            if not {'task_id', 'from_at', 'through_at'} <= set(arguments):
                raise ValueError('タスクIDと検索期間を指定してください。')
            slots = find_task_slots(
                arguments['task_id'],
                datetime.fromisoformat(arguments['from_at']),
                datetime.fromisoformat(arguments['through_at']),
                int(arguments.get('limit', 5)),
            )
            slots['ok'] = True
            return slots

        if name == 'schedule_task':
            if set(arguments) != {'task_id', 'start_at'}:
                raise ValueError('タスクIDと開始時刻を指定してください。')
            allocation = schedule_task_record(arguments['task_id'], datetime.fromisoformat(arguments['start_at']))
            return {
                'ok': True,
                'allocation': allocation,
                'summary': f'タスク「{allocation["title"]}」を{allocation["start_at"]}に割り当てました。',
            }

        if name == 'unschedule_task':
            if set(arguments) != {'task_id'}:
                raise ValueError('タスクIDのみ指定してください。')
            removed = unschedule_task_record(arguments['task_id'])
            return {
                'ok': True,
                'summary': 'タスクの時間枠を解除しました。' if removed else 'このタスクに時間枠は設定されていません。',
            }

        if name in {'update_task', 'complete_task', 'delete_task'}:
            task_id = str(uuid.UUID(arguments.get('task_id', '')))
            if name == 'complete_task':
                if set(arguments) != {'task_id'}:
                    raise ValueError('タスクIDのみ指定してください。')
                task = update_task_record(task_id, TaskUpdate(status='done'))
                return {'ok': True, 'task': task, 'summary': f'タスク「{task["title"]}」を完了にしました。'}
            if name == 'delete_task':
                if set(arguments) != {'task_id'}:
                    raise ValueError('タスクIDのみ指定してください。')
                title = delete_task_record(task_id)
                return {'ok': True, 'summary': f'タスク「{title}」を削除しました。'}

            changes = set(arguments) - {'task_id'}
            allowed = {
                'title', 'project_id', 'description', 'due_at', 'estimated_minutes', 'priority', 'status'
            }
            if not changes or not changes <= allowed:
                raise ValueError('更新するタスク項目を指定してください。')
            task = update_task_record(task_id, TaskUpdate(**{key: value for key, value in arguments.items() if key != 'task_id'}))
            return {'ok': True, 'task': task, 'summary': f'タスク「{task["title"]}」を更新しました。'}

        return {'ok': False, 'error': '許可されていないタスク操作です。'}
    except HTTPException as error:
        return {'ok': False, 'error': error.detail}
    except (ValueError, TypeError, KeyError) as error:
        return {'ok': False, 'error': f'タスク操作の引数が不正です: {error}'}
