import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator

from database import now_iso, open_database
from events import find_event_conflicts, query_events
from settings import PLANNING_DAY_END, PLANNING_DAY_START, PLANNING_WORKDAYS
from work_calendar import load_day_overrides, working_hours_for_day

LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
TaskStatus = Literal['open', 'in_progress', 'done', 'cancelled']
TaskPriority = Literal['low', 'normal', 'high']
TaskFlexibility = Literal['flexible', 'user_approval', 'fixed']
router = APIRouter()


class TaskPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    project_id: str | None = None
    description: str = Field(default='', max_length=4000)
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    remaining_minutes: int | None = Field(default=None, ge=0, le=1440)
    priority: TaskPriority = 'normal'
    focus_level: int | None = Field(default=None, strict=True, ge=1, le=5)
    preferred_period: str | None = Field(default=None, max_length=80)
    splittable: bool = True
    min_session_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_session_minutes: int | None = Field(default=None, ge=1, le=1440)
    context: str = Field(default='', max_length=2000)
    flexibility: TaskFlexibility = 'user_approval'

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

    @model_validator(mode='after')
    def validate_session_bounds(self):
        if (
            self.min_session_minutes is not None and self.max_session_minutes is not None
            and self.min_session_minutes > self.max_session_minutes
        ):
            raise ValueError('最低作業時間は最大作業時間以下にしてください。')
        return self


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    project_id: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    due_at: datetime | None = None
    estimated_minutes: int | None = Field(default=None, ge=1, le=1440)
    remaining_minutes: int | None = Field(default=None, ge=0, le=1440)
    priority: TaskPriority | None = None
    status: TaskStatus | None = None
    focus_level: int | None = Field(default=None, strict=True, ge=1, le=5)
    preferred_period: str | None = Field(default=None, max_length=80)
    splittable: bool | None = None
    min_session_minutes: int | None = Field(default=None, ge=1, le=1440)
    max_session_minutes: int | None = Field(default=None, ge=1, le=1440)
    context: str | None = Field(default=None, max_length=2000)
    flexibility: TaskFlexibility | None = None

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

    @model_validator(mode='after')
    def validate_session_bounds(self):
        minimum = self.min_session_minutes
        maximum = self.max_session_minutes
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError('最低作業時間は最大作業時間以下にしてください。')
        if any(self.model_fields_set.__contains__(field) and getattr(self, field) is None
               for field in ('splittable', 'context', 'flexibility')):
            raise ValueError('このタスク項目はnullにできません。')
        return self


class TaskSchedulePayload(BaseModel):
    start_at: datetime

    @field_validator('start_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)


class TaskAllocationPayload(BaseModel):
    start_at: datetime
    end_at: datetime

    @field_validator('start_at', 'end_at')
    @classmethod
    def use_tokyo_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=LOCAL_TIMEZONE)
        return value.astimezone(LOCAL_TIMEZONE)

    @model_validator(mode='after')
    def validate_time_range(self):
        if self.end_at <= self.start_at:
            raise ValueError('終了時刻は開始時刻より後にしてください。')
        return self


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
            'name': 'plan_task_allocations',
            'description': '指定期間におけるタスクの複数作業枠案を読み取り専用で計算する。空き時間・作業時間・期限・分割条件を守る。保存は行わない。「N日に分けて」ならdistinct_daysにNを渡す。同名タスクが複数ある場合は期限切れでないものを選ぶ。複数枠を配置する場合は各枠をcreate_task_allocationの操作としてapply_scheduleへ渡す（承認なしで保存される）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'task_id': {'type': 'string'},
                    'from_at': {'type': 'string', 'format': 'date-time'},
                    'through_at': {'type': 'string', 'format': 'date-time'},
                    'distinct_days': {'type': 'integer', 'minimum': 1, 'maximum': 8, 'description': '「N日に分けて」と頼まれたときのN。1日1枠ずつ、合計をN日へ均等に配分する。'},
                },
                'required': ['task_id', 'from_at', 'through_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'plan_week_allocations',
            'description': '週間など指定期間で、未完了の複数タスクを優先度順（期限30%・目標貢献30%・緊急性20%・依存10%・停滞10%）に、1日の利用可能時間の85%以内・作業間の休憩10分込みで配置する案を読み取り専用で計算する。保存は行わない。週間の配置案を内容確認だけしたいときに使う。ユーザーが配置を求めたら、操作を自分で組み立てずschedule_week_planを使う（承認なしで保存される）。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'from_at': {'type': 'string', 'format': 'date-time'},
                    'through_at': {'type': 'string', 'format': 'date-time'},
                },
                'required': ['from_at', 'through_at'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'schedule_task',
            'description': '空き時間検索結果から選んだ開始時刻にタスクを1枠だけ割り当てる。所要時間はタスクの見積時間を使う。「N日に分けて」「振り分けて」「複数日に」「次の三日」など複数枠への分割を頼まれたときは使わず、plan_task_allocations（distinct_days=N）の結果をapply_scheduleで保存する。',
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


def ceil_to_five_minutes(value: datetime) -> datetime:
    if value.second or value.microsecond:
        value = value.replace(second=0, microsecond=0) + timedelta(minutes=1)
    return value.replace(minute=value.minute, second=0, microsecond=0) + timedelta(minutes=-value.minute % 5)


def ceil_to_quarter_hour(value: datetime) -> datetime:
    if value.second or value.microsecond:
        value += timedelta(minutes=1)
    rounded_minute = ((value.minute + 14) // 15) * 15
    if rounded_minute == 60:
        return (value + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return value.replace(minute=rounded_minute, second=0, microsecond=0)


def available_work_periods(
    start: datetime,
    end: datetime,
    busy_periods: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    work_start = configured_time(PLANNING_DAY_START)
    work_end = configured_time(PLANNING_DAY_END)
    if work_end <= work_start or not PLANNING_WORKDAYS or any(day < 0 or day > 6 for day in PLANNING_WORKDAYS):
        raise HTTPException(status_code=500, detail='作業時間・曜日の設定が正しくありません。')

    periods: list[tuple[datetime, datetime]] = []
    overrides = load_day_overrides(start.date(), end.date())
    current_day = start.date()
    while current_day <= end.date():
        hours = working_hours_for_day(current_day, work_start, work_end, PLANNING_WORKDAYS, overrides)
        if hours is not None:
            day_start = datetime.combine(current_day, hours[0], LOCAL_TIMEZONE)
            day_end = datetime.combine(current_day, hours[1], LOCAL_TIMEZONE)
            cursor = max(day_start, start)
            day_limit = min(day_end, end)
            for busy_start, busy_end in busy_periods:
                if busy_end <= cursor or busy_start >= day_limit:
                    continue
                if busy_start > cursor:
                    periods.append((cursor, min(busy_start, day_limit)))
                cursor = max(cursor, busy_end)
                if cursor >= day_limit:
                    break
            if cursor < day_limit:
                periods.append((cursor, day_limit))
        current_day += timedelta(days=1)
    return periods


def planning_hours_for_day(day: date) -> tuple[time, time] | None:
    return working_hours_for_day(
        day, configured_time(PLANNING_DAY_START), configured_time(PLANNING_DAY_END),
        PLANNING_WORKDAYS,
    )


def is_planning_workday(day: date) -> bool:
    return planning_hours_for_day(day) is not None


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

        busy_events = [event for event in query_events(local_start, local_end) if not event.get('is_cancelled')]
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

    duration = timedelta(minutes=task['estimated_minutes'])
    maximum = max(1, min(limit, 10))
    slots: list[dict[str, str]] = []
    for period_start, period_end in available_work_periods(local_start, local_end, busy_periods):
        candidate = ceil_to_quarter_hour(period_start)
        while candidate + duration <= period_end and len(slots) < maximum:
            slots.append({
                'start_at': task_datetime_iso(candidate) or '',
                'end_at': task_datetime_iso(candidate + duration) or '',
            })
            candidate += timedelta(minutes=15)
        if len(slots) >= maximum:
            break

    return {
        'task_id': task_id,
        'title': task['title'],
        'estimated_minutes': task['estimated_minutes'],
        'slots': slots,
    }


def plan_task_allocations(
    task_id: str, from_at: datetime, through_at: datetime, distinct_days: int | None = None,
) -> dict[str, Any]:
    task_id = str(uuid.UUID(task_id))
    if distinct_days is not None and not 1 <= distinct_days <= 8:
        raise HTTPException(status_code=422, detail='分ける日数は1〜8日で指定してください。')
    range_start = task_datetime_iso(from_at)
    range_end = task_datetime_iso(through_at)
    if range_start is None or range_end is None or range_end <= range_start:
        raise HTTPException(status_code=422, detail='検索期間が正しくありません。')
    local_start = max(datetime.fromisoformat(range_start), datetime.now(LOCAL_TIMEZONE))
    local_end = datetime.fromisoformat(range_end)
    if local_end - local_start > timedelta(days=31):
        raise HTTPException(status_code=422, detail='空き時間の検索期間は31日以内にしてください。')

    with closing(open_database()) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        if task['status'] in {'done', 'cancelled'}:
            raise HTTPException(status_code=409, detail='完了または中止したタスクには時間枠を割り当てられません。')
        if task['estimated_minutes'] is None:
            raise HTTPException(status_code=409, detail='時間枠の計画には見積時間が必要です。')
        if task['due_at']:
            deadline_passed = datetime.fromisoformat(task['due_at']) <= local_start
            local_end = min(local_end, datetime.fromisoformat(task['due_at']))
        else:
            deadline_passed = False
        existing_minutes = sum(
            int((datetime.fromisoformat(row['end_at']) - datetime.fromisoformat(row['start_at'])).total_seconds() // 60)
            for row in connection.execute(
                'SELECT start_at, end_at FROM task_allocations WHERE task_id = ?', (task_id,),
            ).fetchall()
        )
        budget = task['remaining_minutes'] if task['remaining_minutes'] is not None else task['estimated_minutes']
        remaining = max(0, budget - existing_minutes)
        if local_end <= local_start:
            periods: list[tuple[datetime, datetime]] = []
        else:
            busy_events = [event for event in query_events(local_start, local_end) if not event.get('is_cancelled')]
            busy_tasks = connection.execute(
                '''SELECT start_at, end_at FROM task_allocations
                   WHERE start_at < ? AND end_at > ?''',
                (task_datetime_iso(local_end), task_datetime_iso(local_start)),
            ).fetchall()
            busy_periods = sorted(
                [(datetime.fromisoformat(row['start_at']), datetime.fromisoformat(row['end_at']))
                 for row in [*busy_events, *busy_tasks]],
                key=lambda period: period[0],
            )
            periods = available_work_periods(local_start, local_end, busy_periods)

    min_session = task['min_session_minutes'] or 1
    max_session = task['max_session_minutes'] or remaining
    if not task['splittable']:
        exceeds_maximum = (
            task['max_session_minutes'] is not None
            and remaining > task['max_session_minutes']
        )
        if remaining < min_session or exceeds_maximum:
            max_session = 0
        else:
            min_session = max_session = remaining
    allocations: list[dict[str, str | int]] = []
    block_limit = distinct_days or 8
    used_dates: set[Any] = set()
    if distinct_days and task['splittable'] and max_session:
        per_day = -(-remaining // distinct_days)
        max_session = min(max_session, max(per_day + (-per_day % 5), min_session))
    for period_start, period_end in periods:
        if len(allocations) >= block_limit:
            break
        cursor = ceil_to_five_minutes(period_start)
        while remaining > 0 and cursor < period_end and len(allocations) < block_limit:
            if distinct_days and cursor.date() in used_dates:
                break
            capacity = int((period_end - cursor).total_seconds() // 60)
            duration = min(remaining, max_session, capacity)
            leftover = remaining - duration
            if 0 < leftover < min_session:
                duration = remaining - min_session
            if duration < min_session or duration > capacity:
                break
            allocation_end = cursor + timedelta(minutes=duration)
            allocations.append({
                'start_at': task_datetime_iso(cursor) or '',
                'end_at': task_datetime_iso(allocation_end) or '',
                'duration_minutes': duration,
            })
            remaining -= duration
            used_dates.add(cursor.date())
            cursor = ceil_to_five_minutes(allocation_end)
            if not task['splittable'] or max_session == 0:
                break

    result: dict[str, Any] = {
        'task_id': task_id,
        'title': task['title'],
        'estimated_minutes': task['estimated_minutes'],
        'allocated_minutes': sum(int(slot['duration_minutes']) for slot in allocations),
        'unallocated_minutes': remaining,
        'complete': remaining == 0,
        'allocations': allocations,
    }
    if deadline_passed:
        result['reason'] = 'deadline_passed'
        result['message'] = f'このタスクの期限（{task["due_at"]}）は過ぎています。同名の別タスクが無いか確認してください。'
    return result


TARGET_SCHEDULE_RATIO = 0.85
BREAK_MINUTES = 10
DEFAULT_MAX_SESSION_MINUTES = 90
PRIORITY_WEIGHTS = {'deadline': 0.30, 'goal': 0.30, 'urgency': 0.20, 'dependency': 0.10, 'stagnation': 0.10}


def adaptive_break_minutes(focus_level: int | None, duration_minutes: int) -> int:
    # 基本10分を下回らせない。高集中または長時間の作業の後は15分に延ばす。
    if (focus_level or 0) >= 4 or duration_minutes >= 90:
        return BREAK_MINUTES + 5
    return BREAK_MINUTES


def task_priority_score(task: Any, goal_status: str | None, now: datetime, blocks_others: bool = False) -> dict[str, Any]:
    if task['due_at']:
        days_left = (datetime.fromisoformat(task['due_at']) - now).total_seconds() / 86400
        deadline = 100.0 if days_left <= 0 else max(0.0, 100.0 * (1 - days_left / 14))
    else:
        deadline = 0.0
    if goal_status == 'active':
        goal = 100.0
    elif task['project_id']:
        goal = 50.0
    else:
        goal = 0.0
    urgency = {'high': 100.0, 'normal': 50.0, 'low': 0.0}[task['priority']]
    idle_days = (now - datetime.fromisoformat(task['updated_at'])).total_seconds() / 86400
    stagnation = min(100.0, max(0.0, idle_days) / 14 * 100)
    components = {'deadline': deadline, 'goal': goal, 'urgency': urgency, 'dependency': 100.0 if blocks_others else 0.0, 'stagnation': stagnation}
    score = sum(components[name] * weight for name, weight in PRIORITY_WEIGHTS.items())
    return {'score': round(score, 1), 'components': {name: round(value, 1) for name, value in components.items()}}


def plan_week_allocations(from_at: datetime, through_at: datetime) -> dict[str, Any]:
    range_start = task_datetime_iso(from_at)
    range_end = task_datetime_iso(through_at)
    if range_start is None or range_end is None or range_end <= range_start:
        raise HTTPException(status_code=422, detail='検索期間が正しくありません。')
    now = datetime.now(LOCAL_TIMEZONE)
    local_start = max(datetime.fromisoformat(range_start), now)
    local_end = datetime.fromisoformat(range_end)
    if local_end - local_start > timedelta(days=31):
        raise HTTPException(status_code=422, detail='計画期間は31日以内にしてください。')

    with closing(open_database()) as connection:
        rows = connection.execute(
            '''SELECT tasks.*, goals.status AS goal_status FROM tasks
               LEFT JOIN projects ON projects.id = tasks.project_id
               LEFT JOIN goals ON goals.id = projects.goal_id
               WHERE tasks.status IN ('open', 'in_progress')''',
        ).fetchall()
        allocated = {
            row['task_id']: int(row['minutes'])
            for row in connection.execute(
                '''SELECT task_id, SUM((julianday(end_at) - julianday(start_at)) * 1440 + 0.5) AS minutes
                   FROM task_allocations GROUP BY task_id''',
            ).fetchall()
        }
        last_end = {
            row['task_id']: datetime.fromisoformat(row['last_end'])
            for row in connection.execute(
                'SELECT task_id, MAX(end_at) AS last_end FROM task_allocations GROUP BY task_id',
            ).fetchall()
        }
        open_ids = {row['id'] for row in rows}
        prerequisites: dict[str, set[str]] = {}
        for row in connection.execute('SELECT task_id, depends_on_task_id FROM task_dependencies').fetchall():
            if row['task_id'] in open_ids and row['depends_on_task_id'] in open_ids:
                prerequisites.setdefault(row['task_id'], set()).add(row['depends_on_task_id'])
        blocking = {dependency for dependencies in prerequisites.values() for dependency in dependencies}
        free: list[tuple[datetime, datetime]] = []
        if local_end > local_start:
            busy_events = [event for event in query_events(local_start, local_end) if not event.get('is_cancelled')]
            busy_tasks = connection.execute(
                'SELECT start_at, end_at FROM task_allocations WHERE start_at < ? AND end_at > ?',
                (task_datetime_iso(local_end), task_datetime_iso(local_start)),
            ).fetchall()
            busy_periods = sorted(
                [(datetime.fromisoformat(row['start_at']), datetime.fromisoformat(row['end_at']))
                 for row in [*busy_events, *busy_tasks]],
                key=lambda period: period[0],
            )
            free = available_work_periods(local_start, local_end, busy_periods)

    day_available: dict[Any, int] = {}
    for period_start, period_end in free:
        day_available[period_start.date()] = day_available.get(period_start.date(), 0) + int((period_end - period_start).total_seconds() // 60)
    day_cap = {day: int(minutes * TARGET_SCHEDULE_RATIO) // 5 * 5 for day, minutes in day_available.items()}
    day_used = {day: 0 for day in day_available}

    candidates: dict[str, tuple[Any, int, dict[str, Any]]] = {}
    skipped = []
    skipped_ids: set[str] = set()
    for task in rows:
        if task['estimated_minutes'] is None:
            skipped.append({'task_id': task['id'], 'title': task['title'], 'reason': 'missing_estimate'})
            skipped_ids.add(task['id'])
            continue
        budget = task['remaining_minutes'] if task['remaining_minutes'] is not None else task['estimated_minutes']
        remaining = budget - allocated.get(task['id'], 0)
        if remaining <= 0:
            continue
        if task['due_at'] and datetime.fromisoformat(task['due_at']) <= now:
            skipped.append({'task_id': task['id'], 'title': task['title'], 'reason': 'deadline_passed'})
            skipped_ids.add(task['id'])
            continue
        candidates[task['id']] = (task, remaining, task_priority_score(task, task['goal_status'], now, task['id'] in blocking))
    pending = sorted(
        candidates,
        key=lambda task_id: (-candidates[task_id][2]['score'], candidates[task_id][0]['due_at'] is None,
                             candidates[task_id][0]['due_at'] or '', candidates[task_id][0]['created_at']),
    )

    results = []
    incomplete: set[str] = set()
    processed: set[str] = set()
    planned_end = dict(last_end)
    while pending:
        # 前提タスクの配置が済んだものから、優先度の高い順に処理する。
        ready = next((task_id for task_id in pending if all(
            dependency in processed or (dependency not in candidates and dependency not in skipped_ids)
            for dependency in prerequisites.get(task_id, ())
        )), None)
        task_id = ready or pending[0]
        pending.remove(task_id)
        task, remaining, score = candidates[task_id]
        processed.add(task_id)
        blocked_by = [
            dependency for dependency in prerequisites.get(task_id, ())
            if dependency in skipped_ids or dependency in incomplete or (ready is None and dependency not in processed)
        ]
        if blocked_by:
            skipped.append({'task_id': task['id'], 'title': task['title'], 'reason': 'prerequisite_unplanned'})
            incomplete.add(task_id)
            continue
        earliest = max((planned_end[dependency] for dependency in prerequisites.get(task_id, ()) if dependency in planned_end), default=None)
        due = datetime.fromisoformat(task['due_at']) if task['due_at'] else None
        min_session = task['min_session_minutes'] or 5
        max_session = task['max_session_minutes'] or (DEFAULT_MAX_SESSION_MINUTES if task['splittable'] else remaining)
        if not task['splittable']:
            max_session = remaining if remaining <= max_session else 0
            min_session = remaining
        total = remaining
        allocations: list[dict[str, str | int]] = []
        while remaining > 0 and max_session:
            placed = False
            for index, (period_start, period_end) in enumerate(free):
                cursor = ceil_to_five_minutes(max(period_start, earliest) if earliest else period_start)
                limit = min(period_end, due) if due else period_end
                day = period_start.date()
                capacity = int((limit - cursor).total_seconds() // 60)
                duration = min(remaining, max_session, capacity, day_cap.get(day, 0) - day_used.get(day, 0))
                if duration < remaining:
                    duration = duration // 5 * 5
                leftover = remaining - duration
                if 0 < leftover < min_session:
                    duration = (remaining - min_session) // 5 * 5
                if duration < min_session or duration <= 0 or not task['splittable'] and duration != remaining:
                    continue
                block_end = cursor + timedelta(minutes=duration)
                break_minutes = adaptive_break_minutes(task['focus_level'], duration)
                allocations.append({
                    'start_at': task_datetime_iso(cursor) or '',
                    'end_at': task_datetime_iso(block_end) or '',
                    'duration_minutes': duration,
                    'break_after_minutes': break_minutes,
                })
                replacement = []
                if cursor > period_start:
                    replacement.append((period_start, cursor))
                resume = ceil_to_five_minutes(block_end + timedelta(minutes=break_minutes))
                if resume < period_end:
                    replacement.append((resume, period_end))
                free[index:index + 1] = replacement
                day_used[day] = day_used.get(day, 0) + duration
                remaining -= duration
                placed = True
                break
            if not placed:
                break
        if remaining > 0:
            incomplete.add(task_id)
        if allocations:
            planned_end[task_id] = max(datetime.fromisoformat(str(slot['end_at'])) for slot in allocations)
        results.append({
            'task_id': task['id'],
            'title': task['title'],
            'priority_score': score['score'],
            'score_components': score['components'],
            'depends_on': sorted(prerequisites.get(task_id, ())),
            'estimated_minutes': task['estimated_minutes'],
            'allocated_minutes': total - remaining,
            'unallocated_minutes': remaining,
            'complete': remaining == 0,
            'allocations': allocations,
        })
    results.sort(key=lambda item: -item['priority_score'])

    days = [
        {
            'date': day.isoformat(),
            'available_minutes': day_available[day],
            'planned_minutes': day_used[day],
            'ratio': round(day_used[day] / day_available[day], 2) if day_available[day] else 0,
        }
        for day in sorted(day_available)
    ]
    return {
        'from_at': task_datetime_iso(local_start),
        'through_at': task_datetime_iso(local_end),
        'target_schedule_ratio': TARGET_SCHEDULE_RATIO,
        'break_minutes': BREAK_MINUTES,
        'tasks': results,
        'skipped': skipped,
        'days': days,
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
        if start.minute % 5 != 0 or start.second or start.microsecond:
            raise HTTPException(status_code=409, detail='時間枠の開始時刻は5分刻みにしてください。')
        if start < datetime.now(LOCAL_TIMEZONE):
            raise HTTPException(status_code=409, detail='過去の時間にはタスクを割り当てられません。')
        work_hours = planning_hours_for_day(start.date())
        if work_hours is None:
            raise HTTPException(status_code=409, detail='設定された休日または作業曜日以外には割り当てられません。')
        if start.time() < work_hours[0] or end.date() != start.date() or end.time() > work_hours[1]:
            raise HTTPException(status_code=409, detail='設定された作業時間内に収まりません。')
        if task['due_at'] and end > datetime.fromisoformat(task['due_at']):
            raise HTTPException(status_code=409, detail='タスクの期限を過ぎる時間枠です。')

        start_iso = task_datetime_iso(start)
        end_iso = task_datetime_iso(end)
        conflicts = find_event_conflicts(connection, start_iso, end_iso)
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

        connection.execute('DELETE FROM task_allocations WHERE task_id = ?', (task_id,))
        allocation_id = str(uuid.uuid4())
        connection.execute(
            '''INSERT INTO task_allocations (id, task_id, start_at, end_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (allocation_id, task_id, start_iso, end_iso, timestamp, timestamp),
        )
        connection.commit()
        allocation = connection.execute(
            '''
            SELECT task_allocations.*, tasks.title, tasks.estimated_minutes
            FROM task_allocations JOIN tasks ON tasks.id = task_allocations.task_id
            WHERE task_allocations.id = ?
            ''',
            (allocation_id,),
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


def create_task_allocation_record(task_id: str, payload: TaskAllocationPayload) -> dict[str, Any]:
    task_id = str(uuid.UUID(task_id))
    start = payload.start_at
    end = payload.end_at
    duration = int((end - start).total_seconds() // 60)
    if duration <= 0 or (end - start).total_seconds() != duration * 60:
        raise HTTPException(status_code=422, detail='作業枠は1分以上の整数分で指定してください。')
    if start.minute % 5 != 0 or start.second or start.microsecond:
        raise HTTPException(status_code=409, detail='時間枠の開始時刻は5分刻みにしてください。')
    if start.date() != end.date():
        raise HTTPException(status_code=409, detail='作業枠は同じ日の中に収めてください。')
    if start < datetime.now(LOCAL_TIMEZONE):
        raise HTTPException(status_code=409, detail='過去の時間にはタスクを割り当てられません。')
    work_hours = planning_hours_for_day(start.date())
    if work_hours is None:
        raise HTTPException(status_code=409, detail='設定された休日または作業曜日以外には割り当てられません。')
    if start.time() < work_hours[0] or end.time() > work_hours[1]:
        raise HTTPException(status_code=409, detail='設定された作業時間内に収まりません。')

    start_iso = task_datetime_iso(start)
    end_iso = task_datetime_iso(end)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        task = connection.execute('SELECT * FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if task is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        if task['status'] in {'done', 'cancelled'}:
            raise HTTPException(status_code=409, detail='完了または中止したタスクには時間枠を割り当てられません。')
        remaining = task['remaining_minutes'] if task['remaining_minutes'] is not None else task['estimated_minutes']
        if remaining is None:
            raise HTTPException(status_code=409, detail='時間枠の割り当てには見積時間が必要です。')
        allocated = connection.execute(
            'SELECT COALESCE(SUM((julianday(end_at) - julianday(start_at)) * 1440), 0) FROM task_allocations WHERE task_id = ?',
            (task_id,),
        ).fetchone()[0]
        available = remaining - int(allocated)
        if duration > available:
            raise HTTPException(status_code=409, detail='作業枠の合計が残り見積時間を超えます。')
        if duration < available and not task['splittable']:
            raise HTTPException(status_code=409, detail='このタスクは分割不可です。')
        if task['min_session_minutes'] and duration < task['min_session_minutes']:
            raise HTTPException(status_code=409, detail='作業枠が最低作業時間を下回っています。')
        if task['max_session_minutes'] and duration > task['max_session_minutes']:
            raise HTTPException(status_code=409, detail='作業枠が最大作業時間を超えています。')
        if task['due_at'] and end > datetime.fromisoformat(task['due_at']):
            raise HTTPException(status_code=409, detail='タスクの期限を過ぎる時間枠です。')

        conflicts = find_event_conflicts(connection, start_iso, end_iso)
        conflicts += connection.execute(
            '''SELECT tasks.title FROM task_allocations
               JOIN tasks ON tasks.id = task_allocations.task_id
               WHERE task_allocations.start_at < ? AND task_allocations.end_at > ?''',
            (end_iso, start_iso),
        ).fetchall()
        if conflicts:
            titles = '、'.join(row['title'] for row in conflicts[:3])
            raise HTTPException(status_code=409, detail=f'時間が重複する予定またはタスクがあります: {titles}')
        allocation_id = str(uuid.uuid4())
        connection.execute(
            '''INSERT INTO task_allocations (id, task_id, start_at, end_at, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)''',
            (allocation_id, task_id, start_iso, end_iso, timestamp, timestamp),
        )
        connection.commit()
        row = connection.execute(
            '''SELECT a.*, t.title, t.estimated_minutes, t.status
               FROM task_allocations a JOIN tasks t ON t.id = a.task_id WHERE a.id = ?''',
            (allocation_id,),
        ).fetchone()
    return dict(row)


def delete_task_allocation_record(task_id: str, allocation_id: str) -> None:
    task_id = str(uuid.UUID(task_id))
    allocation_id = str(uuid.UUID(allocation_id))
    with closing(open_database()) as connection:
        result = connection.execute(
            'DELETE FROM task_allocations WHERE id = ? AND task_id = ?',
            (allocation_id, task_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='タスクの作業枠が見つかりません。')
        connection.commit()
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
            (SELECT a.start_at FROM task_allocations a
             WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_start,
            (SELECT a.end_at FROM task_allocations a
             WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_end,
            task_occurrence_links.source_task_id AS routine_source_task_id,
            task_occurrence_links.occurrence_date AS routine_date
        FROM tasks
        LEFT JOIN projects ON projects.id = tasks.project_id
        LEFT JOIN task_occurrence_links ON task_occurrence_links.task_id = tasks.id
    '''
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY CASE tasks.priority WHEN \'high\' THEN 0 WHEN \'normal\' THEN 1 ELSE 2 END, tasks.due_at IS NULL, tasks.due_at, tasks.created_at DESC'
    with closing(open_database()) as connection:
        rows = connection.execute(query, parameters).fetchall()
        dependencies: dict[str, list[str]] = {}
        for row in connection.execute('SELECT task_id, depends_on_task_id FROM task_dependencies ORDER BY depends_on_task_id').fetchall():
            dependencies.setdefault(row['task_id'], []).append(row['depends_on_task_id'])
    return [{**dict(row), 'depends_on': dependencies.get(row['id'], [])} for row in rows]


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
                id, project_id, title, description, due_at, estimated_minutes, remaining_minutes, priority,
                status, focus_level, preferred_period, splittable, min_session_minutes, max_session_minutes,
                context, flexibility, created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
            ''',
            (
                task_id,
                payload.project_id,
                payload.title,
                payload.description.strip(),
                task_datetime_iso(payload.due_at),
                payload.estimated_minutes,
                payload.remaining_minutes if payload.remaining_minutes is not None else payload.estimated_minutes,
                payload.priority,
                payload.focus_level,
                payload.preferred_period.strip() if payload.preferred_period else None,
                int(payload.splittable),
                payload.min_session_minutes,
                payload.max_session_minutes,
                payload.context.strip(),
                payload.flexibility,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            '''
            SELECT tasks.*,
                (SELECT a.start_at FROM task_allocations a
                 WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_start,
                (SELECT a.end_at FROM task_allocations a
                 WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_end,
                projects.title AS project_title
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
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
        if (
            values['min_session_minutes'] is not None and values['max_session_minutes'] is not None
            and values['min_session_minutes'] > values['max_session_minutes']
        ):
            raise HTTPException(status_code=422, detail='最低作業時間は最大作業時間以下にしてください。')
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
                remaining_minutes = ?, priority = ?, status = ?, focus_level = ?, preferred_period = ?,
                splittable = ?, min_session_minutes = ?, max_session_minutes = ?, context = ?, flexibility = ?,
                updated_at = ?, completed_at = ?
            WHERE id = ?
            ''',
            (
                values['title'], values['project_id'], values['description'], values['due_at'],
                values['estimated_minutes'], values['remaining_minutes'], values['priority'], values['status'],
                values['focus_level'], values['preferred_period'], int(values['splittable']),
                values['min_session_minutes'], values['max_session_minutes'], values['context'],
                values['flexibility'], timestamp, completed_at, task_id,
            ),
        )
        connection.commit()
        updated = connection.execute(
            '''
            SELECT tasks.*,
                (SELECT a.start_at FROM task_allocations a
                 WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_start,
                (SELECT a.end_at FROM task_allocations a
                 WHERE a.task_id = tasks.id ORDER BY a.start_at LIMIT 1) AS scheduled_end,
                projects.title AS project_title
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            WHERE tasks.id = ?
            ''',
            (task_id,),
        ).fetchone()
    return dict(updated)


def task_allocation_ids(task_id: str) -> list[str]:
    with closing(open_database()) as connection:
        return [row['id'] for row in connection.execute(
            'SELECT id FROM task_allocations WHERE task_id = ? ORDER BY start_at', (task_id,),
        ).fetchall()]


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


@router.get('/api/tasks/{task_id}/allocations')
def list_task_allocations_for_task(task_id: str) -> list[dict[str, Any]]:
    try:
        task_id = str(uuid.UUID(task_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='タスクIDが不正です。') from error
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(
            '''SELECT * FROM task_allocations WHERE task_id = ? ORDER BY start_at''',
            (task_id,),
        ).fetchall()]


@router.post('/api/tasks/{task_id}/allocations', status_code=201)
def create_task_allocation(task_id: str, payload: TaskAllocationPayload) -> dict[str, Any]:
    try:
        task_id = str(uuid.UUID(task_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='タスクIDが不正です。') from error
    return create_task_allocation_record(task_id, payload)


@router.delete('/api/tasks/{task_id}/allocations/{allocation_id}', status_code=204)
def delete_task_allocation(task_id: str, allocation_id: str) -> None:
    try:
        task_id = str(uuid.UUID(task_id))
        allocation_id = str(uuid.UUID(allocation_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='タスクまたは作業枠のIDが不正です。') from error
    delete_task_allocation_record(task_id, allocation_id)


@router.get('/api/tasks/{task_id}/available-slots')
def get_task_slots(
    task_id: str,
    from_at: datetime,
    through_at: datetime,
    limit: int = Query(default=5, ge=1, le=10),
) -> dict[str, Any]:
    return find_task_slots(task_id, from_at, through_at, limit)


@router.get('/api/tasks/{task_id}/allocation-plan')
def get_task_allocation_plan(
    task_id: str,
    from_at: datetime,
    through_at: datetime,
    distinct_days: int | None = Query(default=None, ge=1, le=8),
) -> dict[str, Any]:
    try:
        task_id = str(uuid.UUID(task_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='タスクIDが不正です。') from error
    return plan_task_allocations(task_id, from_at, through_at, distinct_days)


class TaskDependenciesPayload(BaseModel):
    depends_on: list[str] = Field(default_factory=list, max_length=20)

    @field_validator('depends_on')
    @classmethod
    def clean_ids(cls, value: list[str]) -> list[str]:
        try:
            return sorted({str(uuid.UUID(item)) for item in value})
        except ValueError as error:
            raise ValueError('タスクIDが不正です。') from error


def set_task_dependencies(task_id: str, depends_on: list[str]) -> list[str]:
    task_id = str(uuid.UUID(task_id))
    depends_on = sorted({str(uuid.UUID(item)) for item in depends_on})
    if task_id in depends_on:
        raise HTTPException(status_code=422, detail='タスクは自分自身に依存できません。')
    with closing(open_database()) as connection:
        if connection.execute('SELECT 1 FROM tasks WHERE id = ?', (task_id,)).fetchone() is None:
            raise HTTPException(status_code=404, detail='タスクが見つかりません。')
        for dependency in depends_on:
            if connection.execute('SELECT 1 FROM tasks WHERE id = ?', (dependency,)).fetchone() is None:
                raise HTTPException(status_code=404, detail='依存先のタスクが見つかりません。')
        edges: dict[str, set[str]] = {}
        for row in connection.execute('SELECT task_id, depends_on_task_id FROM task_dependencies WHERE task_id != ?', (task_id,)).fetchall():
            edges.setdefault(row['task_id'], set()).add(row['depends_on_task_id'])
        edges[task_id] = set(depends_on)
        stack, seen = list(depends_on), set()
        while stack:
            current = stack.pop()
            if current == task_id:
                raise HTTPException(status_code=409, detail='依存関係が循環します。')
            if current not in seen:
                seen.add(current)
                stack.extend(edges.get(current, ()))
        connection.execute('DELETE FROM task_dependencies WHERE task_id = ?', (task_id,))
        timestamp = now_iso()
        connection.executemany(
            'INSERT INTO task_dependencies (task_id, depends_on_task_id, created_at) VALUES (?, ?, ?)',
            [(task_id, dependency, timestamp) for dependency in depends_on],
        )
        connection.commit()
    return depends_on


@router.get('/api/task-allocation-plan')
def get_week_allocation_plan(from_at: datetime, through_at: datetime) -> dict[str, Any]:
    return plan_week_allocations(from_at, through_at)


@router.put('/api/tasks/{task_id}/dependencies')
def put_task_dependencies(task_id: str, payload: TaskDependenciesPayload) -> dict[str, Any]:
    try:
        task_id = str(uuid.UUID(task_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='タスクIDが不正です。') from error
    return {'task_id': task_id, 'depends_on': set_task_dependencies(task_id, payload.depends_on)}


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

        if name == 'plan_task_allocations':
            if not {'task_id', 'from_at', 'through_at'} <= set(arguments) <= {'task_id', 'from_at', 'through_at', 'distinct_days'}:
                raise ValueError('タスクIDと検索期間を指定してください。')
            plan = plan_task_allocations(
                arguments['task_id'],
                datetime.fromisoformat(arguments['from_at']),
                datetime.fromisoformat(arguments['through_at']),
                int(arguments['distinct_days']) if arguments.get('distinct_days') is not None else None,
            )
            plan['ok'] = True
            return plan

        if name == 'plan_week_allocations':
            if set(arguments) != {'from_at', 'through_at'}:
                raise ValueError('計画期間を指定してください。')
            plan = plan_week_allocations(
                datetime.fromisoformat(arguments['from_at']),
                datetime.fromisoformat(arguments['through_at']),
            )
            plan['ok'] = True
            return plan

        if name == 'create_task_allocation':
            if set(arguments) != {'task_id', 'start_at', 'end_at'}:
                raise ValueError('タスクIDと作業枠の開始・終了時刻を指定してください。')
            allocation = create_task_allocation_record(
                arguments['task_id'],
                TaskAllocationPayload(
                    start_at=datetime.fromisoformat(arguments['start_at']),
                    end_at=datetime.fromisoformat(arguments['end_at']),
                ),
            )
            return {
                'ok': True,
                'allocation': allocation,
                'summary': f'タスク「{allocation["title"]}」の作業枠を{allocation["start_at"]}〜{allocation["end_at"]}に追加しました。',
            }

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
            removed_ids = task_allocation_ids(str(uuid.UUID(arguments['task_id'])))
            removed = unschedule_task_record(arguments['task_id'])
            return {
                'ok': True,
                'removed_allocation_ids': removed_ids,
                'summary': f'タスクの時間枠{len(removed_ids)}件を解除しました。' if removed else 'このタスクに時間枠は設定されていません。',
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
                removed_ids = task_allocation_ids(task_id)
                title = delete_task_record(task_id)
                return {'ok': True, 'removed_allocation_ids': removed_ids, 'summary': f'タスク「{title}」を削除しました。'}

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
