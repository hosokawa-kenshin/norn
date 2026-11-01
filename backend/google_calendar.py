import asyncio
import json
import os
import secrets
import threading
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import RedirectResponse
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from pydantic import BaseModel

from database import now_iso, open_database
from settings import (
    DATA_DIR,
    GOOGLE_CALENDAR_ID,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    GOOGLE_REDIRECT_URI,
    GOOGLE_SYNC_INTERVAL_SECONDS,
    GOOGLE_TOKEN_PATH,
    NORN_FRONTEND_URL,
)

router = APIRouter()
LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
SCOPES = [
    'https://www.googleapis.com/auth/calendar.events',
    'https://www.googleapis.com/auth/calendar.calendarlist.readonly',
]
SYNC_LOCK = threading.Lock()


class ConflictResolution(BaseModel):
    choice: str


class CalendarSelection(BaseModel):
    calendar_id: str


def _client_config() -> dict[str, Any]:
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(status_code=503, detail='Google OAuth client ID/secretが未設定です。')
    return {'installed': {
        'client_id': GOOGLE_CLIENT_ID,
        'client_secret': GOOGLE_CLIENT_SECRET,
        'auth_uri': 'https://accounts.google.com/o/oauth2/auth',
        'token_uri': 'https://oauth2.googleapis.com/token',
        'redirect_uris': [GOOGLE_REDIRECT_URI],
    }}


def _make_flow(state: str | None = None) -> Flow:
    return Flow.from_client_config(
        _client_config(), scopes=SCOPES, state=state, redirect_uri=GOOGLE_REDIRECT_URI,
    )


def _write_credentials(credentials: Credentials) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(GOOGLE_TOKEN_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as token_file:
        token_file.write(credentials.to_json())
    os.chmod(GOOGLE_TOKEN_PATH, 0o600)


def _get_credentials() -> Credentials:
    if not GOOGLE_TOKEN_PATH.exists():
        raise HTTPException(status_code=401, detail='Google Calendarが未接続です。')
    credentials = Credentials.from_authorized_user_file(str(GOOGLE_TOKEN_PATH), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        _write_credentials(credentials)
    if not credentials.valid:
        raise HTTPException(status_code=401, detail='Google認証の有効期限が切れています。再接続してください。')
    return credentials


def _calendar_service() -> Any:
    return build('calendar', 'v3', credentials=_get_credentials(), cache_discovery=False)


def selected_calendar_id() -> str:
    with closing(open_database()) as connection:
        row = connection.execute(
            'SELECT calendar_id FROM google_calendar_preferences WHERE id = 1',
        ).fetchone()
    return row['calendar_id'] if row else GOOGLE_CALENDAR_ID


def _oauth_authorize_url() -> str:
    flow = _make_flow()
    verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    flow.code_verifier = verifier
    url, _ = flow.authorization_url(
        state=state, access_type='offline', include_granted_scopes='true', prompt='consent',
    )
    with closing(open_database()) as connection:
        connection.execute('DELETE FROM google_calendar_oauth_states WHERE created_at < ?', (
            (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(),
        ))
        connection.execute(
            'INSERT INTO google_calendar_oauth_states (state, code_verifier, created_at) VALUES (?, ?, ?)',
            (state, verifier, now_iso()),
        )
        connection.commit()
    return url


def _oauth_callback(state: str, code: str) -> None:
    with closing(open_database()) as connection:
        row = connection.execute(
            'SELECT code_verifier, created_at FROM google_calendar_oauth_states WHERE state = ?',
            (state,),
        ).fetchone()
        connection.execute('DELETE FROM google_calendar_oauth_states WHERE state = ?', (state,))
        connection.commit()
    if row is None:
        raise HTTPException(status_code=400, detail='OAuth stateが無効か、期限切れです。')
    created_at = datetime.fromisoformat(row['created_at'])
    if datetime.now(timezone.utc) - created_at.astimezone(timezone.utc) > timedelta(minutes=15):
        raise HTTPException(status_code=400, detail='OAuth stateの有効期限が切れています。')
    flow = _make_flow(state)
    flow.code_verifier = row['code_verifier']
    flow.fetch_token(code=code)
    _write_credentials(flow.credentials)


def _to_local_datetime(value: dict[str, str], key: str) -> str:
    if 'date' in value:
        parsed_date = date.fromisoformat(value['date'])
        parsed = datetime.combine(parsed_date, time.min, LOCAL_TIMEZONE)
    else:
        parsed = datetime.fromisoformat(value[key].replace('Z', '+00:00'))
        if parsed.tzinfo is None and value.get('timeZone'):
            parsed = parsed.replace(tzinfo=ZoneInfo(value['timeZone']))
        parsed = parsed.astimezone(LOCAL_TIMEZONE)
    return parsed.isoformat(timespec='seconds')


def _canonical_local(row: Any) -> dict[str, Any]:
    return {
        'title': row['title'],
        'description': row['description'],
        'start_at': row['start_at'],
        'end_at': row['end_at'],
        'is_all_day': bool(row['is_all_day']),
        'recurrence_timezone': row['recurrence_timezone'] or 'Asia/Tokyo',
        'recurrence': json.loads(row['recurrence_json'] or '[]'),
        'recurring_event_id': row['recurring_event_id'],
        'original_start_at': row['original_start_at'],
    }


def _canonical_remote(item: dict[str, Any]) -> dict[str, Any]:
    start_data = item.get('start') or item.get('originalStartTime') or {}
    end_data = item.get('end') or start_data
    return {
        'title': item.get('summary', ''),
        'description': item.get('description', ''),
        'start_at': _to_local_datetime(start_data, 'dateTime') if start_data else '',
        'end_at': _to_local_datetime(end_data, 'dateTime') if end_data else '',
        'is_all_day': 'date' in start_data,
        'recurrence': item.get('recurrence', []),
        'recurring_event_id': item.get('recurringEventId'),
        'original_start_at': (
            _to_local_datetime(item['originalStartTime'], 'dateTime')
            if item.get('originalStartTime') else None
        ),
        'recurrence_timezone': start_data.get('timeZone', 'Asia/Tokyo'),
    }


def _snapshot(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _http_error_status(error: HttpError) -> int | None:
    return getattr(error, 'status_code', getattr(getattr(error, 'resp', None), 'status', None))


def _google_payload(row: Any) -> dict[str, Any]:
    event_timezone = ZoneInfo(row['recurrence_timezone'] or 'Asia/Tokyo')
    start = datetime.fromisoformat(row['start_at']).astimezone(event_timezone)
    end = datetime.fromisoformat(row['end_at']).astimezone(event_timezone)
    if row['is_all_day']:
        result: dict[str, Any] = {
            'start': {'date': start.date().isoformat()},
            'end': {'date': end.date().isoformat()},
        }
    else:
        result = {
            'start': {'dateTime': start.isoformat(), 'timeZone': str(event_timezone)},
            'end': {'dateTime': end.isoformat(), 'timeZone': str(event_timezone)},
        }
    result.update({'summary': row['title'], 'description': row['description']})
    result['recurrence'] = json.loads(row['recurrence_json'] or '[]')
    return result


def _record_conflict(connection: Any, row: Any, local: dict[str, Any], remote: dict[str, Any]) -> None:
    connection.execute(
        "UPDATE events SET sync_status = 'conflict', conflict_payload = ? WHERE id = ?",
        (_snapshot({'local': local, 'remote': remote, 'base': json.loads(row['sync_snapshot'])}), row['id']),
    )


def _apply_remote(connection: Any, item: dict[str, Any], calendar_id: str) -> None:
    remote = _canonical_remote(item)
    google_id = item['id']
    existing = connection.execute('SELECT * FROM events WHERE google_event_id = ?', (google_id,)).fetchone()
    timestamp = now_iso()
    cancelled = item.get('status') == 'cancelled'
    etag = item.get('etag')
    updated = item.get('updated')

    if existing is None:
        candidates = connection.execute(
            "SELECT * FROM events WHERE google_event_id IS NULL AND deleted_at IS NULL "
            "AND title = ? AND description = ? AND start_at = ? AND end_at = ? AND is_all_day = ?",
            (remote['title'], remote['description'], remote['start_at'], remote['end_at'],
             int(remote['is_all_day'])),
        ).fetchall()
        exact_matches = [row for row in candidates if _snapshot(_canonical_local(row)) == _snapshot(remote)]
        if len(exact_matches) == 1:
            match = exact_matches[0]
            connection.execute(
                '''UPDATE events SET google_event_id = ?, google_calendar_id = ?, google_etag = ?,
                    google_updated_at = ?, sync_snapshot = ?, sync_status = 'synced',
                    recurrence_timezone = ?, recurring_event_id = ?, original_start_at = ?, is_cancelled = ?,
                    deleted_at = ?, updated_at = ? WHERE id = ?''',
                (google_id, calendar_id, etag, updated, _snapshot(remote), remote['recurrence_timezone'],
                 remote['recurring_event_id'], remote['original_start_at'], int(cancelled),
                 timestamp if cancelled else None, timestamp, match['id']),
            )
            return
        event_id = str(uuid.uuid4())
        connection.execute(
            '''INSERT INTO events (
                id, title, description, start_at, end_at, is_all_day, recurrence_timezone, recurrence_json,
                google_event_id, google_calendar_id, google_etag, google_updated_at,
                sync_snapshot, sync_status, recurring_event_id, original_start_at,
                is_cancelled, deleted_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synced', ?, ?, ?, ?, ?, ?)''',
            (event_id, remote['title'], remote['description'], remote['start_at'], remote['end_at'],
             int(remote['is_all_day']), remote['recurrence_timezone'], json.dumps(remote['recurrence']), google_id, calendar_id,
             etag, updated, _snapshot(remote), remote['recurring_event_id'],
             remote['original_start_at'], int(cancelled), timestamp if cancelled else None,
             timestamp, timestamp),
        )
        return

    local = _canonical_local(existing)
    base = json.loads(existing['sync_snapshot']) if existing['sync_snapshot'] else None
    local_deleted = existing['deleted_at'] is not None
    local_changed = local_deleted or (base is not None and _snapshot(local) != _snapshot(base))
    remote_changed = base is not None and _snapshot(remote) != _snapshot(base)
    if local_changed and remote_changed and _snapshot(local) != _snapshot(remote):
        _record_conflict(connection, existing, {**local, 'deleted': local_deleted}, remote)
        return
    if local_changed and not remote_changed:
        return
    connection.execute(
        '''UPDATE events SET title = ?, description = ?, start_at = ?, end_at = ?,
            is_all_day = ?, recurrence_timezone = ?, recurrence_json = ?, google_calendar_id = ?, google_etag = ?,
            google_updated_at = ?, sync_snapshot = ?, sync_status = 'synced',
            conflict_payload = NULL, recurring_event_id = ?, original_start_at = ?,
            is_cancelled = ?, deleted_at = ?, updated_at = ? WHERE id = ?''',
        (remote['title'], remote['description'], remote['start_at'], remote['end_at'],
         int(remote['is_all_day']), remote['recurrence_timezone'], json.dumps(remote['recurrence']), calendar_id, etag,
         updated, _snapshot(remote), remote['recurring_event_id'], remote['original_start_at'],
         int(cancelled), timestamp if cancelled else None, timestamp, existing['id']),
    )


def _handle_remote_item(connection: Any, item: dict[str, Any], calendar_id: str) -> None:
    existing = connection.execute('SELECT * FROM events WHERE google_event_id = ?', (item['id'],)).fetchone()
    if item.get('status') == 'cancelled' and existing is None:
        if item.get('recurringEventId') and item.get('originalStartTime'):
            _apply_remote(connection, item, calendar_id)
        return
    if item.get('status') == 'cancelled' and existing is not None:
        local = _canonical_local(existing)
        base = json.loads(existing['sync_snapshot']) if existing['sync_snapshot'] else None
        if base is not None and _snapshot(local) != _snapshot(base):
            _record_conflict(connection, existing, local, {'deleted': True, **_canonical_remote(item)})
            return
        connection.execute(
            "UPDATE events SET is_cancelled = 1, deleted_at = ?, sync_status = 'synced', updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), existing['id']),
        )
        return
    _apply_remote(connection, item, calendar_id)


def _fetch_changes(
    service: Any, connection: Any, calendar_id: str, reconcile_missing: bool = False,
) -> None:
    state = connection.execute(
        'SELECT sync_token FROM google_calendar_sync_state WHERE calendar_id = ?', (calendar_id,),
    ).fetchone()
    sync_token = state['sync_token'] if state else None
    page_token = None
    next_sync_token = None
    remote_ids: set[str] = set()
    while True:
        options: dict[str, Any] = {
            'calendarId': calendar_id,
            'maxResults': 2500,
            'showDeleted': True,
            'singleEvents': False,
        }
        if sync_token:
            options['syncToken'] = sync_token
        if page_token:
            options['pageToken'] = page_token
        try:
            result = service.events().list(**options).execute()
        except HttpError as error:
            if _http_error_status(error) == 410 and sync_token:
                connection.execute(
                    'UPDATE google_calendar_sync_state SET sync_token = NULL, updated_at = ? WHERE calendar_id = ?',
                    (now_iso(), calendar_id),
                )
                connection.commit()
                _fetch_changes(service, connection, calendar_id, reconcile_missing=True)
                return
            raise
        for item in result.get('items', []):
            remote_ids.add(item['id'])
            _handle_remote_item(connection, item, calendar_id)
        page_token = result.get('nextPageToken')
        if not page_token:
            next_sync_token = result.get('nextSyncToken')
            break
    if reconcile_missing:
        mapped_rows = connection.execute(
            'SELECT * FROM events WHERE google_calendar_id = ? AND google_event_id IS NOT NULL',
            (calendar_id,),
        ).fetchall()
        for row in mapped_rows:
            if row['google_event_id'] in remote_ids:
                continue
            local = _canonical_local(row)
            base = json.loads(row['sync_snapshot']) if row['sync_snapshot'] else None
            if row['deleted_at']:
                connection.execute('DELETE FROM events WHERE id = ?', (row['id'],))
            elif base is not None and _snapshot(local) != _snapshot(base):
                _record_conflict(connection, row, {**local, 'deleted': bool(row['deleted_at'])}, {'deleted': True})
            else:
                connection.execute(
                    "UPDATE events SET is_cancelled = 1, deleted_at = ?, sync_status = 'synced', updated_at = ? WHERE id = ?",
                    (now_iso(), now_iso(), row['id']),
                )
    connection.execute(
        '''INSERT INTO google_calendar_sync_state (calendar_id, sync_token, last_sync_at, last_error, updated_at)
           VALUES (?, ?, ?, NULL, ?) ON CONFLICT(calendar_id) DO UPDATE SET
           sync_token = excluded.sync_token, last_sync_at = excluded.last_sync_at,
           last_error = NULL, updated_at = excluded.updated_at''',
        (calendar_id, next_sync_token, now_iso(), now_iso()),
    )
    connection.commit()


def _push_local_changes(service: Any, connection: Any, calendar_id: str) -> None:
    rows = connection.execute(
        "SELECT * FROM events WHERE sync_status != 'conflict' AND (google_event_id IS NULL OR "
        "(google_calendar_id = ? AND (sync_status = 'dirty' OR (deleted_at IS NOT NULL AND is_cancelled = 0))))",
        (calendar_id,),
    ).fetchall()
    for row in rows:
        try:
            if row['deleted_at']:
                if row['google_event_id']:
                    service.events().delete(calendarId=calendar_id, eventId=row['google_event_id']).execute()
                connection.execute('DELETE FROM events WHERE id = ?', (row['id'],))
                continue
            payload = _google_payload(row)
            if row['google_event_id']:
                item = service.events().update(
                    calendarId=calendar_id, eventId=row['google_event_id'], body=payload,
                ).execute()
            else:
                item = service.events().insert(calendarId=calendar_id, body=payload).execute()
            remote = _canonical_remote(item)
            connection.execute(
                '''UPDATE events SET google_event_id = ?, google_calendar_id = ?, google_etag = ?,
                    google_updated_at = ?, sync_snapshot = ?, sync_status = 'synced',
                    conflict_payload = NULL, updated_at = ? WHERE id = ?''',
                (item['id'], calendar_id, item.get('etag'), item.get('updated'),
                 _snapshot(remote), now_iso(), row['id']),
            )
        except HttpError as error:
            if _http_error_status(error) in (404, 410) and row['deleted_at']:
                connection.execute('DELETE FROM events WHERE id = ?', (row['id'],))
                continue
            raise
    connection.commit()


def sync_calendar(service: Any | None = None, calendar_id: str | None = None) -> dict[str, Any]:
    if not SYNC_LOCK.acquire(blocking=False):
        return {'ok': False, 'message': '同期はすでに実行中です。'}
    try:
        service = service or _calendar_service()
        calendar_id = calendar_id or selected_calendar_id()
        with closing(open_database()) as connection:
            try:
                existing_conflicts = connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE sync_status = 'conflict'",
                ).fetchone()['count']
                if existing_conflicts:
                    return {'ok': False, 'paused': True, 'conflicts': existing_conflicts}
                _fetch_changes(service, connection, calendar_id)
                conflicts = connection.execute(
                    "SELECT COUNT(*) AS count FROM events WHERE sync_status = 'conflict'",
                ).fetchone()['count']
                if not conflicts:
                    _push_local_changes(service, connection, calendar_id)
                connection.execute(
                    'UPDATE google_calendar_sync_state SET last_error = NULL, updated_at = ? WHERE calendar_id = ?',
                    (now_iso(), calendar_id),
                )
                connection.commit()
            except Exception as error:
                connection.execute(
                    '''INSERT INTO google_calendar_sync_state (calendar_id, last_error, updated_at)
                       VALUES (?, ?, ?) ON CONFLICT(calendar_id) DO UPDATE SET
                       last_error = excluded.last_error, updated_at = excluded.updated_at''',
                    (calendar_id, str(error)[:500], now_iso()),
                )
                connection.commit()
                raise
        return {'ok': True, 'conflicts': conflicts, 'synced_at': now_iso()}
    finally:
        SYNC_LOCK.release()


def google_calendar_status() -> dict[str, Any]:
    calendar_id = selected_calendar_id()
    with closing(open_database()) as connection:
        state = connection.execute(
            'SELECT last_sync_at, last_error FROM google_calendar_sync_state WHERE calendar_id = ?',
            (calendar_id,),
        ).fetchone()
        conflicts = connection.execute(
            "SELECT id, title, conflict_payload FROM events WHERE sync_status = 'conflict' ORDER BY updated_at",
        ).fetchall()
    connected = GOOGLE_TOKEN_PATH.exists()
    return {
        'configured': bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET),
        'connected': connected,
        'calendar_id': calendar_id,
        'sync_interval_seconds': GOOGLE_SYNC_INTERVAL_SECONDS,
        'last_sync_at': state['last_sync_at'] if state else None,
        'last_error': state['last_error'] if state else None,
        'conflicts': [
            {'event_id': row['id'], 'title': row['title'], **json.loads(row['conflict_payload'])}
            for row in conflicts
        ],
    }


def list_google_calendars(service: Any | None = None) -> dict[str, Any]:
    service = service or _calendar_service()
    calendars = []
    page_token = None
    while True:
        options: dict[str, Any] = {'minAccessRole': 'writer', 'maxResults': 250}
        if page_token:
            options['pageToken'] = page_token
        result = service.calendarList().list(**options).execute()
        for item in result.get('items', []):
            if item.get('accessRole') not in {'owner', 'writer'}:
                continue
            calendars.append({
                'id': 'primary' if item.get('primary') else item['id'],
                'name': item.get('summary', item['id']),
                'primary': bool(item.get('primary')),
                'time_zone': item.get('timeZone', 'Asia/Tokyo'),
            })
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    return {'selected_calendar_id': selected_calendar_id(), 'calendars': calendars}


def select_google_calendar(calendar_id: str, service: Any | None = None) -> dict[str, Any]:
    service = service or _calendar_service()
    item = service.calendarList().get(calendarId=calendar_id).execute()
    if item.get('accessRole') not in {'owner', 'writer'}:
        raise HTTPException(status_code=403, detail='このカレンダーには予定を書き込めません。')
    selected_id = 'primary' if item.get('primary') else item['id']
    with closing(open_database()) as connection:
        connection.execute(
            '''INSERT INTO google_calendar_preferences (id, calendar_id, updated_at)
               VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET
               calendar_id = excluded.calendar_id, updated_at = excluded.updated_at''',
            (selected_id, now_iso()),
        )
        connection.execute(
            'INSERT OR IGNORE INTO google_calendar_sync_state (calendar_id, updated_at) VALUES (?, ?)',
            (selected_id, now_iso()),
        )
        connection.commit()
    sync_result = sync_calendar(service, selected_id)
    return {'calendar_id': selected_id, 'sync': sync_result}


def resolve_conflict(event_id: str, choice: str) -> dict[str, Any]:
    if choice not in {'local', 'google'}:
        raise HTTPException(status_code=422, detail='choiceはlocalまたはgoogleを指定してください。')
    with closing(open_database()) as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE id = ? AND sync_status = 'conflict'", (event_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='競合する予定が見つかりません。')
        conflict = json.loads(row['conflict_payload'])
        if choice == 'google':
            remote = conflict['remote']
            if remote.get('deleted'):
                connection.execute(
                    "UPDATE events SET deleted_at = ?, sync_status = 'synced', conflict_payload = NULL WHERE id = ?",
                    (now_iso(), event_id),
                )
            else:
                connection.execute(
                    '''UPDATE events SET title = ?, description = ?, start_at = ?, end_at = ?,
                                is_all_day = ?, recurrence_timezone = ?, recurrence_json = ?, recurring_event_id = ?, original_start_at = ?,
                        sync_snapshot = ?, sync_status = 'synced', conflict_payload = NULL, updated_at = ?
                       WHERE id = ?''',
                    (remote['title'], remote['description'], remote['start_at'], remote['end_at'],
                            int(remote['is_all_day']), remote['recurrence_timezone'], json.dumps(remote['recurrence']),
                     remote['recurring_event_id'], remote['original_start_at'],
                     _snapshot(remote), now_iso(), event_id),
                )
        else:
            if conflict['local'].get('deleted'):
                connection.execute(
                    "UPDATE events SET deleted_at = ?, sync_status = 'dirty', conflict_payload = NULL WHERE id = ?",
                    (now_iso(), event_id),
                )
            elif conflict['remote'].get('deleted'):
                connection.execute(
                    "UPDATE events SET google_event_id = NULL, google_etag = NULL, sync_snapshot = NULL, "
                    "sync_status = 'dirty', conflict_payload = NULL WHERE id = ?",
                    (event_id,),
                )
            else:
                connection.execute(
                    "UPDATE events SET sync_status = 'dirty', conflict_payload = NULL WHERE id = ?",
                    (event_id,),
                )
        connection.commit()
    if choice == 'local':
        sync_calendar()
    return {'ok': True, 'choice': choice}


@router.get('/api/google-calendar/status')
def get_google_calendar_status() -> dict[str, Any]:
    return google_calendar_status()


@router.get('/api/google-calendar/calendars')
def get_google_calendars() -> dict[str, Any]:
    try:
        return list_google_calendars()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(
            status_code=502,
            detail=f'カレンダー一覧を取得できません: {type(error).__name__}。権限を更新して再接続してください。',
        ) from error


@router.put('/api/google-calendar/calendar')
def put_google_calendar(payload: CalendarSelection) -> dict[str, Any]:
    try:
        return select_google_calendar(payload.calendar_id)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f'同期先を変更できません: {type(error).__name__}') from error


@router.post('/api/google-calendar/authorize')
def authorize_google_calendar() -> dict[str, str]:
    try:
        return {'authorization_url': _oauth_authorize_url()}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=503, detail=f'OAuth URLを作成できません: {type(error).__name__}') from error


@router.get('/')
def google_calendar_callback(state: str = Query(...), code: str = Query(...)) -> RedirectResponse:
    try:
        _oauth_callback(state, code)
        return RedirectResponse(f'{NORN_FRONTEND_URL}/?googleCalendar=connected', status_code=303)
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=400, detail=f'Google認証に失敗しました: {type(error).__name__}') from error


@router.post('/api/google-calendar/sync')
def run_google_calendar_sync() -> dict[str, Any]:
    try:
        return sync_calendar()
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=502, detail=f'Google Calendar同期に失敗しました: {type(error).__name__}') from error


@router.post('/api/google-calendar/conflicts/{event_id}/resolve')
def resolve_google_calendar_conflict(event_id: str, payload: ConflictResolution) -> dict[str, Any]:
    return resolve_conflict(event_id, payload.choice)


async def sync_worker() -> None:
    while True:
        await asyncio.sleep(GOOGLE_SYNC_INTERVAL_SECONDS)
        if GOOGLE_TOKEN_PATH.exists():
            try:
                await asyncio.to_thread(sync_calendar)
            except Exception:
                from settings import logger

                logger.exception('Google Calendar sync failed')