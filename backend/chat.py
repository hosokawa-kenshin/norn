import asyncio
import json
import re
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import get_conversation_messages, now_iso, open_database
from events import SCHEDULE_TOOLS, execute_schedule_tool
from memory import MEMORY_TOOLS, execute_memory_tool, save_memory_record
from planning import GOAL_PROJECT_TOOLS, execute_goal_project_tool
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
    '見積時間付きタスクを新規作成した場合は、期限があれば期限まで、期限がなければ現在から5営業日以内の空き時間を検索し、最も早い候補へ自動で割り当ててください。'
    'ユーザーから特定タスクの時間割当や再割当を頼まれた場合も、空き時間を検索して最適候補へ割り当ててください。空きがなければ未割当のまま理由を伝えてください。'
    '見積時間がなく時間枠を決められない場合は、見積時間だけを質問してください。'
    'タスク操作の結果もツールの戻り値に従って報告してください。'
    '目標とプロジェクトの検索・追加・更新・削除は、ユーザーの明確な依頼に基づいて専用ツールで実行してください。'
    '目標を作成するときは長期的な到達状態、プロジェクトは具体的な取り組みとして区別し、プロジェクトは該当する目標へ関連付けてください。'
    '新しい進行中プロジェクトを作成したら、project_idで既存タスクを確認し、まだ無ければ最初の実行可能な作業タスクを3〜5件考えて同じproject_idに登録してください。'
    'タスクは具体的で重複しない順序にし、期限・見積時間・優先度はユーザーが示した場合だけ設定してください。目標やプロジェクトの更新では既存タスクを重複作成しないでください。'
    'タスクをプロジェクトへ関連付ける場合は、ユーザーの意図が明らかな場合だけ行ってください。'
    '親しみがあり自然な友人のような話し方をしてください。ただし人間だと偽ったり、根拠のない親密さを演じたりしないでください。'
    '会話の記憶はスレッドと別に保存されます。昔の出来事、人、好み、以前の発言について答える前に記憶検索ツールを使ってください。'
    '記憶検索のquery_variantsには、検索意図と人物・場所・時期などの具体的条件を保った言い換えだけを入れてください。'
    '検索結果は意味的に近い候補であり、事実の一致を保証しません。回答前に候補文を証拠として読み、質問が求める人物、行動、場所、時期、対象の各要素が候補に裏付けられているか判定してください。'
    '質問文に含まれる前提は記憶の証拠ではありません。候補にない具体的条件を補って答えたり、別の出来事を同じ出来事として扱ったりしないでください。'
    '一部だけ関連する記憶があれば、覚えている部分と確認できない部分を分けて伝えてください。裏付ける記憶がなければ、思い出せないと率直に伝えてください。'
    '通常の会話中に自動で記憶保存toolを呼ばないでください。自動抽出は1時間に1回、別処理でまとめて行います。ユーザーが明示的に覚えるよう依頼した場合だけ、記憶保存toolをその場で使ってください。'
    'パスワード、APIキー、認証情報、決済情報、正確な住所は記録しないでください。健康・法務・金融等の非常に慎重な情報は、ユーザーが明確に保存を依頼しない限り記録しないでください。'
    'ユーザーが内容を訂正したら対象の記憶を検索して更新し、忘れるよう頼んだら対象を特定して削除してください。保存のたびに承認を求めないでください。'
    '該当する記憶が見つからない場合は、覚えているふりをせず、そのことを率直に伝えてください。'
    'ユーザーの確認なしに外部操作を実行したと主張してはいけません。'
)
MEMORY_EXTRACTION_PROMPT = (
    '前回の抽出後に蓄積した会話から、今後の会話に役立つ長期記憶を最大3件抽出してください。'
    '対象は継続的な事実・好み・価値観と、本人にとって意味のある出来事や思い出です。'
    '一時的な予定、単発の依頼、雑談の全発言、仮定・創作・ロールプレイは記録しないでください。'
    '質問、確認、推測、以前の出来事を思い出せるか尋ねる発言は、ユーザー自身の事実として保存しないでください。'
    'assistantの発言は文脈としてのみ使い、事実の根拠はuserの発言に限定してください。'
    'パスワード、APIキー、認証情報、決済情報、正確な住所は絶対に記録しないでください。'
    '記憶は短く、後から思い出すのに十分な具体性を保ってください。'
    'source_typeはユーザーが明言した場合explicit、文脈から控えめに推定した場合inferredにしてください。'
    '各記憶に根拠となったuserメッセージのconversation_idを含めてください。'
    '適切な記憶がなければ空配列を返してください。JSON objectのみを返し、形式は'
    '{"memories":[{"category":"fact|preference|episode","content":"...","source_type":"explicit|inferred","source_conversation_id":"..."}]}です。'
)
MEMORY_CHANGE_PATTERN = re.compile(
    r'忘れて|忘れといて|忘れてください|忘れたい|覚えないで|覚えなくて|覚えておいて|覚えてね|記憶して|記録して|'
    r'記録しないで|記録しなくて|保存しないで|保存しなくて|記憶しないで|記憶しなくて|記憶から消して|記憶を削除|'
    r'訂正|修正|間違って覚え|ではなく|じゃなく|違うよ'
)
MEMORY_NON_ASSERTION_PATTERN = re.compile(
    r'[?？]|覚えてる|覚えてます|覚えてた|話したっけ|言ったっけ|だったっけ|何だっけ|なんだっけ|'
    r'思い出せる|思い出して|覚えているかな|記憶にあるかな'
)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


async def generate_llm_reply(
    messages: list[dict[str, str]], conversation_id: str | None = None
) -> str:
    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    local_now = datetime.now(LOCAL_TIMEZONE).isoformat(timespec='minutes')
    llm_messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': f'{SYSTEM_PROMPT}\n現在日時（Asia/Tokyo）: {local_now}'},
        *messages,
    ]
    completed_actions: list[str] = []
    seen_tool_calls: set[tuple[str, str]] = set()

    for _ in range(8):
        payload = {
            'model': LLM_MODEL,
            'messages': llm_messages,
            'tools': [*SCHEDULE_TOOLS, *TASK_TOOLS, *MEMORY_TOOLS, *GOAL_PROJECT_TOOLS],
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
                    'schedule_task', 'unschedule_task',
                    'save_memory', 'update_memory', 'forget_memory',
                    'create_goal', 'update_goal', 'delete_goal',
                    'create_project', 'update_project', 'delete_project',
                }
                if name in mutating_tools and call_key in seen_tool_calls:
                    result = {'ok': False, 'error': '同じツール呼び出しを重複実行しませんでした。'}
                else:
                    if name in mutating_tools:
                        seen_tool_calls.add(call_key)
                    task_tools = {
                        'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task',
                        'find_task_slots', 'schedule_task', 'unschedule_task',
                    }
                    memory_tools = {tool['function']['name'] for tool in MEMORY_TOOLS}
                    goal_project_tools = {tool['function']['name'] for tool in GOAL_PROJECT_TOOLS}
                    if name in memory_tools:
                        result = await asyncio.to_thread(
                            execute_memory_tool, name, arguments, conversation_id
                        )
                    elif name in goal_project_tools:
                        result = await asyncio.to_thread(execute_goal_project_tool, name, arguments)
                    elif name in task_tools:
                        result = execute_task_tool(name, arguments)
                    else:
                        result = execute_schedule_tool(name, arguments)
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


MEMORY_EXTRACTION_INTERVAL = timedelta(hours=1)
MEMORY_EXTRACTION_MAX_MESSAGES = 50
MEMORY_EXTRACTION_MAX_CHARACTERS = 24000


def claim_memory_extraction_batch() -> tuple[list[dict[str, Any]], int] | None:
    now = datetime.now(timezone.utc)
    attempted_at = now.isoformat(timespec='seconds')
    with closing(open_database()) as connection:
        connection.execute('BEGIN IMMEDIATE')
        state = connection.execute(
            'SELECT last_attempted_at, last_processed_message_id FROM memory_extraction_state WHERE id = 1'
        ).fetchone()
        if state['last_attempted_at']:
            last_attempted = datetime.fromisoformat(state['last_attempted_at'])
            if now - last_attempted < MEMORY_EXTRACTION_INTERVAL:
                connection.commit()
                return None

        rows = connection.execute(
            '''
            SELECT id, conversation_id, role, content FROM messages
            WHERE id > ? ORDER BY id LIMIT ?
            ''',
            (state['last_processed_message_id'], MEMORY_EXTRACTION_MAX_MESSAGES),
        ).fetchall()
        if not rows:
            connection.commit()
            return None

        batch: list[dict[str, Any]] = []
        last_processed_id = state['last_processed_message_id']
        character_count = 0
        suppressed_assistant_conversations: set[str] = set()
        for row in rows:
            message = dict(row)
            if message['role'] == 'user' and (
                MEMORY_CHANGE_PATTERN.search(message['content'])
                or MEMORY_NON_ASSERTION_PATTERN.search(message['content'])
            ):
                suppressed_assistant_conversations.add(message['conversation_id'])
                last_processed_id = message['id']
                continue
            if message['role'] == 'assistant' and message['conversation_id'] in suppressed_assistant_conversations:
                suppressed_assistant_conversations.remove(message['conversation_id'])
                last_processed_id = message['id']
                continue
            if batch and character_count + len(message['content']) > MEMORY_EXTRACTION_MAX_CHARACTERS:
                break
            batch.append(message)
            character_count += len(message['content'])
            last_processed_id = message['id']

        if not batch:
            connection.execute(
                'UPDATE memory_extraction_state SET last_processed_message_id = ? WHERE id = 1',
                (last_processed_id,),
            )
            connection.commit()
            return None

        connection.execute(
            'UPDATE memory_extraction_state SET last_attempted_at = ? WHERE id = 1',
            (attempted_at,),
        )
        connection.commit()
    return batch, last_processed_id


def mark_memory_batch_processed(message_id: int) -> None:
    with closing(open_database()) as connection:
        connection.execute(
            '''
            UPDATE memory_extraction_state
            SET last_processed_message_id = MAX(last_processed_message_id, ?)
            WHERE id = 1
            ''',
            (message_id,),
        )
        connection.commit()


async def extract_due_memories() -> None:
    claimed = claim_memory_extraction_batch()
    if claimed is None:
        return
    messages, last_processed_id = claimed
    user_conversation_ids = {
        message['conversation_id'] for message in messages if message['role'] == 'user'
    }
    if not user_conversation_ids:
        mark_memory_batch_processed(last_processed_id)
        return

    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    payload = {
        'model': LLM_MODEL,
        'messages': [
            {'role': 'system', 'content': MEMORY_EXTRACTION_PROMPT},
            {'role': 'user', 'content': json.dumps(messages, ensure_ascii=False)},
        ],
        'response_format': {'type': 'json_object'},
        'temperature': 0,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30, connect=5)) as client:
            response = await client.post(
                f'{LLM_BASE_URL}/chat/completions', json=payload, headers=headers,
            )
        if response.is_error:
            logger.warning('Memory extraction returned HTTP %s', response.status_code)
            return
        content = response.json()['choices'][0]['message']['content']
        if not isinstance(content, str):
            return
        extracted = json.loads(content)
        memories = extracted.get('memories', []) if isinstance(extracted, dict) else []
        if not isinstance(memories, list):
            return
        for item in memories[:3]:
            if not isinstance(item, dict) or set(item) != {
                'category', 'content', 'source_type', 'source_conversation_id'
            }:
                continue
            source_conversation_id = item['source_conversation_id']
            if source_conversation_id not in user_conversation_ids:
                continue
            try:
                await asyncio.to_thread(
                    save_memory_record,
                    item['category'], item['content'], item['source_type'], source_conversation_id,
                )
            except (TypeError, ValueError):
                continue
        mark_memory_batch_processed(last_processed_id)
    except (httpx.HTTPError, sqlite3.Error, ValueError, KeyError, IndexError, TypeError) as error:
        logger.warning('Memory extraction failed: %s', type(error).__name__)


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
    reply = await generate_llm_reply(history, conversation_id)
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

    await extract_due_memories()

    return {
        'conversation_id': conversation_id,
        'user_message': {'role': 'user', 'content': content, 'created_at': timestamp},
        'assistant_message': {'role': 'assistant', 'content': reply, 'created_at': timestamp},
    }
