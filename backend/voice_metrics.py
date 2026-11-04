import csv
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from settings import DATA_DIR

router = APIRouter()
write_lock = Lock()
CSV_PATH = DATA_DIR / 'voice-latency.csv'
METRIC_FIELDS = (
    'listening_ms',
    'stt_ms',
    'chat_ms',
    'server_ms',
    'llm_ms',
    'processing_ms',
    'tts_ms',
    'playback_ms',
    'total_ms',
)
CSV_FIELDS = ('timestamp', *METRIC_FIELDS)


class VoiceMetrics(BaseModel):
    model_config = ConfigDict(extra='forbid')

    listening_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    stt_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    chat_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    server_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    llm_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    processing_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    tts_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    playback_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)
    total_ms: float = Field(ge=0, le=3_600_000, allow_inf_nan=False)


@router.post('/api/voice-metrics')
def record_voice_metrics(metrics: VoiceMetrics) -> dict[str, bool]:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with write_lock:
        needs_header = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
        with CSV_PATH.open('a', encoding='utf-8', newline='') as csv_file:
            writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS)
            if needs_header:
                writer.writeheader()
            writer.writerow({
                'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
                **metrics.model_dump(),
            })
    return {'saved': True}
