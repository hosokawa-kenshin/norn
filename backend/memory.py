import array
import math
import re
import sqlite3
import threading
import uuid
from contextlib import closing
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from database import now_iso, open_database
from settings import logger

router = APIRouter()
MEMORY_CATEGORIES = {'fact', 'preference', 'episode'}
MEMORY_SOURCES = {'explicit', 'inferred'}
EMBEDDING_MODEL = 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MIN_MEMORY_SIMILARITY = 0.72
_embedding_model: Any | None = None
_embedding_unavailable = False
_embedding_lock = threading.Lock()
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
                '日本語の言い換えはquery_variantsに最大4件含める。言い換えは検索意図と人物・場所・時期などの事実を変えない。'
                '候補の類似度は一致の保証ではないため、返却された記憶と質問の事実が合うものだけ使う。'
            ),
            'parameters': {
                'type': 'object',
                'properties': {
                    'query': {'type': 'string', 'minLength': 1, 'maxLength': 300},
                    'query_variants': {
                        'type': 'array',
                        'items': {
                            'type': 'string',
                            'maxLength': 300,
                            'description': '検索意図と人物・場所・時期などの事実を保った言い換え。',
                        },
                        'maxItems': 4,
                    },
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


def embed_texts(texts: list[str]) -> list[bytes] | None:
    global _embedding_model, _embedding_unavailable
    if not texts or _embedding_unavailable:
        return [] if not texts else None
    try:
        with _embedding_lock:
            if _embedding_model is None:
                from fastembed import TextEmbedding

                _embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)
            vectors = list(_embedding_model.embed(texts))
        return [array.array('f', vector).tobytes() for vector in vectors]
    except Exception as error:
        _embedding_unavailable = True
        logger.warning('Local memory embeddings unavailable: %s', type(error).__name__)
        return None


def cosine_similarity(left: bytes, right: bytes) -> float:
    left_values = array.array('f')
    right_values = array.array('f')
    left_values.frombytes(left)
    right_values.frombytes(right)
    if len(left_values) != len(right_values) or not left_values:
        return 0.0
    dot = sum(left_value * right_value for left_value, right_value in zip(left_values, right_values))
    left_norm = math.sqrt(sum(value * value for value in left_values))
    right_norm = math.sqrt(sum(value * value for value in right_values))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


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
    vectors = embed_texts([f'{category}: {content}'])
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
        if vectors:
            connection.execute(
                '''
                INSERT INTO memory_embeddings (memory_id, model_name, vector, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_name = excluded.model_name,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                ''',
                (memory_id, EMBEDDING_MODEL, vectors[0], timestamp),
            )
        else:
            connection.execute('DELETE FROM memory_embeddings WHERE memory_id = ?', (memory_id,))
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


def search_memory_records(
    query: str,
    category: str | None = None,
    limit: int = 5,
    query_variants: list[str] | None = None,
) -> list[dict[str, Any]]:
    queries = list(dict.fromkeys(
        ' '.join(value.split()) for value in [query, *(query_variants or [])] if value.strip()
    ))[:5]
    if not queries:
        raise ValueError('検索語を指定してください。')
    if category is not None and category not in MEMORY_CATEGORIES:
        raise ValueError('記憶のカテゴリが不正です。')
    bounded_limit = max(1, min(int(limit), 8))
    query_terms = [_search_terms(value.casefold()) for value in queries]
    with closing(open_database()) as connection:
        rows = connection.execute(
            '''
            SELECT memories.*, memory_embeddings.model_name AS embedding_model,
                memory_embeddings.vector AS embedding_vector
            FROM memories LEFT JOIN memory_embeddings ON memory_embeddings.memory_id = memories.id
            WHERE (? IS NULL OR memories.category = ?)
            ORDER BY memories.updated_at DESC
            ''',
            (category, category),
        ).fetchall()

    if not rows:
        return []

    query_vectors = embed_texts(queries)
    missing_rows = [
        dict(row) for row in rows
        if row['embedding_vector'] is None or row['embedding_model'] != EMBEDDING_MODEL
    ]
    passage_vectors = embed_texts([
        f"{item['category']}: {item['content']}" for item in missing_rows
    ])
    generated_vectors = {
        item['id']: vector for item, vector in zip(missing_rows, passage_vectors or [])
    }
    if generated_vectors:
        timestamp = now_iso()
        with closing(open_database()) as connection:
            connection.executemany(
                '''
                INSERT INTO memory_embeddings (memory_id, model_name, vector, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_name = excluded.model_name,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                ''',
                [(memory_id, EMBEDDING_MODEL, vector, timestamp)
                 for memory_id, vector in generated_vectors.items()],
            )
            connection.commit()

    scored: list[tuple[float, str, dict[str, Any]]] = []
    for row in rows:
        item = dict(row)
        stored_vector = item.pop('embedding_vector')
        item.pop('embedding_model')
        memory_vector = generated_vectors.get(item['id'], stored_vector)
        semantic_score = 0.0
        if memory_vector and query_vectors:
            semantic_score = max(cosine_similarity(vector, memory_vector) for vector in query_vectors)

        content = item['content'].casefold()
        lexical_coverage = 0.0
        exact_match = False
        for normalized_query, terms in zip(queries, query_terms):
            shared_terms = terms & _search_terms(content)
            lexical_coverage = max(
                lexical_coverage,
                len(shared_terms) / max(1, len(terms)),
            )
            exact_match = exact_match or normalized_query.casefold() in content

        if semantic_score >= MIN_MEMORY_SIMILARITY:
            score = semantic_score + min(0.08, lexical_coverage * 0.08)
        elif exact_match:
            score = 1.0
        elif lexical_coverage >= 0.55:
            score = MIN_MEMORY_SIMILARITY + min(0.05, lexical_coverage * 0.05)
        else:
            continue
        item['similarity'] = round(score, 4)
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
        vectors = embed_texts([f'{category}: {content}'])
        connection.execute(
            'UPDATE memories SET category = ?, content = ?, source_type = ?, updated_at = ? WHERE id = ?',
            (category, content, source_type, now_iso(), memory_id),
        )
        if vectors:
            connection.execute(
                '''
                INSERT INTO memory_embeddings (memory_id, model_name, vector, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(memory_id) DO UPDATE SET
                    model_name = excluded.model_name,
                    vector = excluded.vector,
                    updated_at = excluded.updated_at
                ''',
                (memory_id, EMBEDDING_MODEL, vectors[0], now_iso()),
            )
        else:
            connection.execute('DELETE FROM memory_embeddings WHERE memory_id = ?', (memory_id,))
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
            if not set(arguments) <= {'query', 'query_variants', 'category', 'limit'} or 'query' not in arguments:
                raise ValueError('記憶の検索語を指定してください。')
            query_variants = arguments.get('query_variants', [])
            if not isinstance(query_variants, list) or len(query_variants) > 4 or any(
                not isinstance(variant, str) or len(variant) > 300 for variant in query_variants
            ):
                raise ValueError('言い換え検索語は300文字以内で4件まで指定できます。')
            memories = search_memory_records(
                arguments['query'], arguments.get('category'), arguments.get('limit', 5), query_variants
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
