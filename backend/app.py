from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env')

DATA_DIR = Path(os.getenv('NORN_DATA_DIR', PROJECT_ROOT / 'data'))
DB_PATH = Path(os.getenv('NORN_DB_PATH', DATA_DIR / 'norn.sqlite3'))
STT_MODEL_NAME = os.getenv('STT_MODEL', 'small')
STT_DEVICE = os.getenv('STT_DEVICE', 'cpu')
STT_COMPUTE_TYPE = os.getenv('STT_COMPUTE_TYPE', 'int8')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
MAX_AUDIO_BYTES = int(os.getenv('MAX_AUDIO_MB', '20')) * 1024 * 1024
MAX_MESSAGE_LENGTH = 4000
SYSTEM_PROMPT = (
    'あなたはNORNという日本語のパーソナルアシスタントです。'
    '予定やタスクを整理し、根拠と不確実性を明確にして簡潔に回答してください。'
    'ユーザーの確認なしに外部操作を実行したと主張しないでください。'
)

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('norn')
model_lock = threading.Lock()
transcription_model: Any | None = None

@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title='NORN API', version='0.1.0', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://127.0.0.1:5173', 'http://localhost:5173'],
    allow_methods=['GET', 'POST'],
    allow_headers=['Content-Type'],
)


class ChatRequest(BaseModel):
    conversation_id: str | None = None
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_database() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys = ON')
    return connection


def initialize_database() -> None:
    with closing(open_database()) as connection:
        connection.executescript(
            '''
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_messages_conversation
                ON messages(conversation_id, id);
            '''
        )
        connection.commit()


def get_transcription_model() -> Any:
    global transcription_model
    if transcription_model is None:
        with model_lock:
            if transcription_model is None:
                from faster_whisper import WhisperModel

                transcription_model = WhisperModel(
                    STT_MODEL_NAME,
                    device=STT_DEVICE,
                    compute_type=STT_COMPUTE_TYPE,
                )
    return transcription_model


def transcribe_file(path: Path) -> dict[str, Any]:
    model = get_transcription_model()
    segments, info = model.transcribe(
        str(path),
        language='ja',
        beam_size=5,
        vad_filter=True,
    )
    text = ''.join(segment.text for segment in segments).strip()
    return {'text': text, 'language': info.language, 'duration': round(info.duration, 2)}


def get_conversation_messages(conversation_id: str, limit: int = 20) -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            '''
            SELECT role, content FROM messages
            WHERE conversation_id = ? ORDER BY id DESC LIMIT ?
            ''',
            (conversation_id, limit),
        ).fetchall()
    return [dict(row) for row in reversed(rows)]


async def generate_llm_reply(messages: list[dict[str, str]]) -> str:
    headers = {'Authorization': f'Bearer {LLM_API_KEY}'} if LLM_API_KEY else {}
    payload = {
        'model': LLM_MODEL,
        'messages': [{'role': 'system', 'content': SYSTEM_PROMPT}, *messages],
        'temperature': 0.7,
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(90, connect=10)) as client:
            response = await client.post(
                f'{LLM_BASE_URL}/chat/completions',
                json=payload,
                headers=headers,
            )
    except httpx.TimeoutException as error:
        raise HTTPException(status_code=504, detail='LLMの応答がタイムアウトしました。') from error
    except httpx.HTTPError as error:
        logger.warning('LLM connection failed: %s', type(error).__name__)
        raise HTTPException(status_code=502, detail='LLM接続に失敗しました。接続設定を確認してください。') from error

    if response.is_error:
        logger.warning('LLM returned HTTP %s', response.status_code)
        raise HTTPException(status_code=502, detail=f'LLMがエラーを返しました (HTTP {response.status_code})')

    try:
        content = response.json()['choices'][0]['message']['content']
    except (ValueError, KeyError, IndexError, TypeError) as error:
        raise HTTPException(status_code=502, detail='LLMから読み取れる応答がありませんでした。') from error
    if not isinstance(content, str) or not content.strip():
        raise HTTPException(status_code=502, detail='LLMから空の応答が返されました。')
    return content.strip()


@app.get('/api/status')
def get_status() -> dict[str, Any]:
    return {
        'stt': {
            'engine': 'faster-whisper',
            'model': STT_MODEL_NAME,
            'device': STT_DEVICE,
            'model_loaded': transcription_model is not None,
        },
        'llm': {'configured': bool(LLM_MODEL), 'model': LLM_MODEL},
    }


@app.post('/api/transcribe')
async def transcribe_audio(audio: UploadFile = File(...)) -> dict[str, Any]:
    if not audio.content_type or not audio.content_type.startswith('audio/'):
        raise HTTPException(status_code=415, detail='音声ファイルを送信してください。')

    contents = await audio.read(MAX_AUDIO_BYTES + 1)
    if not contents:
        raise HTTPException(status_code=400, detail='録音データが空です。')
    if len(contents) > MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail='音声は20MB以下にしてください。')

    suffix_by_type = {
        'audio/webm': '.webm',
        'audio/mp4': '.m4a',
        'audio/ogg': '.ogg',
        'audio/wav': '.wav',
        'audio/x-wav': '.wav',
    }
    suffix = suffix_by_type.get(audio.content_type.split(';', 1)[0], '.webm')
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary_file:
            temporary_file.write(contents)
            temporary_path = Path(temporary_file.name)
        return await run_in_threadpool(transcribe_file, temporary_path)
    except Exception as error:
        logger.exception('Local transcription failed')
        raise HTTPException(
            status_code=503,
            detail='ローカル音声認識を開始できませんでした。モデルの初回ダウンロードと設定を確認してください。',
        ) from error
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        await audio.close()


@app.get('/api/conversations')
def list_conversations() -> list[dict[str, str]]:
    with closing(open_database()) as connection:
        rows = connection.execute(
            'SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50'
        ).fetchall()
    return [dict(row) for row in rows]


@app.get('/api/conversations/{conversation_id}/messages')
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


@app.post('/api/chat')
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