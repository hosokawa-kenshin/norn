from collections import deque
from pathlib import Path
from zipfile import BadZipFile

import librosa
import numpy as np
import soundfile as sf
import webrtcvad
from scipy.spatial.distance import cdist

from settings import DATA_DIR


PROFILE_PATH = DATA_DIR / 'wake-profile.npz'


def features(audio):
    trimmed, _ = librosa.effects.trim(np.asarray(audio, dtype=np.float32), top_db=25,
                                      frame_length=256, hop_length=80)
    if not 2400 <= len(trimmed) <= 32000:
        return None
    padded = np.pad(trimmed, (640, 640))
    coefficients = librosa.feature.mfcc(
        y=padded, sr=16000, n_mfcc=14, n_mels=40, n_fft=512, hop_length=160, win_length=400,
    )[1:]
    norms = np.linalg.norm(coefficients, axis=0, keepdims=True)
    return np.divide(coefficients, norms, out=np.zeros_like(coefficients), where=norms > 1e-6)


def distance(left, right):
    costs = cdist(left.T, right.T, metric='sqeuclidean') * 0.5
    accumulated, path = librosa.sequence.dtw(C=costs, global_constraints=True, band_rad=0.25)
    return float(accumulated[-1, -1] / len(path))


def enroll(recording: Path, destination=PROFILE_PATH):
    audio, rate = sf.read(recording)
    if rate != 16000 or audio.ndim != 1 or not np.isfinite(audio).all():
        raise ValueError('Enrollment requires finite 16kHz mono audio')
    intervals = librosa.effects.split(audio, top_db=25, frame_length=512, hop_length=160)
    examples = []
    for start, end in intervals:
        if examples and start - examples[-1][1] <= int(rate * 0.6):
            examples[-1][1] = end
        else:
            examples.append([start, end])
    templates = []
    for start, end in examples:
        value = features(audio[start:end])
        if value is not None:
            templates.append(value)
    if not 3 <= len(templates) <= 12:
        raise ValueError(f'Expected 3-12 isolated wake examples, found {len(templates)}')
    holdout = [min(distance(template, other) for other_index, other in enumerate(templates)
                   if other_index != index) for index, template in enumerate(templates)]
    if max(holdout) > 0.08:
        raise ValueError('Wake examples are too inconsistent for conservative matching')
    threshold = max(0.03, min(0.08, max(holdout) * 1.2))
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp.npz')
    np.savez_compressed(temporary, threshold=np.array(threshold),
                        **{f'template_{index}': value for index, value in enumerate(templates)})
    temporary.chmod(0o600)
    temporary.replace(destination)
    return {'examples': len(templates), 'holdout_max': round(max(holdout), 5),
            'threshold': round(threshold, 5)}


def load_profile(path=PROFILE_PATH):
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Personal wake profile is too large')
    try:
        with np.load(path, allow_pickle=False) as profile:
            threshold = float(profile['threshold'])
            templates = [profile[name] for name in profile.files if name.startswith('template_')]
    except (KeyError, TypeError, BadZipFile) as error:
        raise ValueError('Invalid personal wake profile') from error
    if not 0.03 <= threshold <= 0.08 or not 3 <= len(templates) <= 12:
        raise ValueError('Invalid personal wake profile')
    if any(value.ndim != 2 or value.shape[0] != 13 or not 10 <= value.shape[1] <= 220
           or not np.isfinite(value).all() for value in templates):
        raise ValueError('Invalid personal wake features')
    return threshold, templates


class PersonalWakeDetector:
    def __init__(self, path=PROFILE_PATH):
        self.threshold, self.templates = load_profile(path)
        distance(self.templates[0], self.templates[0])
        self.reset()

    def reset(self):
        self.vad = webrtcvad.Vad(2)
        self.pending = bytearray()
        self.preroll = deque(maxlen=5)
        self.frames = []
        self.silence = 0
        self.overlong = False

    def matches(self, audio):
        value = features(audio)
        return value is not None and min(distance(value, template) for template in self.templates) <= self.threshold

    def predict(self, pcm):
        self.pending.extend(pcm)
        while len(self.pending) >= 960:
            frame = bytes(self.pending[:960])
            del self.pending[:960]
            voiced = self.vad.is_speech(frame, 16000)
            if not self.frames:
                self.preroll.append(frame)
                if not voiced:
                    continue
                self.frames = list(self.preroll)
                self.preroll.clear()
            else:
                self.frames.append(frame)
            self.silence = 0 if voiced else self.silence + 1
            if len(self.frames) > 80:
                self.overlong = True
                self.frames = self.frames[-20:]
            if self.silence >= 12:
                audio = np.frombuffer(b''.join(self.frames), dtype='<i2').astype(np.float32) / 32768
                accepted = not self.overlong and self.matches(audio)
                self.frames = []
                self.silence = 0
                self.overlong = False
                self.preroll.clear()
                if accepted:
                    return True
        return False