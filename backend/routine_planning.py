import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import tasks
from database import database_transaction, now_iso, open_database
from events import EventPayload, create_event_record, event_datetime_iso, query_events
from routines import RoutineDefinitionPayload, RoutinePlanPayload, create_routine, create_routine_plan

router = APIRouter()

ACTIVE_STATES = ('scheduled', 'completed', 'partial')
PERIOD_WINDOWS = [
    (('morning', '午前', '朝'), (0, 12)),
    (('afternoon', '午後', '昼'), (12, 18)),
    (('evening', 'night', '夕', '夜'), (18, 24)),
]


class WeeklyPlanPayload(BaseModel):
    week_start: date | None = None
    target_count: int | None = Field(default=None, ge=0, le=31)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    adjustment_reason: str = Field(default='', max_length=2000)
    generate: bool = True


def _uuid(value: str, message: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise HTTPException(status_code=422, detail=message) from error


def _preferred_window(preferred_period: str | None) -> tuple[int, int] | None:
    text = (preferred_period or '').lower()
    for keywords, window in PERIOD_WINDOWS:
        if any(keyword in text for keyword in keywords):
            return window
    return None


def _busy_periods(connection: Any, start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    events = [event for event in query_events(start, end) if not event.get('is_cancelled')]
    allocations = connection.execute(
        'SELECT start_at, end_at FROM task_allocations WHERE start_at < ? AND end_at > ?',
        (event_datetime_iso(end), event_datetime_iso(start)),
    ).fetchall()
    return [
        (datetime.fromisoformat(row['start_at']), datetime.fromisoformat(row['end_at']))
        for row in [*events, *allocations]
    ]


def _find_slot(
    day: date, minutes: int, busy: list[tuple[datetime, datetime]], now: datetime,
    window: tuple[int, int] | None,
) -> tuple[datetime, datetime] | None:
    day_start = datetime.combine(day, time.min, tasks.LOCAL_TIMEZONE)
    day_end = datetime.combine(day, time(23, 59, 59), tasks.LOCAL_TIMEZONE)
    break_gap = timedelta(minutes=tasks.adaptive_break_minutes(None, minutes))
    padded = [(start - break_gap, end + break_gap) for start, end in busy]
    periods = tasks.available_work_periods(max(day_start, now), day_end, sorted(padded))
    duration = timedelta(minutes=minutes)
    for window_to_try in ([window, None] if window else [None]):
        for period_start, period_end in periods:
            if window_to_try:
                period_start = max(period_start, datetime.combine(day, time(window_to_try[0]), tasks.LOCAL_TIMEZONE))
                if window_to_try[1] < 24:
                    period_end = min(period_end, datetime.combine(day, time(window_to_try[1]), tasks.LOCAL_TIMEZONE))
            candidate = tasks.ceil_to_quarter_hour(period_start)
            if candidate + duration <= period_end:
                return candidate, candidate + duration
    return None


def _insert_occurrence(
    connection: Any, plan: dict[str, Any], routine: dict[str, Any], start: datetime, end: datetime,
) -> dict[str, Any]:
    occurrence_id = str(uuid.uuid4())
    event = create_event_record(EventPayload(
        title=routine['title'], description=routine['description'], start_at=start, end_at=end,
    ))
    connection.execute(
        '''UPDATE events SET kind='routine_block', source='norn', is_fixed=0, routine_occurrence_id=?
           WHERE id=?''',
        (occurrence_id, event['id']),
    )
    timestamp = now_iso()
    connection.execute(
        '''INSERT INTO routine_occurrences (
            id, plan_id, routine_id, schedule_id, scheduled_start, scheduled_end, status, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)''',
        (occurrence_id, plan['id'], routine['id'], event['id'],
         event_datetime_iso(start), event_datetime_iso(end), timestamp, timestamp),
    )
    return {'id': occurrence_id, 'schedule_id': event['id'], 'start_at': event_datetime_iso(start),
            'end_at': event_datetime_iso(end)}


def _place_occurrences(
    connection: Any, plan: dict[str, Any], routine: dict[str, Any], count: int, now: datetime,
) -> list[dict[str, Any]]:
    if count <= 0:
        return []
    first_day = max(date.fromisoformat(plan['period_start']), now.date())
    last_day = date.fromisoformat(plan['period_end'])
    taken = {
        row['day'] for row in connection.execute(
            '''SELECT substr(scheduled_start, 1, 10) AS day FROM routine_occurrences
               WHERE routine_id = ? AND status IN ('scheduled', 'completed', 'partial')''',
            (routine['id'],),
        ).fetchall()
    }
    days = []
    cursor = first_day
    while cursor <= last_day:
        if tasks.is_planning_workday(cursor) and cursor.isoformat() not in taken:
            days.append(cursor)
        cursor += timedelta(days=1)
    if not days:
        return []
    window_start = datetime.combine(days[0], time.min, tasks.LOCAL_TIMEZONE)
    window_end = datetime.combine(days[-1], time(23, 59, 59), tasks.LOCAL_TIMEZONE)
    busy = _busy_periods(connection, window_start, window_end)
    window = _preferred_window(routine['preferred_period'])
    count = min(count, len(days))
    used: set[int] = set()
    created = []
    for index in range(count):
        ideal = int((index + 0.5) * len(days) / count)
        for position in sorted((p for p in range(len(days)) if p not in used), key=lambda p: (abs(p - ideal), p)):
            slot = _find_slot(days[position], plan['duration_minutes'], busy, now, window)
            if slot is None:
                continue
            used.add(position)
            busy.append(slot)
            created.append(_insert_occurrence(connection, plan, routine, *slot))
            break
    return sorted(created, key=lambda item: item['start_at'])


def _load_plan(connection: Any, plan_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    plan_row = connection.execute('SELECT * FROM routine_plans WHERE id = ?', (plan_id,)).fetchone()
    if plan_row is None:
        raise HTTPException(status_code=404, detail='ルーティン計画が見つかりません。')
    plan = dict(plan_row)
    if plan['status'] not in ('planned', 'active'):
        raise HTTPException(status_code=409, detail='完了または取り消し済みの計画です。')
    routine_row = connection.execute(
        'SELECT * FROM routines WHERE id = ? AND is_active = 1', (plan['routine_id'],),
    ).fetchone()
    if routine_row is None:
        raise HTTPException(status_code=409, detail='ルーティンが無効です。')
    return plan, dict(routine_row)


def _count_states(connection: Any, plan_id: str, now: datetime) -> dict[str, int]:
    counts = {'achieved': 0, 'future': 0}
    for row in connection.execute(
        'SELECT status, scheduled_end FROM routine_occurrences WHERE plan_id = ?', (plan_id,),
    ).fetchall():
        if row['status'] in ('completed', 'partial'):
            counts['achieved'] += 1
        elif row['status'] == 'scheduled' and datetime.fromisoformat(row['scheduled_end']) >= now:
            counts['future'] += 1
    return counts


def generate_occurrences(plan_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(tasks.LOCAL_TIMEZONE)
    with database_transaction() as connection:
        plan, routine = _load_plan(connection, plan_id)
        counts = _count_states(connection, plan_id, now)
        kept = counts['achieved'] + counts['future']
        needed = max(plan['target_count'] - kept, 0)
        created = _place_occurrences(connection, plan, routine, needed, now)
        if plan['status'] == 'planned':
            connection.execute(
                "UPDATE routine_plans SET status='active', updated_at=? WHERE id=?", (now_iso(), plan_id),
            )
    return {'ok': True, 'plan_id': plan_id, 'kept': kept, 'created': created, 'shortfall': needed - len(created)}


def reevaluate_plan(plan_id: str, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(tasks.LOCAL_TIMEZONE)
    with database_transaction() as connection:
        plan, routine = _load_plan(connection, plan_id)
        missed = [dict(row) for row in connection.execute(
            '''SELECT id, scheduled_start, scheduled_end FROM routine_occurrences
               WHERE plan_id = ? AND status = 'scheduled' ORDER BY scheduled_start''', (plan_id,),
        ).fetchall() if datetime.fromisoformat(row['scheduled_end']) < now]
        for occurrence in missed:
            connection.execute(
                "UPDATE routine_occurrences SET status='skipped', updated_at=? WHERE id=?",
                (now_iso(), occurrence['id']),
            )
        counts = _count_states(connection, plan_id, now)
        need = max(plan['target_count'] - counts['achieved'] - counts['future'], 0)
        created = _place_occurrences(connection, plan, routine, need, now)
        shortfall = need - len(created)
        previous_target = plan['target_count']
        new_target = previous_target
        fixed = routine['routine_type'] == 'fixed' or routine['adjustment_policy'] == 'fixed'
        ended = date.fromisoformat(plan['period_end']) < now.date()
        if shortfall > 0 and not fixed and not ended:
            new_target = previous_target - shortfall
        below_minimum = new_target < routine['min_count']
        status = 'completed' if ended else plan['status']
        reason = plan['adjustment_reason']
        if new_target != previous_target:
            note = f'未実施の再評価で目標回数を{previous_target}回から{new_target}回へ変更（残り日数に配置できない{shortfall}回分）。'
            reason = f'{reason}\n{note}'.strip()
        if new_target != previous_target or status != plan['status']:
            connection.execute(
                '''UPDATE routine_plans SET target_count=?, status=?, adjustment_reason=?,
                   adjustment_source=?, updated_at=? WHERE id=?''',
                (new_target, status, reason, 'norn' if new_target != previous_target else plan['adjustment_source'],
                 now_iso(), plan_id),
            )
    return {
        'ok': True, 'plan_id': plan_id, 'missed': missed, 'created': created,
        'achieved': counts['achieved'], 'upcoming': counts['future'], 'shortfall': shortfall,
        'previous_target_count': previous_target, 'target_count': new_target,
        'below_minimum': below_minimum, 'target_locked': fixed and shortfall > 0, 'plan_status': status,
    }


@router.post('/api/routine-plans/{plan_id}/occurrences/generate')
def generate_plan_occurrences(plan_id: str) -> dict[str, Any]:
    return generate_occurrences(_uuid(plan_id, 'ルーティン計画IDが不正です。'))


@router.post('/api/routine-plans/{plan_id}/reevaluate')
def reevaluate_plan_endpoint(plan_id: str) -> dict[str, Any]:
    return reevaluate_plan(_uuid(plan_id, 'ルーティン計画IDが不正です。'))


@router.post('/api/routines/{routine_id}/weekly-plan', status_code=201)
def create_weekly_plan(routine_id: str, payload: WeeklyPlanPayload | None = None) -> dict[str, Any]:
    payload = payload or WeeklyPlanPayload()
    routine_id = _uuid(routine_id, 'ルーティンIDが不正です。')
    with closing(open_database()) as connection:
        routine = connection.execute(
            'SELECT * FROM routines WHERE id = ? AND is_active = 1', (routine_id,),
        ).fetchone()
    if routine is None:
        raise HTTPException(status_code=404, detail='有効なルーティンが見つかりません。')
    today = datetime.now(tasks.LOCAL_TIMEZONE).date()
    week_start = payload.week_start or today - timedelta(days=today.weekday())
    target = routine['target_count'] if payload.target_count is None else payload.target_count
    duration = routine['duration_minutes'] if payload.duration_minutes is None else payload.duration_minutes
    adjusted = (target, duration) != (routine['target_count'], routine['duration_minutes'])
    plan = create_routine_plan(routine_id, RoutinePlanPayload(
        period_start=week_start, period_end=week_start + timedelta(days=6),
        target_count=target, duration_minutes=duration,
        adjustment_reason=payload.adjustment_reason or ('' if not adjusted else '週間設定で調整'),
        adjustment_source='user' if adjusted else 'norn',
    ))
    result: dict[str, Any] = {'plan': plan, 'generation': None}
    if payload.generate:
        result['generation'] = generate_occurrences(plan['id'])
        with closing(open_database()) as connection:
            result['plan'] = dict(connection.execute('SELECT * FROM routine_plans WHERE id = ?', (plan['id'],)).fetchone())
    return result


ROUTINE_PLAN_TOOLS = [
    {'type': 'function', 'function': {
        'name': 'list_routine_plans',
        'description': '有効なルーティンと、その週間計画（RoutinePlan）・実施予定の件数を取得する。plan_routine_weekやreevaluate_routine_planの対象ID特定に使う。',
        'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'create_routine',
        'description': '回数で決まる習慣（練習など。例: 週N回・1回X分）を新しいルーティンとして登録し、続けて今週（またはweek_start）の週間計画を作って空き時間へ承認なしで配置する。既存タスクを毎日配置するschedule_routineとは別物。同名の有効なルーティンがあればlist_routine_plansのIDでplan_routine_weekを使い、このツールで重複作成しない。所要時間が不明なら呼ぶ前に尋ねる。',
        'parameters': {'type': 'object', 'properties': {
            'title': {'type': 'string'},
            'duration_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440, 'description': '1回あたりの所要時間（分）。'},
            'target_count': {'type': 'integer', 'minimum': 1, 'maximum': 31, 'description': '週の目標回数。未指定なら週3回。'},
            'preferred_period': {'type': 'string', 'description': 'morning/afternoon/evening など。'},
            'description': {'type': 'string'},
            'week_start': {'type': 'string', 'format': 'date', 'description': '未指定なら今週の月曜。'},
        }, 'required': ['title', 'duration_minutes'], 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'plan_routine_week',
        'description': 'ルーティンの週間計画を作り、空き時間へ実施予定を承認なしで配置する。週の目標回数・所要時間だけ変えたい場合に指定し、基本のルーティン設定は変更しない。同じ週に計画が既にあれば失敗する。',
        'parameters': {'type': 'object', 'properties': {
            'routine_id': {'type': 'string'},
            'week_start': {'type': 'string', 'format': 'date', 'description': '週の開始日。未指定なら今週の月曜。'},
            'target_count': {'type': 'integer', 'minimum': 0, 'maximum': 31},
            'duration_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
            'adjustment_reason': {'type': 'string'},
        }, 'required': ['routine_id'], 'additionalProperties': False},
    }},
    {'type': 'function', 'function': {
        'name': 'reevaluate_routine_plan',
        'description': 'ルーティン週間計画の未実施を再評価する。過ぎた未実施を未実施扱いにし、残り日数へ再配置する。配置できない分だけ週の目標回数を下げる（固定型は下げない）。',
        'parameters': {'type': 'object', 'properties': {'plan_id': {'type': 'string'}},
                       'required': ['plan_id'], 'additionalProperties': False},
    }},
]


def execute_routine_plan_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == 'list_routine_plans':
            with closing(open_database()) as connection:
                routines = [dict(row) for row in connection.execute(
                    'SELECT id, title, routine_type, target_count, duration_minutes, preferred_period, min_count FROM routines WHERE is_active = 1 ORDER BY priority DESC, title',
                ).fetchall()]
                for routine in routines:
                    routine['plans'] = [dict(row) for row in connection.execute(
                        '''SELECT p.id, p.period_start, p.period_end, p.target_count, p.status,
                           (SELECT COUNT(*) FROM routine_occurrences o WHERE o.plan_id = p.id AND o.status = 'scheduled') AS scheduled,
                           (SELECT COUNT(*) FROM routine_occurrences o WHERE o.plan_id = p.id AND o.status IN ('completed', 'partial')) AS achieved
                           FROM routine_plans p WHERE p.routine_id = ? ORDER BY p.period_start DESC LIMIT 4''',
                        (routine['id'],),
                    ).fetchall()]
            return {'ok': True, 'routines': routines}
        if name in {'plan_routine_week', 'create_routine'}:
            arguments = dict(arguments)
            if name == 'create_routine':
                count = int(arguments.get('target_count') or 3)
                title = str(arguments.get('title', '')).strip()
                with closing(open_database()) as connection:
                    duplicate = connection.execute(
                        'SELECT id FROM routines WHERE is_active = 1 AND title = ?', (title,),
                    ).fetchone()
                if duplicate:
                    return {'ok': False, 'error': f'同名のルーティンが既にあります（id={duplicate["id"]}）。plan_routine_weekを使ってください。'}
                created = create_routine(RoutineDefinitionPayload(
                    title=title, description=str(arguments.get('description') or ''),
                    target_count=count, min_count=1, max_count=max(count, 7),
                    duration_minutes=int(arguments['duration_minutes']),
                    preferred_period=arguments.get('preferred_period') or None,
                    adjustment_policy='flexible', flexible=True,
                ))
                routine_id = created['id']
                arguments = {key: arguments[key] for key in ('week_start',) if key in arguments}
            else:
                routine_id = arguments.pop('routine_id', '')
            week_start = arguments.get('week_start')
            if isinstance(week_start, str) and week_start:
                arguments['week_start'] = date.fromisoformat(week_start)
            else:
                arguments.pop('week_start', None)
            outcome = create_weekly_plan(str(routine_id), WeeklyPlanPayload(**arguments))
            plan, generation = outcome['plan'], outcome['generation']
            with closing(open_database()) as connection:
                title = connection.execute('SELECT title FROM routines WHERE id = ?', (plan['routine_id'],)).fetchone()['title']
            slots = [f"{item['start_at']}〜{item['end_at']}" for item in generation['created']]
            return {
                'ok': True, 'plan': plan, 'generation': generation,
                'summary': f"ルーティン「{title}」の週間計画（{plan['period_start']}〜{plan['period_end']}、目標{plan['target_count']}回）を作り、"
                           f"{len(slots)}回分を配置しました。" + (f"配置: {', '.join(slots)}。" if slots else '')
                           + (f"空きが足りず{generation['shortfall']}回分は配置できませんでした。" if generation['shortfall'] else ''),
            }
        if name == 'reevaluate_routine_plan':
            result = reevaluate_plan(_uuid(str(arguments.get('plan_id', '')), 'ルーティン計画IDが不正です。'))
            note = f"未実施{len(result['missed'])}件を未実施扱いにし、{len(result['created'])}回分を再配置しました。"
            if result['target_count'] != result['previous_target_count']:
                note += f"配置できない分があるため目標を{result['previous_target_count']}回から{result['target_count']}回へ下げました。"
            elif result['target_locked']:
                note += f"固定型のため目標は変えず、{result['shortfall']}回分が不足しています。"
            if result['below_minimum']:
                note += '最低回数を下回っています。'
            return {**result, 'summary': note}
    except HTTPException as error:
        return {'ok': False, 'error': str(error.detail)}
    except (ValueError, TypeError) as error:
        return {'ok': False, 'error': f'ツール引数が不正です: {error}'}
    return {'ok': False, 'error': f'未対応のツールです: {name}'}
