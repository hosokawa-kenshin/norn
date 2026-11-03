import json
import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from database import database_transaction, open_database
import tasks


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
        'name': 'propose_routine',
        'description': '既存タスクを毎日のルーティンとして最終日を含めて空き時間へ配置する未反映案を作る。31日以内・最大8項目。list_tasksか現在のDB情報で対象IDを特定する。時間未指定なら既存の所要時間を引き継ぐ。「全部1時間」等の明示的な変更は各itemsにestimated_minutesと最新user発言のestimate_evidenceを指定する。既存枠の時間変更も提案だけで、了承後に適用する。所要時間不明ならその項目だけ尋ねる。案に不足日があればその日だけ相談し、配置可能分は了承後にまとめて保存する。',
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


def pending_routine_request(conversation_id: str | None) -> dict[str, Any] | None:
    with closing(open_database()) as connection:
        row = connection.execute(
            "SELECT metadata_json FROM schedule_proposals WHERE conversation_id=? AND state='pending'",
            (conversation_id,),
        ).fetchone()
    metadata = json.loads(row['metadata_json']) if row else {}
    return metadata if metadata.get('items') and metadata.get('through_date') else None


def link_occurrence(arguments: dict[str, Any]) -> dict[str, Any]:
    link = OccurrenceLink.model_validate(arguments)
    with closing(open_database()) as connection:
        allocation = connection.execute('SELECT start_at FROM task_allocations WHERE task_id = ?', (str(link.task_id),)).fetchone()
        if allocation is None or datetime.fromisoformat(allocation['start_at']).date() != link.occurrence_date:
            raise ValueError('ルーティンの実施日と作業枠が一致しません。')
        connection.execute(
            'INSERT INTO routine_occurrences (source_task_id, occurrence_date, task_id) VALUES (?, ?, ?)',
            (str(link.source_task_id), link.occurrence_date.isoformat(), str(link.task_id)),
        )
        connection.commit()
    return {'ok': True, 'source_task_id': str(link.source_task_id), 'task_id': str(link.task_id),
            'occurrence_date': link.occurrence_date.isoformat()}


def propose_routine(conversation_id: str | None, arguments: dict[str, Any], user_content: str = '') -> dict[str, Any]:
    from schedule_proposals import ProposedAction, check_final_conflicts, invalidate_pending_proposal, schedule_snapshot, store_proposal

    try:
        if not conversation_id:
            raise ValueError('提案先の会話がありません。')
        uuid.UUID(conversation_id)
        payload = RoutinePayload.model_validate(arguments)
        if payload.start_date < datetime.now(tasks.LOCAL_TIMEZONE).date():
            raise ValueError('開始日は今日以降にしてください。')
        actions = []
        occurrences = []
        missing = []
        sources = []
        with database_transaction(rollback=True) as connection:
            baseline = schedule_snapshot()
            for item in payload.items:
                row = connection.execute(
                    '''SELECT t.* FROM tasks t WHERE t.id = COALESCE(
                       (SELECT source_task_id FROM routine_occurrences WHERE task_id = ?), ?)''',
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
                        '''SELECT t.*, a.start_at, a.end_at FROM routine_occurrences r JOIN tasks t ON t.id=r.task_id
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
                            actions.append(ProposedAction(name='link_routine_occurrence', arguments_json=json.dumps(args)))
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
                        'SELECT 1 FROM routine_occurrences WHERE task_id=?', (source['id'],),
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
                        actions.append(ProposedAction(name='update_task', arguments_json=json.dumps(update_args)))
                    reference = f'@{len(actions)}' if create_args else occurrence_id
                    if create_args:
                        actions.append(ProposedAction(name='create_task', arguments_json=json.dumps(create_args, ensure_ascii=False)))
                    actions.append(ProposedAction(name='schedule_task', arguments_json=json.dumps({'task_id': reference, 'start_at': slot['start_at']})))
                    if not linked:
                        args = {'source_task_id': source['id'], 'task_id': occurrence_id, 'occurrence_date': day.isoformat()}
                        link_occurrence(args)
                        args['task_id'] = reference
                        actions.append(ProposedAction(name='link_routine_occurrence', arguments_json=json.dumps(args)))
                    connection.execute('RELEASE routine_occurrence')
                    occurrences.append({**base, 'state': 'proposed', **slot})
                day += timedelta(days=1)
            check_final_conflicts()
        metadata = {'start_date': payload.start_date.isoformat(), 'through_date': payload.through_date.isoformat(),
                    'items': [{'task_id': source['id'], 'title': source['title'], 'estimated_minutes': source.get('requested_minutes', source['estimated_minutes'])} for source in sources],
                    'occurrences': occurrences, 'missing': missing}
        if not actions and not missing:
            return {'ok': True, 'state': 'unchanged', 'occurrences': occurrences,
                    'missing': missing, 'instruction': '新しい変更はありません。不足する日・所要時間だけを尋ね、既存の実行分は重複作成しないでください。'}
        proposal_id = store_proposal(conversation_id, actions, baseline, metadata)
        return {'ok': True, 'state': 'proposed' if actions else 'needs_input', 'proposal_id': proposal_id,
                'start_date': payload.start_date.isoformat(), 'through_date': payload.through_date.isoformat(),
                'occurrences': occurrences, 'missing': missing,
                'instruction': '最終日を含むルーティンの未反映案です。既存枠と追加案を区別して日別に説明してください。missingの日は未割当です。その日だけ相談し、配置可能分の案への了承を尋ねてください。全日を保存済みとは述べないでください。'}
    except (ValueError, TypeError, HTTPException, sqlite3.IntegrityError) as error:
        invalidate_pending_proposal(conversation_id)
        return {'ok': False, 'error': error.detail if isinstance(error, HTTPException) else str(error),
            'instruction': '予定とタスクは変更しておらず、有効な提案もありません。具体的な失敗理由と対象・期間・不足情報を説明してください。「お願いします」で反映できるとは案内しないでください。'}