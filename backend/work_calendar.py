import sqlite3
import uuid
from contextlib import closing
from datetime import date, datetime, time, timedelta
from typing import Any, Literal

import jpholiday
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

from database import database_transaction, now_iso, open_database
from settings import PLANNING_DAY_END, PLANNING_DAY_START, PLANNING_WORKDAYS

router = APIRouter()

ExceptionKind = Literal['paid_leave', 'compensatory_leave', 'other_day_off', 'workday']


class WorkCalendarExceptionPayload(BaseModel):
    start_date: date
    end_date: date
    kind: ExceptionKind
    label: str = Field(default='', max_length=120)
    start_time: time | None = None
    end_time: time | None = None

    @model_validator(mode='after')
    def validate_exception(self):
        if self.end_date < self.start_date:
            raise ValueError('終了日は開始日以降にしてください。')
        if (self.end_date - self.start_date).days > 365:
            raise ValueError('例外期間は最大366日です。')
        if self.kind == 'workday' and ((self.start_time is None) != (self.end_time is None)):
            raise ValueError('勤務時間を上書きする場合は開始・終了の両方を指定してください。')
        if self.kind != 'workday' and (self.start_time is not None or self.end_time is not None):
            raise ValueError('勤務時間を指定できるのは臨時勤務日のみです。')
        if self.start_time is not None and self.end_time is not None and self.end_time <= self.start_time:
            raise ValueError('勤務終了時刻は開始時刻より後にしてください。')
        return self


def load_day_overrides(start: date, end: date) -> dict[str, dict[str, Any]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            '''SELECT day, kind, label, start_time, end_time
               FROM work_calendar_exceptions WHERE day >= ? AND day <= ?''',
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return {row['day']: dict(row) for row in rows}


def working_hours_for_day(
    day: date,
    start: time,
    end: time,
    workdays: frozenset[int],
    overrides: dict[str, dict[str, Any]] | None = None,
) -> tuple[time, time] | None:
    key = day.isoformat()
    override = overrides.get(key) if overrides is not None else load_day_overrides(day, day).get(key)
    if override is not None:
        if override['kind'] != 'workday':
            return None
        if override['start_time'] and override['end_time']:
            return time.fromisoformat(override['start_time']), time.fromisoformat(override['end_time'])
        return start, end
    if day.weekday() not in workdays or jpholiday.is_holiday(day):
        return None
    return start, end


def is_workday(
    day: date,
    start: time,
    end: time,
    workdays: frozenset[int],
) -> bool:
    return working_hours_for_day(day, start, end, workdays) is not None


def exception_rows(start: date | None = None, end: date | None = None) -> list[dict[str, Any]]:
    conditions = []
    parameters: list[str] = []
    if start is not None:
        conditions.append('day >= ?')
        parameters.append(start.isoformat())
    if end is not None:
        conditions.append('day <= ?')
        parameters.append(end.isoformat())
    query = 'SELECT day, group_id, kind, label, start_time, end_time FROM work_calendar_exceptions'
    if conditions:
        query += ' WHERE ' + ' AND '.join(conditions)
    query += ' ORDER BY day'
    with closing(open_database()) as connection:
        return [dict(row) for row in connection.execute(query, parameters).fetchall()]


@router.get('/api/work-calendar')
def get_work_calendar() -> dict[str, Any]:
    return {
        'timezone': 'Asia/Tokyo',
        'work_start': PLANNING_DAY_START,
        'work_end': PLANNING_DAY_END,
        'workdays': sorted(PLANNING_WORKDAYS),
        'holiday_source': 'jpholiday',
        'holiday_note': '日本の祝日法に基づくライブラリ内データを使用します。法改正や将来の公式発表に合わせるには依存ライブラリの更新が必要です。',
        'exceptions': exception_rows(),
    }


@router.post('/api/work-calendar/exceptions', status_code=201)
def create_work_calendar_exception(payload: WorkCalendarExceptionPayload) -> dict[str, Any]:
    group_id = str(uuid.uuid4())
    timestamp = now_iso()
    days = [
        (payload.start_date + timedelta(days=offset)).isoformat()
        for offset in range((payload.end_date - payload.start_date).days + 1)
    ]
    try:
        with database_transaction() as connection:
            placeholders = ','.join('?' for _ in days)
            existing = connection.execute(
                f'SELECT day FROM work_calendar_exceptions WHERE day IN ({placeholders})',
                days,
            ).fetchall()
            if existing:
                raise HTTPException(
                    status_code=409,
                    detail=f'例外が既に登録されています: {existing[0]["day"]}',
                )
            connection.executemany(
                '''INSERT INTO work_calendar_exceptions
                   (day, group_id, kind, label, start_time, end_time, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)''',
                [
                    (day, group_id, payload.kind, payload.label.strip() or payload.kind,
                     payload.start_time.strftime('%H:%M') if payload.start_time else None,
                     payload.end_time.strftime('%H:%M') if payload.end_time else None, timestamp)
                    for day in days
                ],
            )
    except sqlite3.IntegrityError as error:
        raise HTTPException(status_code=409, detail='日付例外を登録できませんでした。') from error
    return {
        'group_id': group_id,
        'start_date': payload.start_date.isoformat(),
        'end_date': payload.end_date.isoformat(),
        'kind': payload.kind,
        'label': payload.label.strip() or payload.kind,
        'start_time': payload.start_time.strftime('%H:%M') if payload.start_time else None,
        'end_time': payload.end_time.strftime('%H:%M') if payload.end_time else None,
    }


@router.delete('/api/work-calendar/exceptions/{group_id}', status_code=204)
def delete_work_calendar_exception(group_id: str) -> None:
    try:
        group_id = str(uuid.UUID(group_id))
    except ValueError as error:
        raise HTTPException(status_code=422, detail='例外IDが不正です。') from error
    with database_transaction() as connection:
        result = connection.execute(
            'DELETE FROM work_calendar_exceptions WHERE group_id=?', (group_id,),
        )
        if result.rowcount == 0:
            raise HTTPException(status_code=404, detail='勤務日例外が見つかりません。')
