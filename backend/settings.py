import logging
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_ROOT / '.env')

DATA_DIR = Path(os.getenv('NORN_DATA_DIR', PROJECT_ROOT / 'data'))
DB_PATH = Path(os.getenv('NORN_DB_PATH', DATA_DIR / 'norn.sqlite3'))
NORN_API_URL = os.getenv('NORN_API_URL', 'http://127.0.0.1:8000').rstrip('/')
NORN_FRONTEND_URL = os.getenv('NORN_FRONTEND_URL', 'http://127.0.0.1:5173').rstrip('/')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI = os.getenv(
	'NORN_GOOGLE_REDIRECT_URI', f'{NORN_API_URL}/'
)
GOOGLE_CALENDAR_ID = os.getenv('NORN_GOOGLE_CALENDAR_ID', 'primary')
GOOGLE_SYNC_INTERVAL_SECONDS = 300
GOOGLE_TOKEN_PATH = DATA_DIR / 'google-calendar-token.json'
STT_MODEL_NAME = os.getenv('STT_MODEL', 'small')
STT_DEVICE = os.getenv('STT_DEVICE', 'cpu')
STT_COMPUTE_TYPE = os.getenv('STT_COMPUTE_TYPE', 'int8')
LLM_BASE_URL = os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
LLM_API_KEY = os.getenv('LLM_API_KEY', '')
LLM_MODEL = os.getenv('LLM_MODEL', '')
MAX_AUDIO_BYTES = int(os.getenv('MAX_AUDIO_MB', '20')) * 1024 * 1024
MAX_MESSAGE_LENGTH = 4000
PLANNING_DAY_START = os.getenv('NORN_PLANNING_DAY_START', '09:00')
PLANNING_DAY_END = os.getenv('NORN_PLANNING_DAY_END', '18:00')
PLANNING_WORKDAYS = frozenset(
	int(day.strip())
	for day in os.getenv('NORN_PLANNING_WORKDAYS', '0,1,2,3,4').split(',')
	if day.strip()
)

logging.basicConfig(level=os.getenv('LOG_LEVEL', 'INFO'))
logger = logging.getLogger('norn')
