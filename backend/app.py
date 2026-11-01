from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from chat import router as chat_router
from database import initialize_database
from events import router as events_router
from memory import router as memory_router
from planning import router as planning_router
from tasks import router as tasks_router
import settings
from voice import is_model_loaded, router as voice_router


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize_database()
    yield


app = FastAPI(title='NORN API', version='0.1.0', lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=['http://127.0.0.1:5173', 'http://localhost:5173'],
    allow_methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'],
    allow_headers=['Content-Type'],
)
app.include_router(chat_router)
app.include_router(events_router)
app.include_router(memory_router)
app.include_router(planning_router)
app.include_router(tasks_router)
app.include_router(voice_router)


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
