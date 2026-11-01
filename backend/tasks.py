import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from database import now_iso, open_database

LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
TaskStatus = Literal['open', 'in_progress', 'done', 'cancelled']
TaskPriority = Literal['low', 'normal', 'high']
router = APIRouter()


class TaskPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
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

    @field_validator('due_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)


TASK_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_tasks',
            'description': 'NORNのタスクを検索する。状態や期限で絞り込める。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'status': {'type': 'string', 'enum': ['open', 'in_progress', 'done', 'cancelled']},
                    'due_before': {'type': 'string', 'format': 'date-time'},
                },
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_task',
            'description': 'ユーザーが依頼した作業タスクをNORNに追加する。期限、見積時間、優先度は分かる場合に指定する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
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
            'description': '特定済みのNORNタスクをユーザー依頼に従って更新する。task_idは検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
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
]


def task_datetime_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE).isoformat(timespec='seconds')


def query_tasks(status: TaskStatus | None = None, due_before: datetime | None = None) -> list[dict[str, Any]]:
    conditions: list[str] = []
    parameters: list[str] = []
    if status is not None:
        conditions.append('status = ?')
        parameters.append(status)
    if due_before is not None:
        conditions.append('due_at IS NOT NULL AND due_at <= ?')
        parameters.append(task_datetime_iso(due_before) or '')
    query = 'SELECT * FROM tasks'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY CASE priority WHEN \'high\' THEN 0 WHEN \'normal\' THEN 1 ELSE 2 END, due_at IS NULL, due_at, created_at DESC'
    with closing(open_database()) as connection:
        rows = connection.execute(query, parameters).fetchall()
    return [dict(row) for row in rows]


def create_task_record(payload: TaskPayload) -> dict[str, Any]:
    task_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        connection.execute(
            '''
            INSERT INTO tasks (
                id, title, description, due_at, estimated_minutes, priority,
                status, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, NULL)
            ''',
            (
                task_id,
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
        row = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
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
        if 'description' in payload.model_fields_set:
            values['description'] = (values['description'] or '').strip()
        timestamp = now_iso()
        completed_at = timestamp if values['status'] == 'done' else None
        connection.execute(
            '''
            UPDATE tasks SET title = ?, description = ?, due_at = ?, estimated_minutes = ?,
                priority = ?, status = ?, updated_at = ?, completed_at = ?
            WHERE id = ?
            ''',
            (
                values['title'], values['description'], values['due_at'], values['estimated_minutes'],
                values['priority'], values['status'], timestamp, completed_at, task_id,
            ),
        )
        connection.commit()
        updated = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
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
) -> list[dict[str, Any]]:
    return query_tasks(status, due_before)


@router.post('/api/tasks', status_code=201)
def create_task(payload: TaskPayload) -> dict[str, Any]:
    return create_task_record(payload)


@router.patch('/api/tasks/{task_id}')
def update_task(task_id: str, payload: TaskUpdate) -> dict[str, Any]:
    return update_task_record(task_id, payload)


@router.delete('/api/tasks/{task_id}', status_code=204)
def delete_task(task_id: str) -> None:
    delete_task_record(task_id)


def execute_task_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == 'list_tasks':
            if not set(arguments) <= {'status', 'due_before'}:
                raise ValueError('許可されていない検索条件です。')
            due_before = datetime.fromisoformat(arguments['due_before']) if arguments.get('due_before') else None
            return {'ok': True, 'tasks': query_tasks(arguments.get('status'), due_before)}

        if name == 'create_task':
            if not set(arguments) <= {'title', 'description', 'due_at', 'estimated_minutes', 'priority'}:
                raise ValueError('許可されていないタスク項目が含まれています。')
            task = create_task_record(TaskPayload(**arguments))
            due_text = f'、期限は{task["due_at"]}' if task['due_at'] else ''
            return {
                'ok': True,
                'task': task,
                'summary': f'タスク「{task["title"]}」を追加しました{due_text}。',
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
            allowed = {'title', 'description', 'due_at', 'estimated_minutes', 'priority', 'status'}
            if not changes or not changes <= allowed:
                raise ValueError('更新するタスク項目を指定してください。')
            task = update_task_record(task_id, TaskUpdate(**{key: value for key, value in arguments.items() if key != 'task_id'}))
            return {'ok': True, 'task': task, 'summary': f'タスク「{task["title"]}」を更新しました。'}

        return {'ok': False, 'error': '許可されていないタスク操作です。'}
    except HTTPException as error:
        return {'ok': False, 'error': error.detail}
    except (ValueError, TypeError, KeyError) as error:
        return {'ok': False, 'error': f'タスク操作の引数が不正です: {error}'}
