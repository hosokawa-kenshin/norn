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