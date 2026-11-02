import base64
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import settings


router = APIRouter()


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class SpeechService:
    def __init__(self):
        self.process = None
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.state = {'state': 'disabled', 'device': 'cuda'}

    def start(self):
        if os.getenv('NORN_TTS_ENABLED', '1') != '1':
            return
        root = Path(os.getenv('NORN_TTS_ROOT', settings.PROJECT_ROOT.parent / 'style-bert-vits2'))
        configured = os.getenv('NORN_TTS_MODEL', '')
        candidates = list((root / 'model_assets/norn_voice').glob('norn_voice_e100_s*.safetensors'))
        model = Path(configured) if configured else max(
            candidates, key=lambda path: int(path.stem.rsplit('_s', 1)[1]), default=None,
        )
        python = root / '.venv/bin/python'
        if model is None or not model.is_file() or not python.is_file():
            self.state = {'state': 'unavailable', 'device': 'cuda', 'error': '音声モデルまたは専用Pythonが見つかりません。'}
            return
        self.state = {'state': 'loading', 'device': 'cuda', 'model': model.name}
        threading.Thread(target=self._boot, args=(root, python, model), daemon=True).start()

    def _read_responses(self, process):
        try:
            for line in process.stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)
            if not self.stopped.is_set() and self.state['state'] == 'ready':
                self.state = {'state': 'failed', 'device': 'cuda', 'error': '音声ワーカーが停止しました。'}

    def _receive(self, timeout):
        line = self.responses.get(timeout=timeout)
        if line is None:
            raise RuntimeError('Speech worker exited')
        return json.loads(line)

    def _boot(self, root, python, model):
        try:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self.lock, (settings.DATA_DIR / 'tts-worker.log').open('a', encoding='utf-8') as log:
                if self.stopped.is_set():
                    return
                self.process = subprocess.Popen(
                    [str(python), '-u', str(Path(__file__).with_name('tts_worker.py')),
                     str(root), str(model), str((settings.DATA_DIR / 'tts-bert-cache').resolve())],
                    cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                    text=True, bufsize=1,
                    env=dict(os.environ, OMP_NUM_THREADS='4', MKL_NUM_THREADS='4',
                             HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1'),
                )
                threading.Thread(target=self._read_responses, args=(self.process,), daemon=True).start()
                result = self._receive(900)
                if not result.get('ready'):
                    raise RuntimeError('Speech worker did not become ready')
                self.state = {'state': 'ready', **result}
        except Exception:
            settings.logger.exception('Speech worker initialization failed')
            self.state = {'state': 'failed', 'device': 'cuda', 'error': '音声モデルを読み込めません。tts-worker.logを確認してください。'}
            self.close()

    def synthesize(self, text):
        if not text.strip():
            raise HTTPException(status_code=422, detail='読み上げる文章が空です。')
        if self.state['state'] != 'ready':
            raise HTTPException(status_code=503, detail='音声モデルは準備中、または利用できません。')
        if not self.lock.acquire(blocking=False):
            raise HTTPException(status_code=429, detail='別の音声を生成中です。')
        try:
            if self.process is None or self.process.poll() is not None:
                raise RuntimeError('Speech worker is not running')
            self.process.stdin.write(json.dumps({'text': text}) + '\n')
            self.process.stdin.flush()
            result = self._receive(120)
            if result.get('error'):
                raise HTTPException(status_code=502, detail='音声生成に失敗しました。テキストをご確認ください。')
            return base64.b64decode(result['audio'], validate=True), result['seconds']
        except HTTPException:
            raise
        except Exception:
            settings.logger.exception('Speech worker communication failed')
            self.state = {'state': 'failed', 'device': 'cuda', 'error': '音声ワーカーとの通信に失敗しました。'}
            self.close()
            raise HTTPException(status_code=503, detail='音声生成を利用できません。テキストをご確認ください。')
        finally:
            self.lock.release()

    def close(self):
        self.stopped.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


speech = SpeechService()


@router.get('/api/speech/status')
def speech_status():
    return speech.state


@router.post('/api/speech')
def generate_speech(payload: SpeechRequest):
    audio, seconds = speech.synthesize(payload.text)
    return Response(audio, media_type='audio/wav', headers={
        'Cache-Control': 'no-store', 'X-Synthesis-Seconds': str(seconds),
    })