import uuid
from contextlib import closing
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

import execution
from database import open_database
from execution import ExecutionLogPayload, LOCAL_TIMEZONE

STATUS_LABELS = {'completed': '完了', 'partial': '一部実施', 'skipped': '未実施', 'cancelled': '中止'}
LOOKBACK_DAYS = 14

EXECUTION_TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_unrecorded_executions',
        'description': '実績がまだ記録されていないルーティンの実施予定と、過去の作業枠を日付・時刻つきで取得する。ユーザーが「終わった」「できなかった」と報告したとき、どの日の分か判断するために使う。',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'record_execution',
        'description': (
            'ユーザーが報告した実施結果を実績として記録する（予定やタスクの完了扱いにはしない）。'
            '対象の決め方: 発言に日付（昨日・11/2等）があればdateへ指定する。なければdateを省略して、未記録の候補が1件ならそれに記録し、複数なら記録せず確認が返るのでユーザーに日付を聞き返す。'
            'occurrence_id（実施予定）、routine_id（ルーティン）、task_id（タスク）のいずれかを指定する。記録済みの予定を直すときだけcorrection=trueにする。'
            '記録後は、どの日のどの予定をどう記録したかを必ず返信で伝える。'
        ),
        'parameters': {'type': 'object', 'properties': {
            'occurrence_id': {'type': 'string'},
            'routine_id': {'type': 'string'},
            'task_id': {'type': 'string'},
            'date': {'type': 'string', 'format': 'date', 'description': 'ユーザーが日付を示したときだけ。'},
            'status': {'type': 'string', 'enum': ['completed', 'partial', 'skipped', 'cancelled']},
            'actual_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440, 'description': 'ユーザーが言った実際の作業時間。'},
            'note': {'type': 'string'},
            'resume_point': {'type': 'string', 'description': '次回の再開地点。'},
            'interruption_reason': {'type': 'string'},
            'progress_percent': {'type': 'integer', 'minimum': 0, 'maximum': 100},
            'correction': {'type': 'boolean'},
        }, 'required': ['status'], 'additionalProperties': False},
    }},
]


def _label(start: str) -> str:
    moment = datetime.fromisoformat(start)
    return f'{moment.month}/{moment.day} {moment:%H:%M}'


def _now() -> datetime:
    return datetime.now(LOCAL_TIMEZONE)


def _candidate_occurrences(connection: Any, now: datetime, routine_id: str | None = None) -> list[dict[str, Any]]:
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    query = '''SELECT o.id, o.routine_id, o.scheduled_start, o.scheduled_end, o.status, r.title
               FROM routine_occurrences o JOIN routines r ON r.id = o.routine_id
               WHERE o.status = 'scheduled' AND datetime(o.scheduled_start) <= datetime(?) AND datetime(o.scheduled_start) >= datetime(?)'''
    parameters: list[Any] = [now.isoformat(), since]
    if routine_id:
        query += ' AND o.routine_id = ?'
        parameters.append(routine_id)
    rows = connection.execute(query + ' ORDER BY o.scheduled_start', parameters).fetchall()
    return [{**dict(row), 'label': f"{row['title']}（{_label(row['scheduled_start'])}）"} for row in rows]


def _past_allocations(connection: Any, now: datetime, task_id: str | None = None) -> list[dict[str, Any]]:
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    query = '''SELECT a.task_id, a.start_at, a.end_at, t.title FROM task_allocations a JOIN tasks t ON t.id = a.task_id
               WHERE datetime(a.end_at) <= datetime(?) AND datetime(a.start_at) >= datetime(?)'''
    parameters: list[Any] = [now.isoformat(), since]
    if task_id:
        query += ' AND a.task_id = ?'
        parameters.append(task_id)
    rows = connection.execute(query + ' ORDER BY a.start_at', parameters).fetchall()
    result = []
    for row in rows:
        recorded = connection.execute(
            '''SELECT 1 FROM execution_logs WHERE task_id = ?
               AND substr(COALESCE(started_at, ended_at, created_at), 1, 10) = substr(?, 1, 10)''',
            (row['task_id'], row['start_at']),
        ).fetchone()
        if not recorded:
            result.append({**dict(row), 'label': f"{row['title']}（{_label(row['start_at'])}）"})
    return result


def list_unrecorded() -> dict[str, Any]:
    now = _now()
    with closing(open_database()) as connection:
        occurrences = _candidate_occurrences(connection, now)
        allocations = _past_allocations(connection, now)
    return {
        'ok': True, 'today': now.date().isoformat(),
        'routine_occurrences': [
            {'occurrence_id': item['id'], 'routine_id': item['routine_id'], 'title': item['title'],
             'start': item['scheduled_start'], 'end': item['scheduled_end'], 'label': item['label']}
            for item in occurrences
        ],
        'task_slots': [
            {'task_id': item['task_id'], 'title': item['title'], 'start': item['start_at'], 'end': item['end_at'], 'label': item['label']}
            for item in allocations
        ],
    }


def _needs_input(message: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    return {'ok': True, 'state': 'needs_input', 'message': message, 'candidates': [item['label'] for item in candidates]}


def _uuid(value: Any, message: str) -> str | None:
    if value in (None, ''):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as error:
        raise ValueError(message) from error


def record_execution(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        return _record_execution(arguments)
    except HTTPException as error:
        return {'ok': False, 'error': str(error.detail)}
    except (ValueError, TypeError) as error:
        return {'ok': False, 'error': f'ツール引数が不正です: {error}'}


def _record_execution(arguments: dict[str, Any]) -> dict[str, Any]:
    now = _now()
    status = arguments.get('status')
    if status not in STATUS_LABELS:
        raise ValueError('statusはcompleted/partial/skipped/cancelledのいずれかです。')
    occurrence_id = _uuid(arguments.get('occurrence_id'), '実施予定IDが不正です。')
    routine_id = _uuid(arguments.get('routine_id'), 'ルーティンIDが不正です。')
    task_id = _uuid(arguments.get('task_id'), 'タスクIDが不正です。')
    day = date.fromisoformat(arguments['date']) if arguments.get('date') else None
    correction = bool(arguments.get('correction'))
    minutes = arguments.get('actual_minutes')
    if not (occurrence_id or routine_id or task_id):
        raise ValueError('occurrence_id・routine_id・task_idのいずれかを指定してください。')

    extra = {
        'status': status, 'note': arguments.get('note', ''), 'resume_point': arguments.get('resume_point', ''),
        'interruption_reason': arguments.get('interruption_reason', ''),
        'progress_percent': arguments.get('progress_percent'), 'recorded_source': 'norn',
    }
    timed = status in {'completed', 'partial'}

    with closing(open_database()) as connection:
        if occurrence_id or routine_id:
            if occurrence_id:
                row = connection.execute(
                    '''SELECT o.id, o.routine_id, o.scheduled_start, o.scheduled_end, o.status, r.title FROM routine_occurrences o
                       JOIN routines r ON r.id = o.routine_id WHERE o.id = ?''', (occurrence_id,),
                ).fetchone()
                if row is None:
                    raise HTTPException(status_code=404, detail='ルーティン実施予定が見つかりません。')
                chosen = {**dict(row), 'label': f"{row['title']}（{_label(row['scheduled_start'])}）"}
            elif day is not None:
                rows = [dict(row) for row in connection.execute(
                    '''SELECT o.id, o.routine_id, o.scheduled_start, o.scheduled_end, o.status, r.title FROM routine_occurrences o
                       JOIN routines r ON r.id = o.routine_id
                       WHERE o.routine_id = ? AND substr(o.scheduled_start, 1, 10) = ? AND o.status != 'cancelled' ORDER BY o.scheduled_start''',
                    (routine_id, day.isoformat()),
                ).fetchall()]
                if not rows:
                    raise HTTPException(status_code=404, detail=f'{day.month}/{day.day}にそのルーティンの実施予定はありません。')
                if len(rows) > 1:
                    return _needs_input(f'{day.month}/{day.day}に実施予定が複数あります。どの時刻の分ですか？', [{'label': _label(r['scheduled_start'])} for r in rows])
                chosen = {**rows[0], 'label': f"{rows[0]['title']}（{_label(rows[0]['scheduled_start'])}）"}
            else:
                candidates = _candidate_occurrences(connection, now, routine_id)
                if not candidates:
                    raise HTTPException(status_code=404, detail='未記録の実施予定が見つかりません。')
                if len(candidates) > 1:
                    return _needs_input('未記録の実施予定が複数あります。どの日の分ですか？', candidates)
                chosen = candidates[0]
            if chosen['status'] != 'scheduled' and not correction:
                raise HTTPException(status_code=409, detail=f"{chosen['label']}は{STATUS_LABELS.get(chosen['status'], chosen['status'])}で記録済みです。直す場合はcorrectionを指定してください。")
            started = ended = None
            if timed:
                started = datetime.fromisoformat(chosen['scheduled_start'])
                ended = started + timedelta(minutes=minutes) if minutes else datetime.fromisoformat(chosen['scheduled_end'])
            log = execution.create_execution_log(ExecutionLogPayload(
                occurrence_id=chosen['id'], started_at=started, ended_at=ended,
                actual_minutes=None if timed else 0, **extra,
            ))
            what = chosen['label']
        else:
            if day is not None:
                slots = [slot for slot in _logged_slots(connection, now, task_id) if slot['start_at'][:10] == day.isoformat()]
                if not slots and not correction:
                    raise HTTPException(status_code=404, detail=f'{day.month}/{day.day}にそのタスクの作業枠はありません。')
                slot = slots[0] if slots else None
            else:
                slots = _past_allocations(connection, now, task_id)
                if len(slots) > 1 and not minutes:
                    return _needs_input('未記録の作業枠が複数あります。どの日の分ですか？', slots)
                slot = slots[0] if len(slots) == 1 else None
            title = connection.execute('SELECT title FROM tasks WHERE id = ?', (task_id,)).fetchone()
            if title is None:
                raise HTTPException(status_code=404, detail='関連するタスクが見つかりません。')
            started = ended = None
            if slot is not None:
                started = datetime.fromisoformat(slot['start_at'])
                ended = started + timedelta(minutes=minutes) if minutes else datetime.fromisoformat(slot['end_at'])
            elif timed:
                if not minutes:
                    return {'ok': True, 'state': 'needs_input', 'message': '対象の作業枠が見つかりません。何分くらい作業しましたか？', 'candidates': []}
                ended, started = now, now - timedelta(minutes=minutes)
            log = execution.create_execution_log(ExecutionLogPayload(
                task_id=task_id, started_at=started, ended_at=ended,
                actual_minutes=None if timed else 0, **extra,
            ))
            what = f"{title['title']}（{_label(started.isoformat())}）" if started else title['title']
    spent = f"、{log['actual_minutes']}分" if timed and log.get('actual_minutes') is not None else ''
    follow = f"。次回は「{extra['resume_point']}」から" if extra['resume_point'] else ''
    return {
        'ok': True, 'execution_log': log,
        'summary': f"{what}を{STATUS_LABELS[status]}{spent}で実績に記録しました{follow}。",
    }


def _logged_slots(connection: Any, now: datetime, task_id: str | None) -> list[dict[str, Any]]:
    since = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    query = '''SELECT a.task_id, a.start_at, a.end_at FROM task_allocations a
               WHERE datetime(a.end_at) <= datetime(?) AND datetime(a.start_at) >= datetime(?)'''
    parameters: list[Any] = [now.isoformat(), since]
    if task_id:
        query += ' AND a.task_id = ?'
        parameters.append(task_id)
    return [dict(row) for row in connection.execute(query + ' ORDER BY a.start_at', parameters).fetchall()]


def execute_execution_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == 'list_unrecorded_executions':
        return list_unrecorded()
    if name == 'record_execution':
        return record_execution(arguments)
    return {'ok': False, 'error': f'未対応のツールです: {name}'}
