import json
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database import database_transaction, now_iso, open_database
import tasks

router = APIRouter()


class RoutineDefinitionPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=4000)
    routine_type: Literal['achievement', 'maintenance', 'fixed'] = 'maintenance'
    frequency: str = Field(default='weekly', min_length=1, max_length=80)
    target_count: int = Field(default=1, ge=1, le=31)
    duration_minutes: int = Field(ge=1, le=1440)
    preferred_period: str | None = Field(default=None, max_length=80)
    min_count: int = Field(default=0, ge=0, le=31)
    max_count: int = Field(ge=1, le=31)
    adjustment_policy: Literal['flexible', 'user_approval', 'fixed'] = 'user_approval'
    flexible: bool = False
    priority: Literal['low', 'normal', 'high'] = 'normal'
    is_active: bool = True

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('ルーティン名を入力してください。')
        return value

    @field_validator('frequency')
    @classmethod
    def clean_frequency(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('頻度を入力してください。')
        return value

    @model_validator(mode='after')
    def validate_counts(self):
        if not self.min_count <= self.target_count <= self.max_count:
            raise ValueError('最低回数・目標回数・最大回数の順に設定してください。')
        if self.flexible and self.adjustment_policy == 'fixed':
            raise ValueError('固定ルーティンは柔軟設定にできません。')
        return self


class RoutineDefinitionUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    routine_type: Literal['achievement', 'maintenance', 'fixed'] | None = None
    frequency: str | None = Field(default=None, min_length=1, max_length=80)
    target_count: int | None = Field(default=None, ge=1, le=31)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    preferred_period: str | None = Field(default=None, max_length=80)
    min_count: int | None = Field(default=None, ge=0, le=31)
    max_count: int | None = Field(default=None, ge=1, le=31)
    adjustment_policy: Literal['flexible', 'user_approval', 'fixed'] | None = None
    flexible: bool | None = None
    priority: Literal['low', 'normal', 'high'] | None = None
    is_active: bool | None = None

    @field_validator('title', 'frequency')
    @classmethod
    def clean_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('空の値は指定できません。')
        return value

    @model_validator(mode='after')
    def reject_null_nonnullable_fields(self):
        nonnullable = {
            'title', 'description', 'routine_type', 'frequency', 'target_count', 'duration_minutes',
            'min_count', 'max_count', 'adjustment_policy', 'flexible', 'priority', 'is_active',
        }
        if any(field in self.model_fields_set and getattr(self, field) is None for field in nonnullable):
            raise ValueError('指定項目にnullは設定できません。')
        return self


class RoutinePlanPayload(BaseModel):
    period_start: date
    period_end: date
    target_count: int = Field(ge=0, le=31)
    duration_minutes: int = Field(ge=1, le=1440)
    adjustment_reason: str = Field(default='', max_length=2000)
    adjustment_source: Literal['user', 'norn'] = 'user'

    @model_validator(mode='after')
    def validate_period(self):
        if self.period_end < self.period_start:
            raise ValueError('計画期間の終了日は開始日以降にしてください。')
        if (self.period_end - self.period_start).days > 30:
            raise ValueError('ルーティン計画は最大31日間です。')
        return self


class RoutineItem(BaseModel):
    model_config = ConfigDict(extra='forbid')
    task_id: uuid.UUID
    estimated_minutes: int | None = Field(default=None, strict=True, ge=1, le=1440)
    estimate_evidence: str = ''


class RoutinePayload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start_date: date
    through_date: date
    items: list[RoutineItem] = Field(min_length=1, max_length=8)

    @model_validator(mode='after')
    def bounded_range(self):
        if not 0 <= (self.through_date - self.start_date).days < 31:
            raise ValueError('ルーティンの期間は最終日を含め1〜31日で指定してください。')
        if len({item.task_id for item in self.items}) != len(self.items):
            raise ValueError('同じルーティン項目を重複指定しないでください。')
        return self


class OccurrenceLink(BaseModel):
    model_config = ConfigDict(extra='forbid')
    source_task_id: uuid.UUID
    task_id: uuid.UUID
    occurrence_date: date


ROUTINE_TOOLS = [{
    'type': 'function',
    'function': {
        'name': 'schedule_routine',
        'description': '既存タスクを毎日のルーティンとして最終日を含めて空き時間へ配置し、承認なしで保存する。31日以内・最大8項目。list_tasksか現在のDB情報で対象IDを特定する。時間未指定なら既存の所要時間を引き継ぐ。「全部1時間」等の明示的な変更は各itemsにestimated_minutesと最新user発言のestimate_evidenceを指定する。既存枠の時間変更もこのツールで保存する。所要時間不明ならその項目だけ尋ねる。配置できない日はmissingとして残り、配置できた分だけが一括で保存される。',
        'parameters': {
            'type': 'object',
            'properties': {
                'start_date': {'type': 'string', 'format': 'date', 'description': '未指定なら現在日。'},
                'through_date': {'type': 'string', 'format': 'date', 'description': 'この最終日も含める。'},
                'items': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {
                    'type': 'object', 'properties': {
                        'task_id': {'type': 'string'},
                        'estimated_minutes': {'type': 'integer', 'minimum': 1, 'maximum': 1440},
                        'estimate_evidence': {'type': 'string'},
                    }, 'required': ['task_id'], 'additionalProperties': False,
                }},
            },
            'required': ['start_date', 'through_date', 'items'], 'additionalProperties': False,
        },
    },
}]


@router.get('/api/routines')
def list_routines(active: bool | None = None) -> list[dict[str, Any]]:
    query = 'SELECT * FROM routines'
    parameters: tuple[Any, ...] = ()
    if active is not None:
        query += ' WHERE is_active = ?'
        parameters = (int(active),)
    query += ' ORDER BY is_active DESC, priority DESC, title'
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


@router.post('/api/routines', status_code=201)
def create_routine(payload: RoutineDefinitionPayload) -> dict[str, Any]:
    routine_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        connection.execute(
            '''INSERT INTO routines (
                id, title, description, routine_type, frequency, target_count, duration_minutes,
                preferred_period, min_count, max_count, adjustment_policy, flexible, priority,
                is_active, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                routine_id, payload.title, payload.description.strip(), payload.routine_type,
                payload.frequency, payload.target_count, payload.duration_minutes,
                payload.preferred_period.strip() if payload.preferred_period else None,
                payload.min_count, payload.max_count, payload.adjustment_policy, int(payload.flexible),
                payload.priority, int(payload.is_active), timestamp, timestamp,
            ),
        )
        connection.commit()
        return dict(connection.execute('SELECT * FROM routines WHERE id = ?', (routine_id,)).fetchone())


@router.put('/api/routines/{routine_id}')
def update_routine(routine_id: str, payload: RoutineDefinitionUpdate) -> dict[str, Any]:
    try:
        routine_id = str(uuid.UUID(routine_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='ルーティンIDが不正です。') from error
    with closing(open_database()) as connection:
        row = connection.execute('SELECT * FROM routines WHERE id = ?', (routine_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail='ルーティンが見つかりません。')
        values = dict(row)
        values.update(payload.model_dump(exclude_unset=True))
        if not values['min_count'] <= values['target_count'] <= values['max_count']:
            raise HTTPException(status_code=422, detail='最低回数・目標回数・最大回数の順に設定してください。')
        if values['flexible'] and values['adjustment_policy'] == 'fixed':
            raise HTTPException(status_code=422, detail='固定ルーティンは柔軟設定にできません。')
        connection.execute(
            '''UPDATE routines SET title=?, description=?, routine_type=?, frequency=?, target_count=?,
               duration_minutes=?, preferred_period=?, min_count=?, max_count=?, adjustment_policy=?,
               flexible=?, priority=?, is_active=?, updated_at=? WHERE id=?''',
            (
                values['title'], values['description'].strip(), values['routine_type'], values['frequency'],
                values['target_count'], values['duration_minutes'], values['preferred_period'],
                values['min_count'], values['max_count'], values['adjustment_policy'],
                int(values['flexible']), values['priority'], int(values['is_active']), now_iso(), routine_id,
            ),
        )
        connection.commit()
        return dict(connection.execute('SELECT * FROM routines WHERE id = ?', (routine_id,)).fetchone())


@router.delete('/api/routines/{routine_id}', status_code=204)
def deactivate_routine(routine_id: str) -> None:
    try:
        routine_id = str(uuid.UUID(routine_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='ルーティンIDが不正です。') from error
    with closing(open_database()) as connection:
        result = connection.execute(
            'UPDATE routines SET is_active=0, updated_at=? WHERE id=?',
            (now_iso(), routine_id),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='ルーティンが見つかりません。')
        connection.commit()


@router.get('/api/routine-plans')
def list_routine_plans(
    routine_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
) -> list[dict[str, Any]]:
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail='期間の開始日と終了日を両方指定してください。')
    conditions = []
    parameters: list[Any] = []
    if routine_id is not None:
        try:
            routine_id = str(uuid.UUID(routine_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail='ルーティンIDが不正です。') from error
        conditions.append('routine_id = ?')
        parameters.append(routine_id)
    if start is not None and end is not None:
        if end < start:
            raise HTTPException(status_code=422, detail='期間の終了日は開始日以降にしてください。')
        conditions.append('period_start <= ? AND period_end >= ?')
        parameters.extend((end.isoformat(), start.isoformat()))
    query = 'SELECT * FROM routine_plans'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY period_start, routine_id'
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


@router.post('/api/routines/{routine_id}/plans', status_code=201)
def create_routine_plan(routine_id: str, payload: RoutinePlanPayload) -> dict[str, Any]:
    try:
        routine_id = str(uuid.UUID(routine_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='ルーティンIDが不正です。') from error
    plan_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        if not connection.execute(
            'SELECT id FROM routines WHERE id = ? AND is_active = 1', (routine_id,),
        ).fetchone():
            raise HTTPException(status_code=404, detail='有効なルーティンが見つかりません。')
        overlap = connection.execute(
            '''SELECT id FROM routine_plans
               WHERE routine_id=? AND status IN ('planned', 'active')
                 AND period_start <= ? AND period_end >= ?''',
            (routine_id, payload.period_end.isoformat(), payload.period_start.isoformat()),
        ).fetchone()
        if overlap:
            raise HTTPException(status_code=409, detail='期間が重なるルーティン計画があります。')
        connection.execute(
            '''INSERT INTO routine_plans (
                id, routine_id, period_start, period_end, target_count, duration_minutes,
                adjustment_reason, adjustment_source, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)''',
            (
                plan_id, routine_id, payload.period_start.isoformat(), payload.period_end.isoformat(),
                payload.target_count, payload.duration_minutes, payload.adjustment_reason.strip(),
                payload.adjustment_source, timestamp, timestamp,
            ),
        )
        connection.commit()
        return dict(connection.execute('SELECT * FROM routine_plans WHERE id = ?', (plan_id,)).fetchone())


@router.get('/api/routine-occurrences')
def list_routine_occurrences(
    routine_id: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict[str, Any]]:
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail='期間の開始と終了を両方指定してください。')
    conditions = []
    parameters: list[Any] = []
    if routine_id is not None:
        try:
            routine_id = str(uuid.UUID(routine_id))
        except ValueError as error:
            raise HTTPException(status_code=422, detail='ルーティンIDが不正です。') from error
        conditions.append('routine_id = ?')
        parameters.append(routine_id)
    if start is not None and end is not None:
        start = start.replace(tzinfo=tasks.LOCAL_TIMEZONE) if start.tzinfo is None else start.astimezone(tasks.LOCAL_TIMEZONE)
        end = end.replace(tzinfo=tasks.LOCAL_TIMEZONE) if end.tzinfo is None else end.astimezone(tasks.LOCAL_TIMEZONE)
        if end <= start:
            raise HTTPException(status_code=422, detail='期間の終了は開始より後にしてください。')
        conditions.append('scheduled_start < ? AND scheduled_end > ?')
        parameters.extend((end.isoformat(), start.isoformat()))
    query = 'SELECT * FROM routine_occurrences'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY scheduled_start'
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


def link_occurrence(arguments: dict[str, Any]) -> dict[str, Any]:
    link = OccurrenceLink.model_validate(arguments)
    with closing(open_database()) as connection:
        allocation = connection.execute('SELECT start_at FROM task_allocations WHERE task_id = ?', (str(link.task_id),)).fetchone()
        if allocation is None or datetime.fromisoformat(allocation['start_at']).date() != link.occurrence_date:
            raise ValueError('ルーティンの実施日と作業枠が一致しません。')
        connection.execute(
            'INSERT INTO task_occurrence_links (source_task_id, occurrence_date, task_id) VALUES (?, ?, ?)',
            (str(link.source_task_id), link.occurrence_date.isoformat(), str(link.task_id)),
        )
        connection.commit()
    return {'ok': True, 'source_task_id': str(link.source_task_id), 'task_id': str(link.task_id),
            'occurrence_date': link.occurrence_date.isoformat()}


def schedule_routine(arguments: dict[str, Any], user_content: str = '') -> dict[str, Any]:
    from schedule_actions import ScheduleAction, apply_actions, check_final_conflicts

    try:
        payload = RoutinePayload.model_validate(arguments)
        if payload.start_date < datetime.now(tasks.LOCAL_TIMEZONE).date():
            raise ValueError('開始日は今日以降にしてください。')
        actions = []
        occurrences = []
        missing = []
        sources = []
        with database_transaction(rollback=True) as connection:
            for item in payload.items:
                row = connection.execute(
                    '''SELECT t.* FROM tasks t WHERE t.id = COALESCE(
                       (SELECT source_task_id FROM task_occurrence_links WHERE task_id = ?), ?)''',
                    (str(item.task_id), str(item.task_id)),
                ).fetchone()
                if row is None or row['status'] == 'cancelled':
                    raise ValueError('ルーティンの元タスクが見つからないか中止済みです。')
                if row['id'] in {source['id'] for source in sources}:
                    raise ValueError('同じ元タスクの実行分が重複しています。')
                source = dict(row)
                if item.estimated_minutes is not None:
                    if source['estimated_minutes'] != item.estimated_minutes and (
                        not item.estimate_evidence.strip() or item.estimate_evidence not in user_content
                    ):
                        raise ValueError('所要時間は最新のユーザー発言の根拠を指定してください。')
                    source['requested_minutes'] = item.estimated_minutes
                sources.append(source)
            day = payload.start_date
            while day <= payload.through_date:
                for source in sources:
                    base = {'source_task_id': source['id'], 'title': source['title'], 'date': day.isoformat()}
                    linked = connection.execute(
                        '''SELECT t.*, a.start_at, a.end_at FROM task_occurrence_links r JOIN tasks t ON t.id=r.task_id
                           LEFT JOIN task_allocations a ON a.task_id=t.id
                           WHERE r.source_task_id=? AND r.occurrence_date=?''',
                        (source['id'], day.isoformat()),
                    ).fetchone()
                    original = connection.execute('SELECT * FROM task_allocations WHERE task_id=?', (source['id'],)).fetchone()
                    existing = linked or (original if original and datetime.fromisoformat(original['start_at']).date() == day else None)
                    if linked and linked['status'] == 'done':
                        occurrences.append({**base, 'state': 'done', 'task_id': linked['id']})
                        continue
                    requested = source.get('requested_minutes')
                    duration = requested if requested is not None else linked['estimated_minutes'] if linked else source['estimated_minutes']
                    if linked and linked['status'] == 'cancelled':
                        missing.append({**base, 'reason': 'この日の実行分は中止済みです。'})
                        continue
                    unchanged_duration = requested is None or (
                        (linked['estimated_minutes'] if linked else source['estimated_minutes']) == requested
                        and existing and existing['start_at'] and datetime.fromisoformat(existing['end_at']) - datetime.fromisoformat(existing['start_at']) == timedelta(minutes=requested)
                    )
                    if existing and existing['start_at'] and unchanged_duration:
                        occurrence_id = linked['id'] if linked else source['id']
                        if not linked:
                            args = {'source_task_id': source['id'], 'task_id': occurrence_id, 'occurrence_date': day.isoformat()}
                            link_occurrence(args)
                            actions.append(ScheduleAction(name='link_routine_occurrence', arguments_json=json.dumps(args)))
                        occurrences.append({**base, 'state': 'existing', 'task_id': occurrence_id,
                                            'start_at': existing['start_at'], 'end_at': existing['end_at']})
                        continue
                    if duration is None:
                        missing.append({**base, 'reason': '所要時間が不明です。'})
                        continue
                    if day.weekday() not in tasks.PLANNING_WORKDAYS:
                        missing.append({**base, 'reason': '設定された作業曜日ではありません。'})
                        continue
                    connection.execute('SAVEPOINT routine_occurrence')
                    create_args = None
                    update_args = None
                    if linked:
                        occurrence_id = linked['id']
                    elif original and datetime.fromisoformat(original['start_at']).date() == day:
                        occurrence_id = source['id']
                    elif original is None and source['status'] in {'open', 'in_progress'} and (
                        not source['due_at'] or day <= datetime.fromisoformat(source['due_at']).date()
                    ) and not connection.execute(
                        'SELECT 1 FROM task_occurrence_links WHERE task_id=?', (source['id'],),
                    ).fetchone():
                        occurrence_id = source['id']
                    else:
                        create_args = {'title': source['title'], 'description': source['description'],
                                       'estimated_minutes': duration, 'priority': source['priority'], 'project_id': source['project_id']}
                        occurrence_id = tasks.create_task_record(tasks.TaskPayload(**create_args))['id']
                    if create_args is None:
                        current_minutes = connection.execute('SELECT estimated_minutes FROM tasks WHERE id=?', (occurrence_id,)).fetchone()['estimated_minutes']
                        if current_minutes != duration:
                            tasks.update_task_record(occurrence_id, tasks.TaskUpdate(estimated_minutes=duration))
                            update_args = {'task_id': occurrence_id, 'estimated_minutes': duration}
                    start = datetime.combine(day, time.min, tasks.LOCAL_TIMEZONE)
                    end = datetime.combine(day, time(23, 59, 59), tasks.LOCAL_TIMEZONE)
                    slots = tasks.find_task_slots(occurrence_id, start, end, limit=1)['slots']
                    if not slots:
                        connection.execute('ROLLBACK TO routine_occurrence')
                        connection.execute('RELEASE routine_occurrence')
                        missing.append({**base, 'reason': 'この日の空き時間・作業時間・期限内に収まりません。既存枠がある場合はそのまま維持しています。'})
                        continue
                    slot = slots[0]
                    tasks.schedule_task_record(occurrence_id, datetime.fromisoformat(slot['start_at']))
                    if update_args:
                        actions.append(ScheduleAction(name='update_task', arguments_json=json.dumps(update_args)))
                    reference = f'@{len(actions)}' if create_args else occurrence_id
                    if create_args:
                        actions.append(ScheduleAction(name='create_task', arguments_json=json.dumps(create_args, ensure_ascii=False)))
                    actions.append(ScheduleAction(name='schedule_task', arguments_json=json.dumps({'task_id': reference, 'start_at': slot['start_at']})))
                    if not linked:
                        args = {'source_task_id': source['id'], 'task_id': occurrence_id, 'occurrence_date': day.isoformat()}
                        link_occurrence(args)
                        args['task_id'] = reference
                        actions.append(ScheduleAction(name='link_routine_occurrence', arguments_json=json.dumps(args)))
                    connection.execute('RELEASE routine_occurrence')
                    occurrences.append({**base, 'state': 'scheduled', **slot})
                day += timedelta(days=1)
            check_final_conflicts()
        metadata = {'start_date': payload.start_date.isoformat(), 'through_date': payload.through_date.isoformat(),
                    'items': [{'task_id': source['id'], 'title': source['title'], 'estimated_minutes': source.get('requested_minutes', source['estimated_minutes'])} for source in sources],
                    'occurrences': occurrences, 'missing': missing}
        if not actions and not missing:
            return {'ok': True, 'state': 'unchanged', 'occurrences': occurrences,
                    'missing': missing, 'instruction': '新しい変更はありません。不足する日・所要時間だけを尋ね、既存の実行分は重複作成しないでください。'}
        if not actions:
            return {'ok': True, 'state': 'needs_input', 'start_date': payload.start_date.isoformat(),
                    'through_date': payload.through_date.isoformat(), 'occurrences': occurrences, 'missing': missing,
                    'instruction': '保存できた日はありません。missingの日の理由と、必要な所要時間や日程だけを相談してください。'}
        results = apply_actions(actions)
        return {'ok': True, 'state': 'applied', 'results': results,
                'start_date': payload.start_date.isoformat(), 'through_date': payload.through_date.isoformat(),
                'occurrences': occurrences, 'missing': missing,
                'instruction': '最終日を含むルーティンを保存しました。既存枠と新しく保存した枠を区別して日別に説明してください。missingの日は未割当です。その日だけ相談し、全日を保存済みとは述べないでください。'}
    except (ValueError, TypeError, HTTPException, sqlite3.IntegrityError) as error:
        return {'ok': False, 'error': error.detail if isinstance(error, HTTPException) else str(error),
            'instruction': '予定とタスクは変更していません。具体的な失敗理由と対象・期間・不足情報を説明してください。'}