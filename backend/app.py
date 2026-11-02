from contextlib import asynccontextmanager
import asyncio
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse

from chat import router as chat_router
from database import initialize_database
from events import router as events_router
from google_calendar import router as google_calendar_router, sync_worker
from memory import router as memory_router
from planning import router as planning_router
from tasks import router as tasks_router
import settings
from tts import SpeechService, router as tts_router
import tts
from voice import is_model_loaded, router as voice_router
from wake import router as wake_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    tts.speech = SpeechService()
    tts.speech.start()
    sync_task = asyncio.create_task(sync_worker())
    try:
        yield
    finally:
        tts.speech.close()
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='NORN API', version='0.1.0', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.NORN_FRONTEND_URL, 'http://127.0.0.1:5173', 'http://localhost:5173'],
    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
    allow_headers=['Content-Type'],
)
app.include_router(chat_router)
app.include_router(events_router)
app.include_router(google_calendar_router)
app.include_router(memory_router)
app.include_router(planning_router)
app.include_router(tasks_router)
app.include_router(voice_router)
app.include_router(tts_router)
app.include_router(wake_router)


@app.get('/api/status')
def get_status() -> dict[str, Any]:
    return {
        'stt': {
            'engine': 'faster-whisper',
            'model': settings.STT_MODEL_NAME,
            'device': settings.STT_DEVICE,
            'model_loaded': is_model_loaded(),
        },
        'llm': {'configured': bool(settings.LLM_MODEL), 'model': settings.LLM_MODEL},
    }
