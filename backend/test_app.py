import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

import app as norn_app
import chat as chat_module
import database as database_module
import events as events_module
import settings as settings_module
import voice as voice_module


def test_status_reports_local_stt_and_unconfigured_llm():
    settings_module.LLM_MODEL = ''
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

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(voice_module, 'get_transcription_model', lambda: FakeModel())
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

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'generate_llm_reply', fake_reply)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '明日の予定を教えて'})
        conversation_id = response.json()['conversation_id']
        history = client.get(f'/api/conversations/{conversation_id}/messages')

    assert response.status_code == 200
    assert response.json()['assistant_message']['content'] == '明日の予定を確認します。'
    assert [message['role'] for message in history.json()] == ['user', 'assistant']


def test_chat_requires_llm_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', '')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': 'こんにちは'})

    assert response.status_code == 503


def test_chat_executes_gemini_schedule_tool_without_approval(monkeypatch, tmp_path):
    tool_response = {
        'choices': [{
            'message': {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': 'call-create-event',
                    'type': 'function',
                    'function': {
                        'name': 'create_event',
                        'arguments': json.dumps({
                            'title': 'Gemini連携テスト',
                            'description': '承認操作なしで追加',
                            'start_at': '2026-11-02T14:00:00+09:00',
                            'end_at': '2026-11-02T15:00:00+09:00',
                        }),
                    },
                }],
            },
        }],
    }
    final_response = {
        'choices': [{'message': {'role': 'assistant', 'content': '明日14時からの予定を追加しました。'}}],
    }
    responses = iter([tool_response, final_response])
    request_payloads = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            request_payloads.append(json)
            return SimpleNamespace(is_error=False, json=lambda: next(responses))

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '明日14時から15時にGemini連携テストを追加して'})
        events = client.get('/api/events').json()

    assert response.status_code == 200
    assert response.json()['assistant_message']['content'] == '明日14時からの予定を追加しました。'
    assert len(events) == 1
    assert events[0]['title'] == 'Gemini連携テスト'
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert tool_names == {'list_events', 'create_event', 'update_event', 'delete_event'}
    assert any(message.get('role') == 'tool' for message in request_payloads[1]['messages'])


def test_schedule_tools_update_and_delete_without_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        created = client.post('/api/events', json={
            'title': '元の予定',
            'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        }).json()

        updated = events_module.execute_schedule_tool('update_event', {
            'event_id': created['id'],
            'title': '変更後の予定',
        })
        deleted = events_module.execute_schedule_tool('delete_event', {'event_id': created['id']})
        remaining = client.get('/api/events').json()

    assert updated['ok'] is True
    assert updated['event']['title'] == '変更後の予定'
    assert deleted['ok'] is True
    assert remaining == []


def test_schedule_tools_reject_unregistered_operations():
    result = events_module.execute_schedule_tool('delete_database', {})

    assert result['ok'] is False


def test_event_crud_stores_japan_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
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
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
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
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/events', json={
            'title': '時刻不正',
            'start_at': '2026-11-02T11:00:00+09:00',
            'end_at': '2026-11-02T10:00:00+09:00',
        })

    assert response.status_code == 422