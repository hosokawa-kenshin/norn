import base64
import contextlib
import io
import json
import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path


def prepare_bert_cache(source, cache_root):
    import torch
    from safetensors.torch import save_file

    weights = source / 'pytorch_model.bin'
    if (source / 'model.safetensors').is_file() or not weights.is_file():
        return source
    signature = [(str(path.resolve()), path.stat().st_size, path.stat().st_mtime_ns)
                 for path in (weights, source / 'config.json')]
    key = hashlib.sha256(json.dumps(signature).encode()).hexdigest()[:20]
    cached = cache_root / key
    if (cached / 'model.safetensors').is_file():
        return cached
    cache_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=cache_root) as temporary:
        converted = Path(temporary) / key
        converted.mkdir()
        state = torch.load(weights, map_location='cpu', weights_only=True, mmap=True)
        save_file({name: tensor.clone().contiguous() for name, tensor in state.items()},
                  str(converted / 'model.safetensors'), metadata={'format': 'pt'})
        shutil.copy2(source / 'config.json', converted / 'config.json')
        try:
            converted.rename(cached)
        except FileExistsError:
            if not (cached / 'model.safetensors').is_file():
                raise
    return cached


def main():
    protocol = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        root = Path(sys.argv[1]).resolve()
        model_path = Path(sys.argv[2]).resolve()
        os.chdir(root)
        sys.path.insert(0, str(root))
        import numpy as np
        import torch
        from scipy.io import wavfile
        from style_bert_vits2.constants import Languages, DEFAULT_BERT_MODEL_PATHS
        from style_bert_vits2.nlp import bert_models
        from style_bert_vits2.tts_model import TTSModel

        if not torch.cuda.is_available():
            raise RuntimeError('CUDA is required for NORN speech synthesis')
        started = time.perf_counter()
        bert_models.load_tokenizer(Languages.JP)
        bert_path = prepare_bert_cache(DEFAULT_BERT_MODEL_PATHS[Languages.JP], Path(sys.argv[3]))
        bert_models.load_model(Languages.JP, pretrained_model_name_or_path=str(bert_path), device_map='cuda')
        model = TTSModel(
            model_path=model_path,
            config_path=model_path.parent / 'config.json',
            style_vec_path=model_path.parent / 'style_vectors.npy',
            device='cuda',
        )
        model.load()
        model.infer('準備ができました。', language=Languages.JP, speaker_id=0, style='Neutral')
        torch.cuda.synchronize()
        print(json.dumps({'ready': True, 'device': 'cuda', 'model': model_path.name,
                          'load_seconds': round(time.perf_counter() - started, 3)}),
              file=protocol, flush=True)
        for line in sys.stdin:
            try:
                text = json.loads(line)['text']
                started = time.perf_counter()
                sample_rate, audio = model.infer(
                    text, language=Languages.JP, speaker_id=0, style='Neutral',
                )
                torch.cuda.synchronize()
                if not audio.size or not np.isfinite(audio).all() or not np.any(audio):
                    raise RuntimeError('Speech synthesis produced invalid audio')
                buffer = io.BytesIO()
                wavfile.write(buffer, sample_rate, audio)
                result = {'audio': base64.b64encode(buffer.getvalue()).decode('ascii'),
                          'seconds': round(time.perf_counter() - started, 3)}
            except Exception:
                import traceback
                traceback.print_exc(file=sys.stderr)
                result = {'error': 'Speech synthesis failed; see tts-worker.log'}
            print(json.dumps(result), file=protocol, flush=True)


if __name__ == '__main__':
    main()