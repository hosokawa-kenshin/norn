import re
import sqlite3
import uuid
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from database import now_iso, open_database

router = APIRouter()
MEMORY_CATEGORIES = {'fact', 'preference', 'episode'}
MEMORY_SOURCES = {'explicit', 'inferred'}
SENSITIVE_MEMORY_PATTERN = re.compile(
    r'\b(?:password|passphrase|api[_ -]?key|access[_ -]?token|secret|credential)\b\s*[:=]\s*\S+'
    r'|(?:パスワード|APIキー|アクセストークン|認証情報)\s*(?:は|[:=])?\s*\S+'
    r'|\b(?:\d[ -]*?){13,19}\b',
    re.IGNORECASE,
)

MEMORY_TOOLS = [
    {
        'type': 'function',
        'function': {
            'name': 'save_memory',
            'description': (
                'ユーザーが明示的に記憶を依頼した内容を保存する。通常会話の自動抽出には使わず、'
                '一時的な用事や些細な発言は保存しない。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'category': {'type': 'string', 'enum': ['fact', 'preference', 'episode']},
                    'content': {'type': 'string', 'minLength': 1, 'maxLength': 600},
                    'source_type': {'type': 'string', 'enum': ['explicit', 'inferred']},
                },
                'required': ['category', 'content', 'source_type'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'search_memories',
            'description': (
                '昔の出来事、人、好み、ユーザーが以前話した内容を答える前に記憶を検索する。'
                '日本語の言い換えや呼び方も考慮して検索語を選ぶ。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                    'category': {'type': 'string', 'enum': ['fact', 'preference', 'episode']},
                    'limit': {'type': 'integer', 'minimum': 1, 'maximum': 8},
                },
                'required': ['query'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'update_memory',
            'description': 'ユーザーが記憶内容を訂正したとき、対象を検索してその記憶を修正する。',
            'parameters': {
                'type': 'object',
                'properties': {
                    'memory_id': {'type': 'string'},
                    'content': {'type': 'string', 'minLength': 1, 'maxLength': 600},
                    'category': {'type': 'string', 'enum': ['fact', 'preference', 'episode']},
                    'source_type': {'type': 'string', 'enum': ['explicit', 'inferred']},
                },
                'required': ['memory_id', 'content'],
                'additionalProperties': False,
            },
        },
    },
    {
        'type': 'function',
        'function': {
            'name': 'forget_memory',
            'description': 'ユーザーが忘れるよう頼んだ記憶を、検索結果から対象を特定して削除する。',
            'parameters': {
                'type': 'object',
                'properties': {'memory_id': {'type': 'string'}},
                'required': ['memory_id'],
                'additionalProperties': False,
            },
        },
    },
]


def _validate_memory(category: str, content: str, source_type: str) -> tuple[str, str, str]:
    normalized_content = ' '.join(content.split())
    if category not in MEMORY_CATEGORIES:
        raise ValueError('記憶のカテゴリが不正です。')
    if source_type not in MEMORY_SOURCES:
        raise ValueError('記憶の出典区分が不正です。')
    if not normalized_content or len(normalized_content) > 600:
        raise ValueError('記憶は1〜600文字で指定してください。')
    if SENSITIVE_MEMORY_PATTERN.search(normalized_content):
        raise ValueError('認証情報や決済情報は記憶できません。')
    return category, normalized_content, source_type


def save_memory_record(
    category: str,
    content: str,
    source_type: str,
    source_conversation_id: str | None = None,
) -> dict[str, Any]:
    category, content, source_type = _validate_memory(category, content, source_type)
    timestamp = now_iso()
    with closing(open_database()) as connection:
        existing = connection.execute(
            'SELECT id FROM memories WHERE category = ? AND lower(content) = lower(?)',
            (category, content),
        ).fetchone()
        if existing:
            connection.execute(
                '''
                UPDATE memories SET content = ?, source_type = ?, source_conversation_id = ?, updated_at = ?
                WHERE id = ?
                ''',
                (content, source_type, source_conversation_id, timestamp, existing['id']),
            )
            memory_id = existing['id']
        else:
            memory_id = str(uuid.uuid4())
            connection.execute(
                '''
                INSERT INTO memories
                    (id, category, content, source_type, source_conversation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ''',
                (memory_id, category, content, source_type, source_conversation_id, timestamp, timestamp),
            )
        connection.commit()
        row = connection.execute('SELECT * FROM memories WHERE id = ?', (memory_id,)).fetchone()
    return dict(row)


def _search_terms(value: str) -> set[str]:
    terms: set[str] = set()
    for token in re.findall(r'[A-Za-z0-9][A-Za-z0-9_-]*|[ぁ-んァ-ヶ一-龯ー]+', value.casefold()):
        if len(token) < 2:
            continue
        terms.add(token)
        if re.search(r'[ぁ-んァ-ヶ一-龯]', token):
            terms.update(token[index:index + 2] for index in range(len(token) - 1))
    return terms


def search_memory_records(query: str, category: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
    normalized_query = ' '.join(query.split()).casefold()
    if not normalized_query:
        raise ValueError('検索語を指定してください。')
    if category is not None and category not in MEMORY_CATEGORIES:
        raise ValueError('記憶のカテゴリが不正です。')
    bounded_limit = max(1, min(int(limit), 8))
    query_terms = _search_terms(normalized_query)
    with closing(open_database()) as connection:
        rows = connection.execute(
            'SELECT * FROM memories WHERE (? IS NULL OR category = ?) ORDER BY updated_at DESC',
            (category, category),
        ).fetchall()

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        item = dict(row)
        content = item['content'].casefold()
        shared_terms = query_terms & _search_terms(content)
        score = len(shared_terms) * 2
        if normalized_query in content:
            score += 10
        if score:
            scored.append((score, item['updated_at'], item))
    scored.sort(key=lambda result: (result[0], result[1]), reverse=True)
    return [result[2] for result in scored[:bounded_limit]]


def update_memory_record(
    memory_id: str,
    content: str,
    category: str | None = None,
    source_type: str | None = None,
) -> dict[str, Any]:
    try:
        memory_id = str(uuid.UUID(memory_id))
    except ValueError as error:
        raise ValueError('記憶IDが不正です。') from error
    with closing(open_database()) as connection:
        row = connection.execute('SELECT * FROM memories WHERE id = ?', (memory_id,)).fetchone()
        if row is None:
            raise ValueError('記憶が見つかりません。')
        category = category or row['category']
        source_type = source_type or 'explicit'
        category, content, source_type = _validate_memory(category, content, source_type)
        connection.execute(
            'UPDATE memories SET category = ?, content = ?, source_type = ?, updated_at = ? WHERE id = ?',
            (category, content, source_type, now_iso(), memory_id),
        )
        connection.commit()
        updated = connection.execute('SELECT * FROM memories WHERE id = ?', (memory_id,)).fetchone()
    return dict(updated)


def forget_memory_record(memory_id: str) -> bool:
    try:
        memory_id = str(uuid.UUID(memory_id))
    except ValueError as error:
        raise ValueError('記憶IDが不正です。') from error
    with closing(open_database()) as connection:
        result = connection.execute('DELETE FROM memories WHERE id = ?', (memory_id,))
        connection.commit()
    return result.rowcount > 0


def execute_memory_tool(
    name: str,
    arguments: dict[str, Any],
    source_conversation_id: str | None = None,
) -> dict[str, Any]:
    try:
        if name == 'save_memory':
            if set(arguments) != {'category', 'content', 'source_type'}:
                raise ValueError('記憶のカテゴリ・内容・出典区分を指定してください。')
            memory = save_memory_record(**arguments, source_conversation_id=source_conversation_id)
            return {'ok': True, 'memory': memory, 'summary': '大切なことを記憶しました。'}

        if name == 'search_memories':
            if not set(arguments) <= {'query', 'category', 'limit'} or 'query' not in arguments:
                raise ValueError('記憶の検索語を指定してください。')
            memories = search_memory_records(
                arguments['query'], arguments.get('category'), arguments.get('limit', 5)
            )
            return {'ok': True, 'memories': memories, 'count': len(memories)}

        if name == 'update_memory':
            if not {'memory_id', 'content'} <= set(arguments) or not set(arguments) <= {
                'memory_id', 'content', 'category', 'source_type'
            }:
                raise ValueError('記憶IDと訂正後の内容を指定してください。')
            memory = update_memory_record(
                arguments['memory_id'], arguments['content'],
                arguments.get('category'), arguments.get('source_type'),
            )
            return {'ok': True, 'memory': memory, 'summary': '記憶を訂正しました。'}

        if name == 'forget_memory':
            if set(arguments) != {'memory_id'}:
                raise ValueError('削除する記憶IDを指定してください。')
            removed = forget_memory_record(arguments['memory_id'])
            return {
                'ok': True,
                'summary': '記憶を忘れました。' if removed else 'その記憶はすでにありません。',
            }
        return {'ok': False, 'error': '許可されていない記憶操作です。'}
    except ValueError as error:
        return {'ok': False, 'error': str(error)}
    except sqlite3.Error:
        return {'ok': False, 'error': '記憶を保存できませんでした。'}


@router.get('/api/memories')
def list_memories(limit: int = Query(default=100, ge=1, le=500)) -> list[dict[str, Any]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            'SELECT * FROM memories ORDER BY updated_at DESC LIMIT ?', (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


@router.delete('/api/memories/{memory_id}', status_code=204)
def delete_memory(memory_id: str) -> None:
    removed = forget_memory_record(memory_id)
    if not removed:
        raise HTTPException(status_code=404, detail='記憶が見つかりません。')
