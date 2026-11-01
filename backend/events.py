import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator, model_validator

from database import now_iso, open_database

LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
router = APIRouter()


class EventPayload(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default='', max_length=2000)
    start_at: datetime
    end_at: datetime

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('予定名を入力してください。')
        return value

    @field_validator('start_at', 'end_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)

    @model_validator(mode='after')
    def validate_time_range(self) -> 'EventPayload':
        if self.end_at <= self.start_at:
            raise ValueError('終了時刻は開始時刻より後にしてください。')
        return self


SCHEDULE_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_events',
            'description': '指定した期間にあるNORN内の予定を検索する。日時はAsia/Tokyo。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'start_at': {'type': 'string', 'format': 'date-time'},
                    'end_at': {'type': 'string', 'format': 'date-time'},
                },
                'required': ['start_at', 'end_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_event',
            'description': 'ユーザーが依頼した予定をNORN内に承認なしで追加する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                    'description': {'type': 'string', 'maxLength': 2000},
                    'start_at': {'type': 'string', 'format': 'date-time'},
                    'end_at': {'type': 'string', 'format': 'date-time'},
                },
                'required': ['title', 'start_at', 'end_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_event',
            'description': '特定済みのNORN内予定をユーザー依頼に従って承認なしで変更する。event_idは予定検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'event_id': {'type': 'string'},
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 120},
                    'description': {'type': 'string', 'maxLength': 2000},
                    'start_at': {'type': 'string', 'format': 'date-time'},
                    'end_at': {'type': 'string', 'format': 'date-time'},
                },
                'required': ['event_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_event',
            'description': '特定済みのNORN内予定をユーザー依頼に従って承認なしで削除する。event_idは予定検索結果のidを使う。',
            'parameters': {
                'type': 'object',
                'properties': {'event_id': {'type': 'string'}},
                'required': ['event_id'],
                'additionalProperties': False,
            },
        },
    },
]


def event_datetime_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TIMEZONE)
    return value.astimezone(LOCAL_TIMEZONE).isoformat(timespec='seconds')


def find_event_conflicts(
    connection: sqlite3.Connection,
    start_at: str,
    end_at: str,
    exclude_id: str | None = None,
) -> list[sqlite3.Row]:
    query = '''
        SELECT id, title, start_at, end_at FROM events
        WHERE start_at < ? AND end_at > ?
    '''
    parameters: list[str] = [end_at, start_at]
    if exclude_id is not None:
        query += ' AND id != ?'
        parameters.append(exclude_id)
    query += ' ORDER BY start_at'
    return connection.execute(query, parameters).fetchall()


def reject_event_conflicts(conflicts: list[sqlite3.Row]) -> None:
    if conflicts:
        titles = '、'.join(row['title'] for row in conflicts[:3])
        raise HTTPException(status_code=409, detail=f'時間が重複する予定があります: {titles}')


def query_events(start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail='期間の開始と終了を両方指定してください。')

    with closing(open_database()) as connection:
        if start is None or end is None:
            rows = connection.execute('SELECT * FROM events ORDER BY start_at').fetchall()
        else:
            range_start = event_datetime_iso(start)
            range_end = event_datetime_iso(end)
            if range_start >= range_end:
                raise HTTPException(status_code=422, detail='期間の終了は開始より後にしてください。')
            rows = connection.execute(
                '''
                SELECT * FROM events
                WHERE start_at < ? AND end_at > ?
                ORDER BY start_at
                ''',
                (range_end, range_start),
            ).fetchall()
    return [dict(row) for row in rows]


def create_event_record(payload: EventPayload) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    start_at = event_datetime_iso(payload.start_at)
    end_at = event_datetime_iso(payload.end_at)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        reject_event_conflicts(find_event_conflicts(connection, start_at, end_at))
        connection.execute(
            '''
            INSERT INTO events (id, title, description, start_at, end_at, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (event_id, payload.title, payload.description.strip(), start_at, end_at, timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    return dict(row)


def update_event_record(event_id: str, payload: EventPayload) -> dict[str, Any]:
    start_at = event_datetime_iso(payload.start_at)
    end_at = event_datetime_iso(payload.end_at)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        existing = connection.execute('SELECT id FROM events WHERE id = ?', (event_id,)).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail='予定が見つかりません。')
        reject_event_conflicts(find_event_conflicts(connection, start_at, end_at, event_id))
        connection.execute(
            '''
            UPDATE events SET title = ?, description = ?, start_at = ?, end_at = ?, updated_at = ?
            WHERE id = ?
            ''',
            (payload.title, payload.description.strip(), start_at, end_at, timestamp, event_id),
        )
        connection.commit()
        row = connection.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    return dict(row)


def delete_event_record(event_id: str) -> str:
    with closing(open_database()) as connection:
        row = connection.execute('SELECT title FROM events WHERE id = ?', (event_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='予定が見つかりません。')
        connection.execute('DELETE FROM events WHERE id = ?', (event_id,))
        connection.commit()
    return row['title']


@router.get('/api/events')
def list_events(start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    return query_events(start, end)


@router.post('/api/events', status_code=201)
def create_event(payload: EventPayload) -> dict[str, Any]:
    return create_event_record(payload)


@router.put('/api/events/{event_id}')
def update_event(event_id: str, payload: EventPayload) -> dict[str, Any]:
    return update_event_record(event_id, payload)


@router.delete('/api/events/{event_id}', status_code=204)
def delete_event(event_id: str) -> None:
    delete_event_record(event_id)


def execute_schedule_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == 'list_events':
            if set(arguments) != {'start_at', 'end_at'}:
                raise ValueError('検索期間の開始と終了を指定してください。')
            start_at = datetime.fromisoformat(arguments['start_at'])
            end_at = datetime.fromisoformat(arguments['end_at'])
            return {'ok': True, 'events': query_events(start_at, end_at)}

        if name == 'create_event':
            if not set(arguments) <= {'title', 'description', 'start_at', 'end_at'}:
                raise ValueError('許可されていない予定項目が含まれています。')
            event = create_event_record(EventPayload(**arguments))
            return {
                'ok': True,
                'event': event,
                'summary': f'「{event["title"]}」を{event["start_at"]}に追加しました。',
            }

        if name == 'update_event':
            event_id = str(uuid.UUID(arguments.get('event_id', '')))
            changes = set(arguments) - {'event_id'}
            allowed = {'title', 'description', 'start_at', 'end_at'}
            if not changes or not changes <= allowed:
                raise ValueError('変更する項目を指定してください。')
            with closing(open_database()) as connection:
                row = connection.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail='予定が見つかりません。')
            updated_values = {
                'title': arguments.get('title', row['title']),
                'description': arguments.get('description', row['description']),
                'start_at': arguments.get('start_at', row['start_at']),
                'end_at': arguments.get('end_at', row['end_at']),
            }
            event = update_event_record(event_id, EventPayload(**updated_values))
            return {'ok': True, 'event': event, 'summary': f'「{event["title"]}」を変更しました。'}

        if name == 'delete_event':
            if set(arguments) != {'event_id'}:
                raise ValueError('削除する予定のIDを指定してください。')
            event_id = str(uuid.UUID(arguments['event_id']))
            title = delete_event_record(event_id)
            return {'ok': True, 'summary': f'「{title}」を削除しました。'}

        return {'ok': False, 'error': '許可されていない予定操作です。'}
    except HTTPException as error:
        return {'ok': False, 'error': error.detail}
    except (ValueError, TypeError, KeyError) as error:
        return {'ok': False, 'error': f'予定操作の引数が不正です: {error}'}
    except Exception as error:
        from settings import logger

        logger.exception('Schedule tool failed')
        return {'ok': False, 'error': f'予定操作に失敗しました: {type(error).__name__}'}
