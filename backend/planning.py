import uuid
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

from database import now_iso, open_database

router = APIRouter()
LifecycleStatus = Literal['active', 'paused', 'completed', 'cancelled']


class GoalPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    description: str = Field(default='', max_length=4000)
    status: LifecycleStatus = 'active'
    target_date: date | None = None

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('目標名を入力してください。')
        return value


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    status: LifecycleStatus | None = None
    target_date: date | None = None

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('目標名を空にはできません。')
        return value


class ProjectPayload(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    goal_id: str | None = None
    description: str = Field(default='', max_length=4000)
    status: LifecycleStatus = 'active'
    target_date: date | None = None
    next_action: str = Field(default='', max_length=500)

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError('プロジェクト名を入力してください。')
        return value

    @field_validator('goal_id')
    @classmethod
    def clean_goal_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError('目標IDが不正です。') from error


class ProjectUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=160)
    goal_id: str | None = None
    description: str | None = Field(default=None, max_length=4000)
    status: LifecycleStatus | None = None
    target_date: date | None = None
    next_action: str | None = Field(default=None, max_length=500)

    @field_validator('title')
    @classmethod
    def clean_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not value:
            raise ValueError('プロジェクト名を空にはできません。')
        return value

    @field_validator('goal_id')
    @classmethod
    def clean_goal_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            return str(uuid.UUID(value))
        except ValueError as error:
            raise ValueError('目標IDが不正です。') from error


GOAL_PROJECT_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'list_goals',
            'description': 'NORNの目標を検索し、関連プロジェクトと進捗を確認する。',
            'parameters': {'type': 'object', 'properties': {}, 'additionalProperties': False},
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_goal',
            'description': 'ユーザーが依頼した長期・中期目標をNORNへ登録する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'target_date': {'type': 'string', 'format': 'date'},
                },
                'required': ['title'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_goal',
            'description': '検索結果で特定した目標の内容・期限・状態をユーザー依頼に従って更新する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'goal_id': {'type': 'string'},
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'status': {'type': 'string', 'enum': ['active', 'paused', 'completed', 'cancelled']},
                    'target_date': {'type': 'string', 'format': 'date'},
                },
                'required': ['goal_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_goal',
            'description': 'ユーザーが明確に依頼した目標を削除する。関連プロジェクトは未分類に戻す。',
            'parameters': {
                'type': 'object',
                'properties': {'goal_id': {'type': 'string'}},
                'required': ['goal_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'list_projects',
            'description': 'NORNのプロジェクトとタスク完了率、次のアクションを確認する。goal_idで絞り込める。',
            'parameters': {
                'type': 'object',
                'properties': {'goal_id': {'type': 'string'}},
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_project',
            'description': 'ユーザーが依頼した取り組みをプロジェクトとして登録し、分かる場合は目標へ関連付ける。新しい進行中プロジェクトなら初期タスクも作成する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'goal_id': {'type': 'string'},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'target_date': {'type': 'string', 'format': 'date'},
                    'next_action': {'type': 'string', 'maxLength': 500},
                },
                'required': ['title'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_project',
            'description': '検索結果で特定したプロジェクトの目標、内容、期限、状態、次のアクションを更新する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'project_id': {'type': 'string'},
                    'goal_id': {'type': 'string'},
                    'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                    'description': {'type': 'string', 'maxLength': 4000},
                    'status': {'type': 'string', 'enum': ['active', 'paused', 'completed', 'cancelled']},
                    'target_date': {'type': 'string', 'format': 'date'},
                    'next_action': {'type': 'string', 'maxLength': 500},
                },
                'required': ['project_id'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'delete_project',
            'description': 'ユーザーが明確に依頼したプロジェクトを削除する。関連タスクは未分類に戻す。',
            'parameters': {
                'type': 'object',
                'properties': {'project_id': {'type': 'string'}},
                'required': ['project_id'],
                'additionalProperties': False,
            },
        },
    },
]


def _normalize_id(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError(f'{label}IDが不正です。') from error


def _project_rows(connection, goal_id: str | None = None) -> list[dict[str, Any]]:
    rows = connection.execute(
        '''
        SELECT projects.*, goals.title AS goal_title,
            COUNT(tasks.id) AS task_count,
            COALESCE(SUM(CASE WHEN tasks.status = 'done' THEN 1 ELSE 0 END), 0) AS completed_task_count
        FROM projects
        LEFT JOIN goals ON goals.id = projects.goal_id
        LEFT JOIN tasks ON tasks.project_id = projects.id
        WHERE (? IS NULL OR projects.goal_id = ?)
        GROUP BY projects.id
        ORDER BY projects.updated_at DESC, projects.title COLLATE NOCASE
        ''',
        (goal_id, goal_id),
    ).fetchall()
    projects = []
    for row in rows:
        project = dict(row)
        total = project['task_count']
        completed = project['completed_task_count']
        project['progress'] = 100 if project['status'] == 'completed' else (
            round(completed / total * 100) if total else 0
        )
        projects.append(project)
    return projects


def list_goals() -> list[dict[str, Any]]:
    with closing(open_database()) as connection:
        goals = connection.execute(
            'SELECT * FROM goals ORDER BY updated_at DESC, title COLLATE NOCASE'
        ).fetchall()
        projects = _project_rows(connection)
    projects_by_goal: dict[str, list[dict[str, Any]]] = {}
    for project in projects:
        if project['goal_id']:
            projects_by_goal.setdefault(project['goal_id'], []).append(project)

    results = []
    for row in goals:
        goal = dict(row)
        goal_projects = projects_by_goal.get(goal['id'], [])
        goal['projects'] = goal_projects
        goal['project_count'] = len(goal_projects)
        goal['completed_project_count'] = sum(project['status'] == 'completed' for project in goal_projects)
        goal['progress'] = 100 if goal['status'] == 'completed' else (
            round(sum(project['progress'] for project in goal_projects) / len(goal_projects))
            if goal_projects else 0
        )
        results.append(goal)
    return results


def list_projects(goal_id: str | None = None) -> list[dict[str, Any]]:
    if goal_id is not None:
        goal_id = _normalize_id(goal_id, '目標')
    with closing(open_database()) as connection:
        return _project_rows(connection, goal_id)


def create_goal_record(payload: GoalPayload) -> dict[str, Any]:
    goal_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        connection.execute(
            '''
            INSERT INTO goals (id, title, description, status, target_date, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ''',
            (goal_id, payload.title, payload.description, payload.status,
             payload.target_date.isoformat() if payload.target_date else None, timestamp, timestamp),
        )
        connection.commit()
    return next(goal for goal in list_goals() if goal['id'] == goal_id)


def update_goal_record(goal_id: str, payload: GoalUpdate) -> dict[str, Any]:
    goal_id = _normalize_id(goal_id, '目標')
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise ValueError('更新内容を指定してください。')
    if 'target_date' in updates:
        updates['target_date'] = updates['target_date'].isoformat() if updates['target_date'] else None
    assignments = ', '.join(f'{key} = ?' for key in updates)
    with closing(open_database()) as connection:
        result = connection.execute(
            f'UPDATE goals SET {assignments}, updated_at = ? WHERE id = ?',
            (*updates.values(), now_iso(), goal_id),
        )
        connection.commit()
    if not result.rowcount:
        raise ValueError('目標が見つかりません。')
    return next(goal for goal in list_goals() if goal['id'] == goal_id)


def delete_goal_record(goal_id: str) -> bool:
    goal_id = _normalize_id(goal_id, '目標')
    with closing(open_database()) as connection:
        result = connection.execute('DELETE FROM goals WHERE id = ?', (goal_id,))
        connection.commit()
    return result.rowcount > 0


def create_project_record(payload: ProjectPayload) -> dict[str, Any]:
    project_id = str(uuid.uuid4())
    timestamp = now_iso()
    with closing(open_database()) as connection:
        if payload.goal_id and not connection.execute(
            'SELECT id FROM goals WHERE id = ?', (payload.goal_id,)
        ).fetchone():
            raise ValueError('関連付ける目標が見つかりません。')
        connection.execute(
            '''
            INSERT INTO projects
                (id, goal_id, title, description, status, target_date, next_action, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''',
            (project_id, payload.goal_id, payload.title, payload.description, payload.status,
             payload.target_date.isoformat() if payload.target_date else None,
             payload.next_action, timestamp, timestamp),
        )
        connection.commit()
    return next(project for project in list_projects() if project['id'] == project_id)


def update_project_record(project_id: str, payload: ProjectUpdate) -> dict[str, Any]:
    project_id = _normalize_id(project_id, 'プロジェクト')
    updates = payload.model_dump(exclude_unset=True)
    if not updates:
        raise ValueError('更新内容を指定してください。')
    if 'target_date' in updates:
        updates['target_date'] = updates['target_date'].isoformat() if updates['target_date'] else None
    with closing(open_database()) as connection:
        exists = connection.execute('SELECT id FROM projects WHERE id = ?', (project_id,)).fetchone()
        if exists is None:
            raise ValueError('プロジェクトが見つかりません。')
        if updates.get('goal_id') and not connection.execute(
            'SELECT id FROM goals WHERE id = ?', (updates['goal_id'],)
        ).fetchone():
            raise ValueError('関連付ける目標が見つかりません。')
        assignments = ', '.join(f'{key} = ?' for key in updates)
        connection.execute(
            f'UPDATE projects SET {assignments}, updated_at = ? WHERE id = ?',
            (*updates.values(), now_iso(), project_id),
        )
        connection.commit()
    return next(project for project in list_projects() if project['id'] == project_id)


def delete_project_record(project_id: str) -> bool:
    project_id = _normalize_id(project_id, 'プロジェクト')
    with closing(open_database()) as connection:
        result = connection.execute('DELETE FROM projects WHERE id = ?', (project_id,))
        connection.commit()
    return result.rowcount > 0


@router.get('/api/goals')
def get_goals() -> list[dict[str, Any]]:
    return list_goals()


@router.post('/api/goals', status_code=201)
def create_goal(payload: GoalPayload) -> dict[str, Any]:
    return create_goal_record(payload)


@router.patch('/api/goals/{goal_id}')
def update_goal(goal_id: str, payload: GoalUpdate) -> dict[str, Any]:
    try:
        return update_goal_record(goal_id, payload)
    except ValueError as error:
        raise HTTPException(status_code=404 if '見つかりません' in str(error) else 422, detail=str(error)) from error


@router.delete('/api/goals/{goal_id}', status_code=204)
def delete_goal(goal_id: str) -> None:
    if not delete_goal_record(goal_id):
        raise HTTPException(status_code=404, detail='目標が見つかりません。')


@router.get('/api/projects')
def get_projects(goal_id: str | None = Query(default=None)) -> list[dict[str, Any]]:
    try:
        return list_projects(goal_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post('/api/projects', status_code=201)
def create_project(payload: ProjectPayload) -> dict[str, Any]:
    try:
        return create_project_record(payload)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.patch('/api/projects/{project_id}')
def update_project(project_id: str, payload: ProjectUpdate) -> dict[str, Any]:
    try:
        return update_project_record(project_id, payload)
    except ValueError as error:
        code = 404 if '見つかりません' in str(error) else 422
        raise HTTPException(status_code=code, detail=str(error)) from error


@router.delete('/api/projects/{project_id}', status_code=204)
def delete_project(project_id: str) -> None:
    if not delete_project_record(project_id):
        raise HTTPException(status_code=404, detail='プロジェクトが見つかりません。')


def execute_goal_project_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == 'list_goals':
            if arguments:
                raise ValueError('引数は不要です。')
            return {'ok': True, 'goals': list_goals()}
        if name == 'create_goal':
            if not set(arguments) <= {'title', 'description', 'target_date'}:
                raise ValueError('許可されていない目標項目が含まれています。')
            goal = create_goal_record(GoalPayload(**arguments))
            return {'ok': True, 'goal': goal, 'summary': f'目標「{goal["title"]}」を登録しました。'}
        if name == 'update_goal':
            if 'goal_id' not in arguments or not set(arguments) <= {
                'goal_id', 'title', 'description', 'status', 'target_date'
            }:
                raise ValueError('目標IDと更新項目を指定してください。')
            goal = update_goal_record(arguments['goal_id'], GoalUpdate(**{
                key: value for key, value in arguments.items() if key != 'goal_id'
            }))
            return {'ok': True, 'goal': goal, 'summary': f'目標「{goal["title"]}」を更新しました。'}
        if name == 'delete_goal':
            if set(arguments) != {'goal_id'}:
                raise ValueError('削除する目標IDを指定してください。')
            removed = delete_goal_record(arguments['goal_id'])
            return {'ok': True, 'summary': '目標を削除しました。' if removed else '目標はすでにありません。'}
        if name == 'list_projects':
            if not set(arguments) <= {'goal_id'}:
                raise ValueError('許可されていない検索条件です。')
            return {'ok': True, 'projects': list_projects(arguments.get('goal_id'))}
        if name == 'create_project':
            if not set(arguments) <= {'title', 'goal_id', 'description', 'target_date', 'next_action'}:
                raise ValueError('許可されていないプロジェクト項目が含まれています。')
            project = create_project_record(ProjectPayload(**arguments))
            return {'ok': True, 'project': project, 'summary': f'プロジェクト「{project["title"]}」を登録しました。'}
        if name == 'update_project':
            if 'project_id' not in arguments or not set(arguments) <= {
                'project_id', 'goal_id', 'title', 'description', 'status', 'target_date', 'next_action'
            }:
                raise ValueError('プロジェクトIDと更新項目を指定してください。')
            project = update_project_record(arguments['project_id'], ProjectUpdate(**{
                key: value for key, value in arguments.items() if key != 'project_id'
            }))
            return {
                'ok': True,
                'project': project,
                'summary': f'プロジェクト「{project["title"]}」を更新しました。',
            }
        if name == 'delete_project':
            if set(arguments) != {'project_id'}:
                raise ValueError('削除するプロジェクトIDを指定してください。')
            removed = delete_project_record(arguments['project_id'])
            return {
                'ok': True,
                'summary': 'プロジェクトを削除しました。関連タスクは未分類に戻しました。'
                if removed else 'プロジェクトはすでにありません。',
            }
        return {'ok': False, 'error': '許可されていない目標・プロジェクト操作です。'}
    except ValueError as error:
        return {'ok': False, 'error': str(error)}


WEEKLY_GOAL_REVIEW_INTERVAL = timedelta(days=7)


def get_goals_due_for_review(now: datetime | None = None, limit: int = 8) -> list[dict[str, Any]]:
    now = now or datetime.now(timezone.utc)
    cutoff = (now - WEEKLY_GOAL_REVIEW_INTERVAL).isoformat(timespec='seconds')
    with closing(open_database()) as connection:
        goals = connection.execute(
            '''
            SELECT * FROM goals
            WHERE status = 'active'
                AND COALESCE(last_reviewed_at, created_at) <= ?
            ORDER BY target_date IS NULL, target_date, created_at
            LIMIT ?
            ''',
            (cutoff, max(1, min(limit, 20))),
        ).fetchall()
        reviews: list[dict[str, Any]] = []
        for row in goals:
            goal = dict(row)
            projects = _project_rows(connection, goal['id'])
            for project in projects:
                project['tasks'] = [dict(task) for task in connection.execute(
                    '''
                    SELECT title, status, due_at FROM tasks
                    WHERE project_id = ? ORDER BY due_at IS NULL, due_at, created_at LIMIT 12
                    ''',
                    (project['id'],),
                ).fetchall()]
            goal['projects'] = projects
            goal['project_count'] = len(projects)
            goal['completed_project_count'] = sum(project['status'] == 'completed' for project in projects)
            goal['progress'] = 100 if goal['status'] == 'completed' else (
                round(sum(project['progress'] for project in projects) / len(projects))
                if projects else 0
            )
            reviews.append(goal)
    return reviews


def format_goal_review_context(goals: list[dict[str, Any]], today: date | None = None) -> str:
    if not goals:
        return ''
    today = today or datetime.now(timezone.utc).date()
    lines = [
        f'週次目標レビューの対象があります（基準日: {today.isoformat()}）。今回の返答に短い振り返りを含めてください。',
        '観測できるタスク状態・期限・進捗だけを根拠にし、理由や実績を推測しないでください。',
        '次の一歩は登録済みのnext_actionまたは未完了タスクから提案し、依頼されていない変更やタスク作成は行わないでください。',
        '根拠が足りなければ順調/遅延を断定せず、その旨を率直に伝えてください。',
    ]
    for goal in goals:
        target = goal['target_date'] or '期限なし'
        lines.append(
            f'目標「{goal["title"]}」: 進捗{goal["progress"]}%、期限{target}、'
            f'関連プロジェクト{goal["project_count"]}件。'
        )
        for project in goal['projects']:
            lines.append(
                f'  プロジェクト「{project["title"]}」: {project["status"]}、進捗{project["progress"]}%、'
                f'期限{project["target_date"] or "期限なし"}、次のアクション: {project["next_action"] or "未設定"}。'
            )
            for task in project['tasks']:
                lines.append(
                    f'    タスク「{task["title"]}」: {task["status"]}、期限{task["due_at"] or "期限なし"}。'
                )
    return '\n'.join(lines)


def mark_goals_reviewed(goal_ids: list[str], reviewed_at: str | None = None) -> None:
    normalized_ids = [_normalize_id(goal_id, '目標') for goal_id in goal_ids]
    if not normalized_ids:
        return
    timestamp = reviewed_at or now_iso()
    with closing(open_database()) as connection:
        connection.executemany(
            'UPDATE goals SET last_reviewed_at = ? WHERE id = ? AND status = \'active\'',
            [(timestamp, goal_id) for goal_id in normalized_ids],
        )
        connection.commit()
