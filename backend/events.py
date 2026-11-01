import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
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
    is_all_day: bool = False
    recurrence: list[str] = Field(default_factory=list, max_length=5)

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
        if self.is_all_day and any(
            (value.hour, value.minute, value.second) != (0, 0, 0)
            for value in (self.start_at, self.end_at)
        ):
            raise ValueError('終日予定の開始・終了は日付の午前0時にしてください。')
        allowed_prefixes = ('RRULE:', 'EXRULE:', 'RDATE:', 'EXDATE:')
        if any(not rule.startswith(allowed_prefixes) for rule in self.recurrence):
            raise ValueError('繰り返し規則の形式が正しくありません。')
        if self.recurrence:
            try:
                rrulestr('\n'.join(self.recurrence), dtstart=self.start_at, forceset=True)
            except (TypeError, ValueError) as error:
                raise ValueError('繰り返し規則を解析できません。') from error
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
                    'is_all_day': {'type': 'boolean'},
                    'recurrence': {'type': 'array', 'items': {'type': 'string'}},
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
                    'is_all_day': {'type': 'boolean'},
                    'recurrence': {'type': 'array', 'items': {'type': 'string'}},
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
        SELECT id, title, start_at, end_at, is_all_day, recurrence_json, recurrence_timezone, google_event_id FROM events
        WHERE deleted_at IS NULL AND (start_at < ? AND end_at > ? OR recurrence_json != '[]')
    '''
    parameters: list[str] = [end_at, start_at]
    if exclude_id is not None:
        query += ' AND id != ?'
        parameters.append(exclude_id)
    query += ' ORDER BY start_at'
    rows = connection.execute(query, parameters).fetchall()
    requested_start_local = datetime.fromisoformat(start_at)
    requested_end_local = datetime.fromisoformat(end_at)
    exception_rows = connection.execute(
        '''SELECT recurring_event_id, original_start_at FROM events
           WHERE recurring_event_id IS NOT NULL AND original_start_at IS NOT NULL''',
    ).fetchall()
    exceptions: dict[str, set[str]] = {}
    for row in exception_rows:
        exceptions.setdefault(row['recurring_event_id'], set()).add(row['original_start_at'])

    conflicts = []
    for row in rows:
        recurrence = json.loads(row['recurrence_json'] or '[]')
        if not recurrence:
            conflicts.append(row)
            continue
        if row['is_all_day']:
            event_start = datetime.fromisoformat(row['start_at']).replace(tzinfo=None)
            event_end = datetime.fromisoformat(row['end_at']).replace(tzinfo=None)
            requested_start = requested_start_local.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
            requested_end = requested_end_local.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
        else:
            event_timezone = ZoneInfo(row['recurrence_timezone'] or 'Asia/Tokyo')
            event_start = datetime.fromisoformat(row['start_at']).astimezone(event_timezone)
            event_end = datetime.fromisoformat(row['end_at']).astimezone(event_timezone)
            requested_start = requested_start_local.astimezone(event_timezone)
            requested_end = requested_end_local.astimezone(event_timezone)
        duration = event_end - event_start
        try:
            rule_set = rrulestr('\n'.join(recurrence), dtstart=event_start, forceset=True)
            occurrences = rule_set.between(requested_start - duration, requested_end, inc=True)
        except (TypeError, ValueError):
            if event_start < requested_end and event_end > requested_start:
                conflicts.append(row)
            continue
        ignored = exceptions.get(row['google_event_id'], set())
        if any(
            (occurrence.replace(tzinfo=LOCAL_TIMEZONE) if occurrence.tzinfo is None else occurrence.astimezone(LOCAL_TIMEZONE)).isoformat(timespec='seconds') not in ignored
            and occurrence < requested_end
            and occurrence + duration > requested_start
            for occurrence in occurrences
        ):
            conflicts.append(row)
    return conflicts


def reject_event_conflicts(conflicts: list[sqlite3.Row]) -> None:
    if conflicts:
        titles = '、'.join(row['title'] for row in conflicts[:3])
        raise HTTPException(status_code=409, detail=f'時間が重複する予定があります: {titles}')


def serialize_event(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    event = dict(row)
    event['is_all_day'] = bool(event['is_all_day'])
    event['recurrence'] = json.loads(event.pop('recurrence_json') or '[]')
    return event


def query_events(start: datetime | None = None, end: datetime | None = None) -> list[dict[str, Any]]:
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail='期間の開始と終了を両方指定してください。')

    with closing(open_database()) as connection:
        if start is None or end is None:
            rows = connection.execute(
                'SELECT * FROM events WHERE deleted_at IS NULL ORDER BY start_at',
            ).fetchall()
            return [serialize_event(row) for row in rows]
        else:
            range_start = event_datetime_iso(start)
            range_end = event_datetime_iso(end)
            if range_start >= range_end:
                raise HTTPException(status_code=422, detail='期間の終了は開始より後にしてください。')
            rows = connection.execute(
                '''
                SELECT * FROM events
                WHERE deleted_at IS NULL AND (start_at < ? AND end_at > ?
                    OR recurrence_json != '[]')
                ORDER BY start_at
                ''',
                (range_end, range_start),
            ).fetchall()
            exception_rows = connection.execute(
                '''SELECT recurring_event_id, original_start_at FROM events
                   WHERE recurring_event_id IS NOT NULL AND original_start_at IS NOT NULL''',
            ).fetchall()

    exception_starts: dict[str, set[str]] = {}
    for row in exception_rows:
        exception_starts.setdefault(row['recurring_event_id'], set()).add(row['original_start_at'])
    series_records = {
        row['google_event_id']: row
        for row in rows if row['google_event_id'] and json.loads(row['recurrence_json'] or '[]')
    }
    expanded: list[dict[str, Any]] = []
    window_start = datetime.fromisoformat(range_start)
    window_end = datetime.fromisoformat(range_end)
    for row in rows:
        event = serialize_event(row)
        recurrence = event['recurrence']
        if not recurrence:
            if row['recurring_event_id']:
                master = series_records.get(row['recurring_event_id'])
                if master is not None:
                    event['series_id'] = master['id']
                    event['occurrence_id'] = event['id']
                    event['id'] = master['id']
                    event['series_title'] = master['title']
                    event['series_description'] = master['description']
                    event['series_start_at'] = master['start_at']
                    event['series_end_at'] = master['end_at']
                    event['series_is_all_day'] = bool(master['is_all_day'])
                    event['series_recurrence'] = json.loads(master['recurrence_json'] or '[]')
                    event['recurrence'] = event['series_recurrence']
            expanded.append(event)
            continue
        if event['is_all_day']:
            event_timezone = LOCAL_TIMEZONE
            event_start = datetime.fromisoformat(event['start_at']).replace(tzinfo=None)
            event_end = datetime.fromisoformat(event['end_at']).replace(tzinfo=None)
            recurrence_window_start = window_start.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
            recurrence_window_end = window_end.astimezone(LOCAL_TIMEZONE).replace(tzinfo=None)
        else:
            event_timezone = ZoneInfo(row['recurrence_timezone'] or 'Asia/Tokyo')
            event_start = datetime.fromisoformat(event['start_at']).astimezone(event_timezone)
            event_end = datetime.fromisoformat(event['end_at']).astimezone(event_timezone)
            recurrence_window_start = window_start.astimezone(event_timezone)
            recurrence_window_end = window_end.astimezone(event_timezone)
        duration = event_end - event_start
        try:
            rule_set = rrulestr('\n'.join(recurrence), dtstart=event_start, forceset=True)
            occurrences = rule_set.between(
                recurrence_window_start - duration, recurrence_window_end, inc=True,
            )
        except (TypeError, ValueError):
            if event_start < recurrence_window_end and event_end > recurrence_window_start:
                event['series_id'] = event['id']
                expanded.append(event)
            continue
        exception_keys = exception_starts.get(row['google_event_id'], set())
        for occurrence in occurrences:
            local_occurrence = (
                occurrence.replace(tzinfo=LOCAL_TIMEZONE)
                if occurrence.tzinfo is None else occurrence.astimezone(LOCAL_TIMEZONE)
            )
            if local_occurrence.isoformat(timespec='seconds') in exception_keys:
                continue
            if occurrence >= recurrence_window_end or occurrence + duration <= recurrence_window_start:
                continue
            instance = dict(event)
            instance['series_id'] = event['id']
            instance['occurrence_id'] = f"{event['id']}@{occurrence.isoformat(timespec='seconds')}"
            instance['series_start_at'] = event['start_at']
            instance['series_end_at'] = event['end_at']
            instance['series_title'] = event['title']
            instance['series_description'] = event['description']
            instance['series_is_all_day'] = event['is_all_day']
            instance['series_recurrence'] = recurrence
            local_end = occurrence + duration
            if local_end.tzinfo is None:
                local_end = local_end.replace(tzinfo=LOCAL_TIMEZONE)
            else:
                local_end = local_end.astimezone(LOCAL_TIMEZONE)
            instance['start_at'] = local_occurrence.isoformat(timespec='seconds')
            instance['end_at'] = local_end.isoformat(timespec='seconds')
            expanded.append(instance)
    return sorted(expanded, key=lambda event: event['start_at'])


def create_event_record(payload: EventPayload) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    start_at = event_datetime_iso(payload.start_at)
    end_at = event_datetime_iso(payload.end_at)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        reject_event_conflicts(find_event_conflicts(connection, start_at, end_at))
        connection.execute(
            '''
            INSERT INTO events (id, title, description, start_at, end_at, is_all_day,
                recurrence_timezone, recurrence_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (event_id, payload.title, payload.description.strip(), start_at, end_at,
             int(payload.is_all_day), str(LOCAL_TIMEZONE), json.dumps(payload.recurrence), timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    return serialize_event(row)


def update_event_record(event_id: str, payload: EventPayload) -> dict[str, Any]:
    start_at = event_datetime_iso(payload.start_at)
    end_at = event_datetime_iso(payload.end_at)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        existing = connection.execute(
            'SELECT id, sync_status, recurrence_timezone FROM events WHERE id = ?', (event_id,),
        ).fetchone()
        if existing is None:
            raise HTTPException(status_code=404, detail='予定が見つかりません。')
        if existing['sync_status'] == 'conflict':
            raise HTTPException(status_code=409, detail='Google Calendarとの競合を先に解決してください。')
        reject_event_conflicts(find_event_conflicts(connection, start_at, end_at, event_id))
        connection.execute(
            '''
            UPDATE events SET title = ?, description = ?, start_at = ?, end_at = ?,
                is_all_day = ?, recurrence_timezone = ?, recurrence_json = ?, deleted_at = NULL, is_cancelled = 0,
                updated_at = ?, sync_status = CASE WHEN google_event_id IS NULL THEN 'local' ELSE 'dirty' END
            WHERE id = ?
            ''',
            (payload.title, payload.description.strip(), start_at, end_at, int(payload.is_all_day),
             existing['recurrence_timezone'] or str(LOCAL_TIMEZONE),
             json.dumps(payload.recurrence), timestamp, event_id),
        )
        connection.commit()
        row = connection.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
    return serialize_event(row)


def delete_event_record(event_id: str) -> str:
    with closing(open_database()) as connection:
        row = connection.execute(
            'SELECT title, google_event_id, sync_status FROM events WHERE id = ?', (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='予定が見つかりません。')
        if row['sync_status'] == 'conflict':
            raise HTTPException(status_code=409, detail='Google Calendarとの競合を先に解決してください。')
        if row['google_event_id']:
            timestamp = now_iso()
            connection.execute(
                "UPDATE events SET deleted_at = ?, updated_at = ?, sync_status = 'dirty' WHERE id = ?",
                (timestamp, timestamp, event_id),
            )
        else:
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
            if not set(arguments) <= {'title', 'description', 'start_at', 'end_at', 'is_all_day', 'recurrence'}:
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
            allowed = {'title', 'description', 'start_at', 'end_at', 'is_all_day', 'recurrence'}
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
                'is_all_day': arguments.get('is_all_day', bool(row['is_all_day'])),
                'recurrence': arguments.get('recurrence', json.loads(row['recurrence_json'] or '[]')),
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
