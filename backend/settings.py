import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env')

DATA_DIR = Path(os.getenv('NORN_DATA_DIR', PROJECT_ROOT / 'data'))
DB_PATH = Path(os.getenv('NORN_DB_PATH', DATA_DIR / 'norn.sqlite3'))
STT_MODEL_NAME = os.getenv('STT_MODEL', 'small')
STT_DEVICE = os.getenv('STT_DEVICE', 'cpu')
STT_COMPUTE_TYPE = os.getenv('STT_COMPUTE_TYPE', 'int8')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
MAX_AUDIO_BYTES = int(os.getenv('MAX_AUDIO_MB', '20')) * 1024 * 1024
MAX_MESSAGE_LENGTH = 4000

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('norn')
