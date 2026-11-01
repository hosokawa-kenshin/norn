from types import SimpleNamespace

from fastapi.testclient import TestClient

import app as norn_app


def test_status_reports_local_stt_and_unconfigured_llm():
    norn_app.LLM_MODEL = ''
    with TestClient(norn_app.app) as client:
        response = client.get('/api/status')

    assert response.status_code == 200
    assert response.json()['stt']['engine'] == 'faster-whisper'
    assert response.json()['llm']['configured'] is False


def test_transcription_uses_model_and_returns_text(monkeypatch, tmp_path):
    class FakeModel:
        def transcribe(self, path, **kwargs):
            assert path
            assert kwargs['language'] == 'ja'
            segments = iter([SimpleNamespace(text=' 今日は予定を確認します。')])
            return segments, SimpleNamespace(language='ja', duration=1.25)

    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(norn_app, 'get_transcription_model', lambda: FakeModel())
    with TestClient(norn_app.app) as client:
        response = client.post(
            '/api/transcribe',
            files={'audio': ('sample.webm', b'fake audio', 'audio/webm')},
        )

    assert response.status_code == 200
    assert response.json() == {
        'text': '今日は予定を確認します。',
        'language': 'ja',
        'duration': 1.25,
    }


def test_chat_saves_messages_and_returns_reply(monkeypatch, tmp_path):
    async def fake_reply(messages):
        assert messages[-1] == {'role': 'user', 'content': '明日の予定を教えて'}
        return '明日の予定を確認します。'

    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(norn_app, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(norn_app, 'generate_llm_reply', fake_reply)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '明日の予定を教えて'})
        conversation_id = response.json()['conversation_id']
        history = client.get(f'/api/conversations/{conversation_id}/messages')

    assert response.status_code == 200
    assert response.json()['assistant_message']['content'] == '明日の予定を確認します。'
    assert [message['role'] for message in history.json()] == ['user', 'assistant']


def test_chat_requires_llm_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(norn_app, 'LLM_MODEL', '')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': 'こんにちは'})

    assert response.status_code == 503


def test_event_crud_stores_japan_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        created = client.post('/api/events', json={
            'title': '定例ミーティング',
            'description': '週次の確認',
            'start_at': '2026-11-02T10:00',
            'end_at': '2026-11-02T11:00',
        })
        event_id = created.json()['id']
        listed = client.get('/api/events', params={
            'start': '2026-11-02T00:00:00+09:00',
            'end': '2026-11-03T00:00:00+09:00',
        })
        updated = client.put(f'/api/events/{event_id}', json={
            'title': '定例ミーティング（変更）',
            'description': '',
            'start_at': '2026-11-02T11:00',
            'end_at': '2026-11-02T12:00',
        })
        deleted = client.delete(f'/api/events/{event_id}')
        remaining = client.get('/api/events')

    assert created.status_code == 201
    assert created.json()['start_at'].endswith('+09:00')
    assert len(listed.json()) == 1
    assert updated.json()['title'] == '定例ミーティング（変更）'
    assert deleted.status_code == 204
    assert remaining.json() == []


def test_event_rejects_overlapping_times(monkeypatch, tmp_path):
    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        first = client.post('/api/events', json={
            'title': '予定A',
            'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        })
        overlap = client.post('/api/events', json={
            'title': '予定B',
            'start_at': '2026-11-02T10:30:00+09:00',
            'end_at': '2026-11-02T11:30:00+09:00',
        })

    assert first.status_code == 201
    assert overlap.status_code == 409
    assert '予定A' in overlap.json()['detail']


def test_event_requires_end_after_start(monkeypatch, tmp_path):
    monkeypatch.setattr(norn_app, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/events', json={
            'title': '時刻不正',
            'start_at': '2026-11-02T11:00:00+09:00',
            'end_at': '2026-11-02T10:00:00+09:00',
        })

    assert response.status_code == 422