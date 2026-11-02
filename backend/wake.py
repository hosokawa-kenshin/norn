import asyncio
import json
from functools import lru_cache

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import settings
from setup_voice_models import MODEL_DIR


router = APIRouter()
PHRASE = 'ノルン'
connections = asyncio.Semaphore(2)


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
        self.reset()

    def reset(self):
        from vosk import KaldiRecognizer

        self.recognizer = KaldiRecognizer(self.model, 16000, json.dumps([PHRASE, '[unk]'], ensure_ascii=False))
        self.recognizer.SetWords(True)

    def predict(self, frame):
        import numpy as np

        if not frame or len(frame) % 4 or len(frame) > 32768:
            raise ValueError('Invalid 16kHz float32 audio frame')
        samples = np.frombuffer(frame, dtype='<f4')
        if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
            raise ValueError('Invalid audio values')
        pcm = (np.clip(samples, -1, 1) * 32767).astype('<i2').tobytes()
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
    return {'available': model_available(), 'phrase': PHRASE, 'engine': 'vosk-ja-keyword-grammar',
            'processing': 'local', 'idle_seconds': 60}


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