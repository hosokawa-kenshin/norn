import json
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_conversation_messages, now_iso, open_database
from events import SCHEDULE_TOOLS, execute_schedule_tool
from tasks import TASK_TOOLS, execute_task_tool
from settings import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, MAX_MESSAGE_LENGTH, logger

router = APIRouter()
LOCAL_TIMEZONE = ZoneInfo('Asia/Tokyo')
SYSTEM_PROMPT = (
    'あなたはNORNという日本語のパーソナルアシスタントです。'
    '予定やタスクを整理し、根拠と不確実性を明確にして簡潔に回答してください。'
    'NORN内の予定の検索・追加・変更・削除は、ユーザーから明確に依頼されたら承認を求めず予定ツールで実行してください。'
    '対象や日時など実行に必要な情報が曖昧または不足している場合に限り、その情報を質問してください。'
    '変更・削除の対象が不明な場合は予定検索ツールを使って特定してください。'
    '予定操作の結果はツールの戻り値に従って報告し、失敗した操作を成功したと述べてはいけません。'
    'NORN内のタスク検索・追加・更新・完了・削除は、ユーザーから明確に依頼されたら承認なしでタスクツールから実行してください。'
    '期限や所要時間が示されていない場合は推測で設定せず、未設定のままにしてください。'
    'タスク操作の結果もツールの戻り値に従って報告してください。'
    'ユーザーの確認なしに外部操作を実行したと主張してはいけません。'
)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


async def generate_llm_reply(messages: list[dict[str, str]]) -> str:
    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    local_now = datetime.now(LOCAL_TIMEZONE).isoformat(timespec='minutes')
    llm_messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': f'{SYSTEM_PROMPT}\n現在日時（Asia/Tokyo）: {local_now}'},
        *messages,
    ]
    completed_actions: list[str] = []
    seen_tool_calls: set[tuple[str, str]] = set()

    for _ in range(5):
        payload = {
            'model': LLM_MODEL,
            'messages': llm_messages,
            'tools': [*SCHEDULE_TOOLS, *TASK_TOOLS],
            'tool_choice': 'auto',
            'temperature': 0.2,
        }
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
                response = await client.post(
                    f'{LLM_BASE_URL}/chat/completions',
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException as error:
            logger.warning('LLM request timed out')
            if completed_actions:
                return '操作を実行しました。' + '。'.join(completed_actions)
            raise HTTPException(status_code=504, detail='LLMの応答がタイムアウトしました。') from error
        except httpx.HTTPError as error:
            logger.warning('LLM connection failed: %s', type(error).__name__)
            if completed_actions:
                return '操作を実行しました。' + '。'.join(completed_actions)
            raise HTTPException(status_code=502, detail='LLM接続に失敗しました。接続設定を確認してください。') from error

        if response.is_error:
            logger.warning('LLM returned HTTP %s', response.status_code)
            if completed_actions:
                return '操作を実行しました。' + '。'.join(completed_actions)
            raise HTTPException(status_code=502, detail=f'LLMがエラーを返しました (HTTP {response.status_code})')

        try:
            assistant_message = response.json()['choices'][0]['message']
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise HTTPException(status_code=502, detail='LLMから読み取れる応答がありませんでした。') from error

        tool_calls = assistant_message.get('tool_calls') or []
        if not tool_calls:
            content = assistant_message.get('content')
            if isinstance(content, str) and content.strip():
                return content.strip()
            if completed_actions:
                return '操作を実行しました。' + '。'.join(completed_actions)
            raise HTTPException(status_code=502, detail='LLMから空の応答が返されました。')

        llm_messages.append({
            'role': 'assistant',
            'content': assistant_message.get('content'),
            'tool_calls': tool_calls,
        })
        for tool_call in tool_calls:
            function = tool_call.get('function') or {}
            name = function.get('name', '')
            raw_arguments = function.get('arguments') or '{}'
            call_id = tool_call.get('id') or str(uuid.uuid4())
            call_key = (name, raw_arguments)
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError('ツール引数はオブジェクトである必要があります。')
                mutating_tools = {
                    'create_event', 'update_event', 'delete_event',
                    'create_task', 'update_task', 'complete_task', 'delete_task',
                }
                if name in mutating_tools and call_key in seen_tool_calls:
                    result = {'ok': False, 'error': '同じツール呼び出しを重複実行しませんでした。'}
                else:
                    if name in mutating_tools:
                        seen_tool_calls.add(call_key)
                    task_tools = {'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task'}
                    result = (
                        execute_task_tool(name, arguments)
                        if name in task_tools
                        else execute_schedule_tool(name, arguments)
                    )
            except (json.JSONDecodeError, TypeError, ValueError) as error:
                result = {'ok': False, 'error': f'ツール引数が不正です: {error}'}

            if result.get('ok') and result.get('summary'):
                completed_actions.append(result['summary'])
            llm_messages.append({
                'role': 'tool',
                'tool_call_id': call_id,
                'content': json.dumps(result, ensure_ascii=False),
            })

    if completed_actions:
        return '操作を実行しました。' + '。'.join(completed_actions)
    raise HTTPException(status_code=502, detail='Geminiが予定操作を完了できませんでした。')


@router.get('/api/conversations')
def list_conversations() -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            'SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50'
        ).fetchall()
    return [dict(row) for row in rows]


@router.get('/api/conversations/{conversation_id}/messages')
def list_messages(conversation_id: str) -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        conversation = connection.execute(
            'SELECT id FROM conversations WHERE id = ?', (conversation_id,)
        ).fetchone()
        if conversation is None:
            raise HTTPException(status_code=404, detail='会話が見つかりません。')
        rows = connection.execute(
            'SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id',
            (conversation_id,),
        ).fetchall()
    return [dict(row) for row in rows]


@router.post('/api/chat')
async def chat(request: ChatRequest) -> dict[str, Any]:
    if not LLM_MODEL:
        raise HTTPException(
            status_code=503,
            detail='LLMが未設定です。.envでLLM_MODELと接続情報を設定してください。',
        )

    content = request.content.strip()
    if not content:
        raise HTTPException(status_code=422, detail='メッセージを入力してください。')

    conversation_id = request.conversation_id or str(uuid.uuid4())
    try:
        uuid.UUID(conversation_id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail='会話IDが不正です。') from error

    if request.conversation_id:
        with closing(open_database()) as connection:
            exists = connection.execute(
                'SELECT id FROM conversations WHERE id = ?', (conversation_id,)
            ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail='会話が見つかりません。')

    history = get_conversation_messages(conversation_id) if request.conversation_id else []
    history.append({'role': 'user', 'content': content})
    reply = await generate_llm_reply(history)
    timestamp = now_iso()

    with closing(open_database()) as connection:
        connection.execute(
            '''
            INSERT INTO conversations (id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET updated_at = excluded.updated_at
            ''',
            (conversation_id, content[:48], timestamp, timestamp),
        )
        connection.executemany(
            'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
            [
                (conversation_id, 'user', content, timestamp),
                (conversation_id, 'assistant', reply, timestamp),
            ],
        )
        connection.commit()

    return {
        'conversation_id': conversation_id,
        'user_message': {'role': 'user', 'content': content, 'created_at': timestamp},
        'assistant_message': {'role': 'assistant', 'content': reply, 'created_at': timestamp},
    }
