import asyncio
import json
import threading
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

import settings
from personal_wake import PROFILE_PATH, enroll, load_profile
from setup_voice_models import MODEL_DIR
from wake_calibration import MAX_BYTES, store_clip


router = APIRouter()
PHRASE = 'ノルン'
connections = asyncio.Semaphore(2)
calibration_lock = threading.Lock()


def calibration_required():
    try:
        load_profile(PROFILE_PATH)
    except (OSError, ValueError, EOFError):
        return True
    return False


def register_initial_calibration(contents):
    with calibration_lock:
        if not calibration_required():
            raise HTTPException(status_code=409, detail='キャリブレーションは登録済みです。')
        recording = None
        try:
            result = store_clip(contents, directory=PROFILE_PATH.parent / 'wake-calibration')
            recording = Path(result['path'])
            registered = enroll(recording, destination=PROFILE_PATH)
            load_profile(PROFILE_PATH)
            return {'calibrated': True, 'examples': registered['examples']}
        except ValueError as error:
            raise HTTPException(status_code=422, detail='録音を登録できませんでした。1秒ずつあけて「ノルン」を4回録音してください。') from error
        except OSError as error:
            settings.logger.warning('Initial wake calibration storage failed: %s', type(error).__name__)
            raise HTTPException(status_code=503, detail='キャリブレーションを保存できません。') from error
        finally:
            if recording is not None:
                recording.unlink(missing_ok=True)


def model_available():
    return all((MODEL_DIR / name).is_file() for name in ['am/final.mdl', 'graph/Gr.fst'])


@lru_cache(maxsize=1)
def get_keyword_model():
    from vosk import Model, SetLogLevel

    SetLogLevel(-1)
    model = Model(str(MODEL_DIR))
    if model.vosk_model_find_word(PHRASE) < 0:
        raise RuntimeError('The Japanese model does not contain the wake word')
    return model


class KeywordDetector:
    def __init__(self):
        self.model = get_keyword_model()
        self.personal = None
        if PROFILE_PATH.is_file():
            from personal_wake import PersonalWakeDetector

            try:
                self.personal = PersonalWakeDetector()
            except (ValueError, OSError):
                settings.logger.warning('Personal wake profile could not be loaded; using standard detector')
        self.reset()

    def reset(self):
        from vosk import KaldiRecognizer

        self.recognizer = KaldiRecognizer(self.model, 16000, json.dumps([PHRASE, '[unk]'], ensure_ascii=False))
        self.recognizer.SetWords(True)
        if getattr(self, 'personal', None) is not None:
            self.personal.reset()

    def predict(self, frame):
        import numpy as np

        if not frame or len(frame) % 4 or len(frame) > 32768:
            raise ValueError('Invalid 16kHz float32 audio frame')
        samples = np.frombuffer(frame, dtype='<f4')
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
            raise ValueError('Invalid audio values')
        pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
        if getattr(self, 'personal', None) is not None and self.personal.predict(pcm):
            self.reset()
            return True
        if self.recognizer.AcceptWaveform(pcm):
            result = json.loads(self.recognizer.Result())
            words = result.get('result', [])
            detected = any(
                word.get('word') == PHRASE
                and word.get('conf', 0) >= 0.9
                and word.get('end', 0) - word.get('start', 0) >= 0.15
                for word in words
            )
            if detected:
                self.reset()
                return True
        return False


@router.get('/api/wake/status')
def wake_status():
    required = calibration_required()
    return {'available': model_available(), 'phrase': PHRASE, 'engine': 'vosk-ja-keyword-grammar',
            'processing': 'local', 'idle_seconds': 60, 'personalized': not required,
            'calibration_required': required}


@router.post('/api/wake/calibration')
async def initial_calibration(request: Request, audio: UploadFile = File(...), consent: bool = Form(False)):
    try:
        origin = request.headers.get('origin')
        if origin is not None and origin not in {settings.NORN_FRONTEND_URL, settings.NORN_API_URL}:
            raise HTTPException(status_code=403, detail='NORNの画面から登録してください。')
        if not consent:
            raise HTTPException(status_code=400, detail='録音の登録には同意が必要です。')
        if not audio.content_type or not audio.content_type.startswith('audio/'):
            raise HTTPException(status_code=415, detail='音声ファイルを送信してください。')
        contents = await audio.read(MAX_BYTES + 1)
        if len(contents) > MAX_BYTES:
            raise HTTPException(status_code=413, detail='録音は15秒以内にしてください。')
    finally:
        await audio.close()
    return await run_in_threadpool(register_initial_calibration, contents)


@router.websocket('/api/wake/listen')
async def listen_for_wake(websocket: WebSocket):
    if websocket.headers.get('origin') not in {settings.NORN_FRONTEND_URL, settings.NORN_API_URL}:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    if not model_available() or connections.locked():
        await websocket.send_json({'error': 'ウェイクワードモデルが未配置、または接続数上限です。'})
        await websocket.close(code=1013)
        return
    async with connections:
        try:
            detector = await asyncio.to_thread(KeywordDetector)
            await websocket.send_json({'ready': True, 'phrase': PHRASE})
            while True:
                message = await websocket.receive()
                if message['type'] == 'websocket.disconnect':
                    break
                if message.get('text') == 'reset':
                    await asyncio.to_thread(detector.reset)
                elif message.get('bytes') is not None:
                    if await asyncio.to_thread(detector.predict, message['bytes']):
                        await websocket.send_json({'wake': True})
        except WebSocketDisconnect:
            pass
        except ValueError:
            await websocket.close(code=1008)
        except Exception:
            settings.logger.exception('Local keyword detection failed')
            try:
                await websocket.send_json({'error': 'ローカルウェイクワード検出を開始できません。'})
                await websocket.close(code=1011)
            except RuntimeError:
                pass