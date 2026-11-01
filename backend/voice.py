import tempfile
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from settings import MAX_AUDIO_BYTES, STT_COMPUTE_TYPE, STT_DEVICE, STT_MODEL_NAME, logger

router = APIRouter()
model_lock = threading.Lock()
transcription_model: Any | None = None


def is_model_loaded() -> bool:
    return transcription_model is not None


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


@router.post('/api/transcribe')
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
