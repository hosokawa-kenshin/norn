import io
import zipfile

import httpx

from settings import PROJECT_ROOT


MODEL_NAME = 'vosk-model-small-ja-0.22'
MODEL_DIR = PROJECT_ROOT / 'models/wakeword-ja' / MODEL_NAME


def main():
    if (MODEL_DIR / 'am/final.mdl').is_file():
        print(f'Japanese wake model already available: {MODEL_DIR}')
        return
    response = httpx.get(f'https://alphacephei.com/vosk/models/{MODEL_NAME}.zip', timeout=180, follow_redirects=True)
    response.raise_for_status()
    root = MODEL_DIR.parent.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        if sum(member.file_size for member in archive.infolist()) > 512 * 1024 * 1024:
            raise ValueError('Unexpected model archive size')
        for member in archive.infolist():
            destination = (root / member.filename).resolve()
            destination.relative_to(root)
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(member))
    print(f'Japanese wake model ready: {MODEL_DIR}')


if __name__ == '__main__':
    main()