import csv
from pathlib import Path

from settings import DATA_DIR
from voice_metrics import METRIC_FIELDS

CSV_PATH = DATA_DIR / 'voice-latency.csv'
LABELS = {
    'listening_ms': '聞き取り（発話）',
    'stt_ms': 'STT',
    'chat_ms': 'チャット往復',
    'server_ms': 'バックエンド合計',
    'llm_ms': 'LLM待ち時間（合計）',
    'processing_ms': 'バックエンド処理（LLM以外）',
    'tts_ms': 'TTS（準備・生成・受信）',
    'playback_ms': '音声再生',
    'total_ms': '発話開始から再生終了まで',
}


def summarize_csv(path: Path) -> tuple[int, dict[str, float]]:
    with path.open(encoding='utf-8', newline='') as csv_file:
        rows = list(csv.DictReader(csv_file))
    if not rows:
        raise ValueError(f'計測データがありません: {path}')
    averages = {
        field: sum(float(row[field]) for row in rows) / len(rows)
        for field in METRIC_FIELDS
    }
    return len(rows), averages


def main() -> int:
    if not CSV_PATH.is_file():
        print(f'計測CSVが見つかりません: {CSV_PATH}')
        return 1
    try:
        count, averages = summarize_csv(CSV_PATH)
    except ValueError as error:
        print(error)
        return 1
    print(f'計測件数: {count}')
    for field in METRIC_FIELDS:
        print(f'{LABELS[field]}: {averages[field] / 1000:.2f} 秒')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
