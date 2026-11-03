import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from fastapi import HTTPException


lock = threading.Lock()
blocked_until = 0.0


def rate_limit_error(seconds: int) -> HTTPException:
    return HTTPException(
        429,
        f'Geminiの利用制限に達しました。少なくとも{seconds}秒ほど待ってから再度お試しください。'
        '予定・タスクの操作が一部保存済みの場合があるため、再送前に現在の一覧も確認してください。'
        '時間をおいても続く場合はGemini側の利用枠を確認してください。',
        headers={'Retry-After': str(seconds)},
    )


def check_llm_available() -> None:
    with lock:
        remaining = math.ceil(blocked_until - time.monotonic())
    if remaining > 0:
        raise rate_limit_error(remaining)


def check_llm_response(response) -> None:
    global blocked_until
    if getattr(response, 'status_code', None) != 429:
        return
    value = getattr(response, 'headers', {}).get('Retry-After', '')
    seconds = 60
    try:
        if value.strip().isdigit():
            seconds = int(value)
        elif value:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = math.ceil((retry_at - datetime.now(timezone.utc)).total_seconds())
    except (ValueError, TypeError, OverflowError):
        pass
    seconds = max(1, min(seconds, 86400))
    with lock:
        blocked_until = max(blocked_until, time.monotonic() + seconds)
    raise rate_limit_error(seconds)