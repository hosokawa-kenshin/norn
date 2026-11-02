import base64
import json
import os
import queue
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

import settings


router = APIRouter()


class SpeechRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@dataclass(frozen=True)
class SpeechConfiguration:
    root: Path
    python: Path
    model: Path
    config: Path
    style_vectors: Path
    device: str
    bert_model: Path | None
    speaker_id: int
    style: str


def speech_configuration(environ=None, base=None, platform=None):
    env = os.environ if environ is None else environ
    project = settings.PROJECT_ROOT if base is None else Path(base)

    def resolve(value):
        candidate = Path(value).expanduser()
        return Path(os.path.abspath(candidate if candidate.is_absolute() else project / candidate))

    missing = [key for key in ('NORN_TTS_ROOT', 'NORN_TTS_MODEL') if not env.get(key, '').strip()]
    if missing:
        raise ValueError('音声合成は未設定です。' + '、'.join(missing) + 'を設定してください。')
    root = resolve(env['NORN_TTS_ROOT'])
    model = resolve(env['NORN_TTS_MODEL'])
    windows = (sys.platform if platform is None else platform) == 'win32'
    default_python = root / '.venv' / ('Scripts/python.exe' if windows else 'bin/python')
    python = resolve(env['NORN_TTS_PYTHON']) if env.get('NORN_TTS_PYTHON', '').strip() else default_python
    config = resolve(env['NORN_TTS_CONFIG']) if env.get('NORN_TTS_CONFIG', '').strip() else model.parent / 'config.json'
    vectors = resolve(env['NORN_TTS_STYLE_VECTORS']) if env.get('NORN_TTS_STYLE_VECTORS', '').strip() else model.parent / 'style_vectors.npy'
    device = env.get('NORN_TTS_DEVICE', 'auto').strip().lower()
    if not re.fullmatch(r'auto|cpu|cuda(?::\d+)?', device):
        raise ValueError('NORN_TTS_DEVICEはauto、cpu、cuda、cuda:番号のいずれかにしてください。')
    if not root.is_dir():
        raise ValueError('NORN_TTS_ROOTのディレクトリが見つかりません。')
    for key, candidate in [('NORN_TTS_PYTHON', python), ('NORN_TTS_MODEL', model),
                           ('NORN_TTS_CONFIG', config), ('NORN_TTS_STYLE_VECTORS', vectors)]:
        if not candidate.is_file():
            raise ValueError(key + 'のファイルが見つかりません。')
    if model.suffix.lower() not in {'.safetensors', '.pth'}:
        raise ValueError('NORN_TTS_MODELはPyTorch用のsafetensorsまたはpthを指定してください。')
    bert = resolve(env['NORN_TTS_BERT_MODEL']) if env.get('NORN_TTS_BERT_MODEL', '').strip() else None
    if bert is not None and not bert.is_dir():
        raise ValueError('NORN_TTS_BERT_MODELのディレクトリが見つかりません。')
    try:
        speaker_id = int(env.get('NORN_TTS_SPEAKER_ID', '0'))
    except ValueError as error:
        raise ValueError('NORN_TTS_SPEAKER_IDは0以上の整数にしてください。') from error
    if speaker_id < 0:
        raise ValueError('NORN_TTS_SPEAKER_IDは0以上の整数にしてください。')
    return SpeechConfiguration(root, python, model, config, vectors, device, bert, speaker_id,
                               env.get('NORN_TTS_STYLE', 'Neutral').strip() or 'Neutral')


class SpeechService:
    def __init__(self):
        self.process = None
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.device = 'auto'
        self.state = {'state': 'disabled', 'device': self.device}

    def start(self):
        if os.getenv('NORN_TTS_ENABLED', '0') != '1':
            return
        try:
            config = speech_configuration()
        except ValueError as error:
            unconfigured = any(not os.getenv(key, '').strip() for key in ('NORN_TTS_ROOT', 'NORN_TTS_MODEL'))
            self.state = {'state': 'unconfigured' if unconfigured else 'unavailable',
                          'device': self.device, 'error': str(error)}
            return
        self.device = config.device
        self.state = {'state': 'loading', 'device': self.device, 'model': config.model.name}
        threading.Thread(target=self._boot, args=(config,), daemon=True).start()

    def _read_responses(self, process):
        try:
            for line in process.stdout:
                self.responses.put(line)
        finally:
            self.responses.put(None)
            if not self.stopped.is_set() and self.state['state'] == 'ready':
                self.state = {'state': 'failed', 'device': self.device, 'error': '音声ワーカーが停止しました。'}

    def _receive(self, timeout):
        line = self.responses.get(timeout=timeout)
        if line is None:
            raise RuntimeError('Speech worker exited')
        return json.loads(line)

    def _boot(self, config):
        try:
            settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
            with self.lock, (settings.DATA_DIR / 'tts-worker.log').open('a', encoding='utf-8') as log:
                if self.stopped.is_set():
                    return
                command = [str(config.python), '-u', str(Path(__file__).with_name('tts_worker.py')),
                           str(config.root), str(config.model), str((settings.DATA_DIR / 'tts-bert-cache').resolve()),
                           '--config', str(config.config), '--style-vectors', str(config.style_vectors),
                           '--device', config.device, '--speaker-id', str(config.speaker_id), '--style', config.style]
                if config.bert_model is not None:
                    command.extend(['--bert-model', str(config.bert_model)])
                self.process = subprocess.Popen(
                    command,
                    cwd=config.root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
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
            self.state = {'state': 'failed', 'device': self.device, 'error': '音声モデルを読み込めません。tts-worker.logを確認してください。'}
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
            self.state = {'state': 'failed', 'device': self.device, 'error': '音声ワーカーとの通信に失敗しました。'}
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