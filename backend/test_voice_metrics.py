import csv

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

import analyze_voice_latency
import app as norn_app
import voice_metrics


def test_metrics_endpoint_is_registered():
    assert '/api/voice-metrics' in norn_app.app.openapi()['paths']


def test_voice_metrics_are_saved_without_transcript_and_averaged(monkeypatch, tmp_path):
    csv_path = tmp_path / 'voice-latency.csv'
    monkeypatch.setattr(voice_metrics, 'CSV_PATH', csv_path)
    app = FastAPI()
    app.include_router(voice_metrics.router)
    payload = {field: float(index * 100) for index, field in enumerate(voice_metrics.METRIC_FIELDS, 1)}

    with TestClient(app) as client:
        response = client.post('/api/voice-metrics', json=payload)

    assert response.status_code == 200
    with csv_path.open(encoding='utf-8', newline='') as csv_file:
        rows = list(csv.DictReader(csv_file))
    assert len(rows) == 1
    assert set(rows[0]) == set(voice_metrics.CSV_FIELDS)
    assert 'text' not in rows[0]
    count, averages = analyze_voice_latency.summarize_csv(csv_path)
    assert count == 1
    assert averages == payload


def test_voice_metrics_reject_transcript_and_invalid_durations(tmp_path, monkeypatch):
    monkeypatch.setattr(voice_metrics, 'CSV_PATH', tmp_path / 'voice-latency.csv')
    app = FastAPI()
    app.include_router(voice_metrics.router)
    payload = {field: 10 for field in voice_metrics.METRIC_FIELDS}

    with TestClient(app) as client:
        transcript_response = client.post('/api/voice-metrics', json={**payload, 'text': 'not stored'})
        negative_response = client.post('/api/voice-metrics', json={**payload, 'stt_ms': -1})

    assert transcript_response.status_code == 422
    assert negative_response.status_code == 422
    assert not (tmp_path / 'voice-latency.csv').exists()


def test_analyzer_rejects_empty_csv(tmp_path):
    path = tmp_path / 'empty.csv'
    path.write_text(','.join(voice_metrics.CSV_FIELDS) + '\n', encoding='utf-8')

    with pytest.raises(ValueError, match='計測データがありません'):
        analyze_voice_latency.summarize_csv(path)


def test_analyzer_prints_average_in_seconds(monkeypatch, tmp_path, capsys):
    path = tmp_path / 'voice-latency.csv'
    row = {field: 1000 for field in voice_metrics.METRIC_FIELDS}
    with path.open('w', encoding='utf-8', newline='') as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=voice_metrics.CSV_FIELDS)
        writer.writeheader()
        writer.writerow({'timestamp': '2026-11-04T00:00:00+00:00', **row})
    monkeypatch.setattr(analyze_voice_latency, 'CSV_PATH', path)

    assert analyze_voice_latency.main() == 0
    output = capsys.readouterr().out
    assert '計測件数: 1' in output
    assert 'STT: 1.00 秒' in output
