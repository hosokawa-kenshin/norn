import json
import sqlite3
import stat
import struct
import uuid
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import pytest

import app as norn_app
import chat as chat_module
import database as database_module
import events as events_module
import google_calendar as google_calendar_module
import memory as memory_module
import planning as planning_module
import settings as settings_module
import tasks as tasks_module
import voice as voice_module
import tts as tts_module


@pytest.fixture(autouse=True)
def disable_real_speech_worker(monkeypatch, tmp_path):
    monkeypatch.setenv('NORN_TTS_ENABLED', '0')
    monkeypatch.setattr(chat_module, 'CHARACTER_PATH', tmp_path / 'character.json')


def test_missing_character_profile_uses_default_persona():
    prompt = chat_module.build_system_prompt()
    assert chat_module.DEFAULT_PERSONA in prompt
    assert chat_module.SYSTEM_PROMPT in prompt


@pytest.mark.parametrize('contents', ['{', '[]', '{}', '{"name":22}', 'あ' * 32769])
def test_invalid_character_profile_is_not_silently_ignored(contents):
    chat_module.CHARACTER_PATH.write_text(contents, encoding='utf-8')
    with pytest.raises(chat_module.HTTPException) as error:
        chat_module.build_system_prompt()
    assert error.value.status_code == 503


def test_character_profile_reloads_between_chat_requests(monkeypatch, tmp_path):
    payloads = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            return SimpleNamespace(is_error=False, json=lambda: {
                'choices': [{'message': {'content': 'こんにちは。'}}],
            })

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        for name in ['設定テスト一号', '設定テスト二号']:
            chat_module.CHARACTER_PATH.write_text(
                json.dumps({'名前': name, '話し方': '自然な口調'}, ensure_ascii=False), encoding='utf-8',
            )
            response = client.post('/api/chat', json={'content': 'こんにちは'})
            assert response.status_code == 200
    assert '設定テスト一号' in payloads[0]['messages'][0]['content']
    assert '設定テスト二号' in payloads[1]['messages'][0]['content']
    assert '設定テスト一号' not in payloads[1]['messages'][0]['content']
    for payload in payloads:
        assert chat_module.SYSTEM_PROMPT in payload['messages'][0]['content']
        assert chat_module.DEFAULT_PERSONA not in payload['messages'][0]['content']
        assert payload['tools']


def test_speech_returns_wav_without_saving_reply_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        calls = []

        def fake_synthesize(text):
            calls.append(text)
            return b'RIFFtest-wave', 0.56

        monkeypatch.setattr(tts_module.speech, 'synthesize', fake_synthesize)
        response = client.post('/api/speech', json={'text': 'こんにちは。'})
    assert response.status_code == 200
    assert response.content == b'RIFFtest-wave'
    assert response.headers['content-type'] == 'audio/wav'
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['x-synthesis-seconds'] == '0.56'
    assert calls == ['こんにちは。']


def test_speech_status_loading_and_unavailable_do_not_block_chat(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        tts_module.speech.state = {'state': 'loading', 'device': 'cuda'}
        assert client.get('/api/speech/status').json()['state'] == 'loading'
        assert client.post('/api/speech', json={'text': 'テスト'}).status_code == 503
        assert client.get('/api/status').status_code == 200
        assert client.post('/api/speech', json={'text': '   '}).status_code == 422
        assert client.post('/api/speech', json={'text': 'あ' * 4001}).status_code == 422


def test_speech_worker_reuses_process_and_rejects_concurrent_requests():
    from unittest.mock import MagicMock
    import base64

    service = tts_module.SpeechService()
    service.state = {'state': 'ready'}
    service.process = MagicMock()
    service.process.poll.return_value = None
    for _ in range(2):
        service.responses.put(json.dumps({'audio': base64.b64encode(b'RIFF').decode(), 'seconds': 0.5}))
        assert service.synthesize('テスト') == (b'RIFF', 0.5)
    assert service.process.stdin.write.call_count == 2
    service.lock.acquire()
    try:
        with pytest.raises(tts_module.HTTPException) as error:
            service.synthesize('テスト')
        assert error.value.status_code == 429
    finally:
        service.lock.release()


def test_speech_worker_failure_keeps_text_api_available(monkeypatch, tmp_path):
    from unittest.mock import MagicMock

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        service = tts_module.speech
        service.state = {'state': 'ready'}
        service.process = MagicMock()
        service.process.poll.return_value = None
        service.responses.put(json.dumps({'error': 'synthetic failure'}))
        assert client.post('/api/speech', json={'text': 'テスト'}).status_code == 502
        assert client.get('/api/status').status_code == 200
        service.responses.put(None)
        assert client.post('/api/speech', json={'text': 'テスト'}).status_code == 503
        assert service.state['state'] == 'failed'


async def no_op_memory_extraction(*args, **kwargs):
    return None


def fake_memory_embeddings(texts):
    vectors = []
    for text in texts:
        normalized = text.casefold()
        vector = [0.0] * 10
        if any(term in normalized for term in ('祖母', 'おばあちゃん', 'ばあちゃん')):
            vector[0] = 1.0
        if any(term in normalized for term in ('祖父', 'じいちゃん', 'おじいちゃん')):
            vector[1] = 1.0
        if '風車' in normalized:
            vector[2] = 1.0
        if '温泉' in normalized or '長野' in normalized:
            vector[3] = 1.0
        if '姉' in normalized or '兄' in normalized or '楽器' in normalized:
            vector[4] = 1.0
        if '上司' in normalized or '設計' in normalized:
            vector[5] = 1.0
        if 'コーヒー' in normalized or '浅煎り' in normalized:
            vector[6] = 1.0
        if '青い' in normalized or '青色' in normalized:
            vector[8] = 1.0
        if '旅行' in normalized or '出かけ' in normalized:
            vector[9] = 1.0
        if not any(vector):
            vector[7] = 1.0
        vectors.append(struct.pack('10f', *vector))
    return vectors


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
    async def fake_reply(messages, conversation_id=None):
        assert messages[-1] == {'role': 'user', 'content': '明日の予定を教えて'}
        return '明日の予定を確認します。'

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'generate_llm_reply', fake_reply)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
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
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '明日14時から15時にGemini連携テストを追加して'})
        events = client.get('/api/events').json()

    assert response.status_code == 200
    assert '予定を追加しました' in response.json()['assistant_message']['content']
    assert len(events) == 1
    assert events[0]['title'] == 'Gemini連携テスト'
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert tool_names == {
        'list_events', 'create_event', 'update_event', 'delete_event',
        'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task',
        'find_task_slots', 'schedule_task', 'unschedule_task',
        'save_memory', 'search_memories', 'update_memory', 'forget_memory',
        'list_goals', 'create_goal', 'update_goal', 'delete_goal',
        'list_projects', 'create_project', 'update_project', 'delete_project',
    }
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


def test_event_rejects_overlap_with_future_recurring_occurrence(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        series = client.post('/api/events', json={
            'title': '毎週の会議', 'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
            'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'],
        })
        overlap = client.post('/api/events', json={
            'title': '別の予定', 'start_at': '2026-11-09T10:30:00+09:00',
            'end_at': '2026-11-09T11:30:00+09:00',
        })

    assert series.status_code == 201
    assert overlap.status_code == 409
    assert '毎週の会議' in overlap.json()['detail']


def test_event_requires_end_after_start(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/events', json={
            'title': '時刻不正',
            'start_at': '2026-11-02T11:00:00+09:00',
            'end_at': '2026-11-02T10:00:00+09:00',
        })

    assert response.status_code == 422


def test_task_crud_status_and_japan_timezone(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        created = client.post('/api/tasks', json={
            'title': 'レポートを書く',
            'description': '結果をまとめる',
            'due_at': '2026-11-05T17:00',
            'estimated_minutes': 45,
            'priority': 'high',
        })
        task_id = created.json()['id']
        in_progress = client.patch(f'/api/tasks/{task_id}', json={'status': 'in_progress'})
        completed = tasks_module.execute_task_tool('complete_task', {'task_id': task_id})
        listed = client.get('/api/tasks', params={'status': 'done'})
        deleted = client.delete(f'/api/tasks/{task_id}')
        remaining = client.get('/api/tasks')

    assert created.status_code == 201
    assert created.json()['due_at'].endswith('+09:00')
    assert created.json()['estimated_minutes'] == 45
    assert in_progress.json()['status'] == 'in_progress'
    assert completed['ok'] is True
    assert listed.json()[0]['status'] == 'done'
    assert listed.json()[0]['completed_at'] is not None
    assert deleted.status_code == 204
    assert remaining.json() == []


def test_tasks_filter_by_status_and_due_date(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        client.post('/api/tasks', json={
            'title': '今週の作業',
            'due_at': '2026-11-03T17:00:00+09:00',
        })
        client.post('/api/tasks', json={
            'title': '来週の作業',
            'due_at': '2026-11-10T17:00:00+09:00',
        })
        response = client.get('/api/tasks', params={
            'status': 'open',
            'due_before': '2026-11-05T00:00:00+09:00',
        })

    assert response.status_code == 200
    assert [task['title'] for task in response.json()] == ['今週の作業']


def test_task_rejects_invalid_estimate_and_unknown_status(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        invalid_estimate = client.post('/api/tasks', json={
            'title': '不正な見積もり',
            'estimated_minutes': 0,
        })
        created = client.post('/api/tasks', json={'title': '状態テスト'})
        invalid_status = client.patch(
            f"/api/tasks/{created.json()['id']}",
            json={'status': 'sleeping'},
        )

    assert invalid_estimate.status_code == 422
    assert invalid_status.status_code == 422


def test_chat_executes_gemini_task_tool_without_approval(monkeypatch, tmp_path):
    tool_response = {
        'choices': [{
            'message': {
                'role': 'assistant',
                'content': None,
                'tool_calls': [{
                    'id': 'call-create-task',
                    'type': 'function',
                    'function': {
                        'name': 'create_task',
                        'arguments': json.dumps({
                            'title': 'Geminiタスクテスト',
                            'due_at': '2026-11-06T17:00:00+09:00',
                            'estimated_minutes': 30,
                            'priority': 'high',
                        }),
                    },
                }],
            },
        }],
    }
    final_response = {
        'choices': [{'message': {'role': 'assistant', 'content': 'タスクを追加しました。'}}],
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
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '金曜までに30分の高優先度タスクを追加して'})
        tasks = client.get('/api/tasks').json()

    assert response.status_code == 200
    assert 'タスクを追加しました' in response.json()['assistant_message']['content']
    assert len(tasks) == 1
    assert tasks[0]['title'] == 'Geminiタスクテスト'
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert {
        'create_task', 'update_task', 'complete_task', 'delete_task', 'list_tasks',
        'find_task_slots', 'schedule_task', 'unschedule_task',
    } <= tool_names
    assert any(message.get('role') == 'tool' for message in request_payloads[1]['messages'])


def test_task_slots_avoid_events_and_other_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    workday = (now + timedelta(days=1)).date()
    while workday.weekday() >= 5:
        workday += timedelta(days=1)
    workday_text = workday.isoformat()
    range_start = datetime.combine(workday, time(9), ZoneInfo('Asia/Tokyo'))
    range_end = datetime.combine(workday, time(18), ZoneInfo('Asia/Tokyo'))

    with TestClient(norn_app.app) as client:
        event = client.post('/api/events', json={
            'title': '朝会',
            'start_at': f'{workday_text}T09:00:00+09:00',
            'end_at': f'{workday_text}T10:00:00+09:00',
        })
        first = client.post('/api/tasks', json={
            'title': '集中作業A',
            'estimated_minutes': 60,
            'due_at': f'{workday_text}T18:00:00+09:00',
        }).json()
        first_slots = client.get(f"/api/tasks/{first['id']}/available-slots", params={
            'from_at': range_start.isoformat(), 'through_at': range_end.isoformat(),
        })
        first_slot = first_slots.json()['slots'][0]
        first_assignment = client.put(f"/api/tasks/{first['id']}/schedule", json={'start_at': first_slot['start_at']})

        second = client.post('/api/tasks', json={
            'title': '集中作業B',
            'estimated_minutes': 30,
            'due_at': f'{workday_text}T18:00:00+09:00',
        }).json()
        invalid_increment = client.put(f"/api/tasks/{second['id']}/schedule", json={
            'start_at': f'{workday_text}T12:07:00+09:00',
        })
        second_slots = client.get(f"/api/tasks/{second['id']}/available-slots", params={
            'from_at': range_start.isoformat(), 'through_at': range_end.isoformat(),
        })
        second_assignment = client.put(f"/api/tasks/{second['id']}/schedule", json={
            'start_at': f'{workday_text}T10:30:00+09:00',
        })
        next_assignment = client.put(f"/api/tasks/{second['id']}/schedule", json={
            'start_at': second_slots.json()['slots'][0]['start_at'],
        })
        allocations = client.get('/api/task-allocations', params={
            'start': range_start.isoformat(), 'end': range_end.isoformat(),
        })
        completed = client.patch(f"/api/tasks/{first['id']}", json={'status': 'done'})
        remaining_allocation = client.delete(f"/api/tasks/{second['id']}/schedule")
        empty_allocations = client.get('/api/task-allocations', params={
            'start': range_start.isoformat(), 'end': range_end.isoformat(),
        })

    assert event.status_code == 201
    assert first_slot['start_at'].endswith('T10:00:00+09:00')
    assert first_assignment.status_code == 200
    assert second_slots.json()['slots'][0]['start_at'].endswith('T11:00:00+09:00')
    assert invalid_increment.status_code == 409
    assert second_assignment.status_code == 409
    assert next_assignment.status_code == 200
    assert len(allocations.json()) == 2
    assert completed.json()['scheduled_start'] is None
    assert remaining_allocation.status_code == 204
    assert empty_allocations.json() == []


def test_gemini_auto_assigns_new_task_to_earliest_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    now = datetime.now(ZoneInfo('Asia/Tokyo'))
    workday = (now + timedelta(days=1)).date()
    while workday.weekday() >= 5:
        workday += timedelta(days=1)
    workday_text = workday.isoformat()
    calls = []

    def tool_response(name, arguments):
        return {'choices': [{'message': {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{
                'id': f'call-{len(calls)}',
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)},
            }],
        }}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            if len(calls) == 1:
                reply = tool_response('create_task', {
                    'title': '自動割当テスト',
                    'estimated_minutes': 30,
                    'due_at': f'{workday_text}T18:00:00+09:00',
                })
            elif len(calls) == 2:
                created_result = json_module.loads(next(message['content'] for message in reversed(json['messages']) if message['role'] == 'tool'))
                reply = tool_response('find_task_slots', {
                    'task_id': created_result['task']['id'],
                    'from_at': f'{workday_text}T09:00:00+09:00',
                    'through_at': f'{workday_text}T18:00:00+09:00',
                    'limit': 1,
                })
            elif len(calls) == 3:
                slots_result = json_module.loads(next(message['content'] for message in reversed(json['messages']) if message['role'] == 'tool'))
                reply = tool_response('schedule_task', {
                    'task_id': slots_result['task_id'],
                    'start_at': slots_result['slots'][0]['start_at'],
                })
            else:
                reply = {'choices': [{'message': {'role': 'assistant', 'content': 'タスクを最短の空き時間に割り当てました。'}}]}
            return SimpleNamespace(is_error=False, json=lambda: reply)

    import json as json_module

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '見積30分、期限は指定日の18時のタスクを追加して予定に割り当てて'})
        tasks = client.get('/api/tasks').json()

    assert response.status_code == 200
    assert '最短の空き時間に割り当てました' in response.json()['assistant_message']['content']
    assert len(calls) == 4
    assert len(tasks) == 1
    assert tasks[0]['scheduled_start'].endswith('T09:00:00+09:00')


def test_gemini_saves_and_recalls_memory_across_conversations(monkeypatch, tmp_path):
    def tool_response(call_id, name, arguments):
        return {'choices': [{'message': {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)},
            }],
        }}]}

    responses = iter([
        tool_response('call-save-memory', 'save_memory', {
            'category': 'episode',
            'content': '子どもの頃、祖父と長野の温泉へ行った思い出がある。',
            'source_type': 'explicit',
        }),
        {'choices': [{'message': {'role': 'assistant', 'content': 'その思い出を覚えておくね。'}}]},
        tool_response('call-search-memory', 'search_memories', {
            'query': '祖父 長野 温泉',
            'category': 'episode',
        }),
        {'choices': [{'message': {'role': 'assistant', 'content': 'おじいさまと長野の温泉へ行った思い出だね。'}}]},
    ])
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
    monkeypatch.setattr(memory_module, 'embed_texts', fake_memory_embeddings)
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        remembered = client.post('/api/chat', json={'content': '子どもの頃の大切な思い出を話す。'})
        recalled = client.post('/api/chat', json={'content': '昔、祖父と行った場所を覚えてる？'})
        memories = client.get('/api/memories').json()

    assert remembered.status_code == 200
    assert recalled.status_code == 200
    assert 'おじいさまと長野の温泉' in recalled.json()['assistant_message']['content']
    assert len(memories) == 1
    assert memories[0]['source_conversation_id'] == remembered.json()['conversation_id']
    memory_tool_result = next(
        message for message in request_payloads[3]['messages']
        if message.get('role') == 'tool' and message.get('tool_call_id') == 'call-search-memory'
    )
    assert '長野の温泉' in memory_tool_result['content']


def test_memory_can_be_corrected_and_forgotten_without_a_gui(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(memory_module, 'embed_texts', fake_memory_embeddings)
    with TestClient(norn_app.app) as client:
        created = memory_module.execute_memory_tool('save_memory', {
            'category': 'preference',
            'content': 'コーヒーが好き。',
            'source_type': 'inferred',
        }, 'first-thread')
        memory_id = created['memory']['id']
        corrected = memory_module.execute_memory_tool('update_memory', {
            'memory_id': memory_id,
            'content': '紅茶が好き。',
        }, 'second-thread')
        listed = client.get('/api/memories').json()
        forgotten = memory_module.execute_memory_tool('forget_memory', {'memory_id': memory_id})
        remaining = client.get('/api/memories').json()

    assert created['ok'] is True
    assert corrected['memory']['content'] == '紅茶が好き。'
    assert corrected['memory']['source_type'] == 'explicit'
    assert len(listed) == 1
    assert forgotten['summary'] == '記憶を忘れました。'
    assert remaining == []


def test_chat_automatically_extracts_memory_without_explicit_request(monkeypatch, tmp_path):
    json_module = json
    chat_response = {'choices': [{'message': {'role': 'assistant', 'content': 'それは素敵な思い出だね。'}}]}
    request_payloads = []
    extraction_batches = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            request_payloads.append(json)
            if json.get('response_format'):
                batch = json_module.loads(json['messages'][1]['content'])
                extraction_batches.append(batch)
                memories = []
                if len(extraction_batches) == 1:
                    source = next(message for message in batch if message['role'] == 'user')
                    memories.append({
                        'category': 'episode',
                        'content': '子どもの頃、祖母と青い風車を見に行った思い出がある。',
                        'source_type': 'explicit',
                        'source_conversation_id': source['conversation_id'],
                    })
                extraction_response = {
                    'choices': [{'message': {'role': 'assistant', 'content': json_module.dumps({'memories': memories}, ensure_ascii=False)}}],
                }
                response = extraction_response
            else:
                response = chat_response
            return SimpleNamespace(is_error=False, json=lambda: response)

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(memory_module, 'embed_texts', fake_memory_embeddings)
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        first = client.post('/api/chat', json={
            'content': '小学生のころ祖母と青い風車を見に行ったのが、今でも好きな思い出なんだ。',
        })
        second = client.post('/api/chat', json={'content': '今日は別の話をしよう。'})
        connection = database_module.open_database()
        connection.execute(
            'UPDATE memory_extraction_state SET last_attempted_at = ? WHERE id = 1',
            ('2000-01-01T00:00:00+00:00',),
        )
        connection.commit()
        connection.close()
        third = client.post('/api/chat', json={'content': '最近の出来事について話したい。'})
        memories = client.get('/api/memories').json()

    assert first.status_code == second.status_code == third.status_code == 200
    assert len([payload for payload in request_payloads if payload.get('response_format')]) == 2
    assert len(extraction_batches) == 2
    batched_user_content = {
        message['content'] for message in extraction_batches[1] if message['role'] == 'user'
    }
    assert {'今日は別の話をしよう。', '最近の出来事について話したい。'} <= batched_user_content
    assert len(memories) == 1
    assert memories[0]['content'] == '子どもの頃、祖母と青い風車を見に行った思い出がある。'
    assert memories[0]['source_conversation_id'] == first.json()['conversation_id']


def test_memory_rejects_credentials(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        result = memory_module.execute_memory_tool('save_memory', {
            'category': 'fact',
            'content': 'API key: sk-test-credential-value',
            'source_type': 'explicit',
        })
        memories = client.get('/api/memories').json()

    assert result['ok'] is False
    assert '認証情報' in result['error']
    assert memories == []


def test_memory_change_requests_are_excluded_from_automatic_extraction():
    assert chat_module.MEMORY_CHANGE_PATTERN.search('この話は保存しないで。')
    assert chat_module.MEMORY_CHANGE_PATTERN.search('祖父じゃなく叔父だよ。')
    assert chat_module.MEMORY_CHANGE_PATTERN.search('このことは覚えておいて。')
    assert chat_module.MEMORY_NON_ASSERTION_PATTERN.search('祖父と青い風車に行った話、覚えてる？')


def test_memory_extraction_skips_questions_and_their_replies(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app):
        connection = database_module.open_database()
        timestamp = database_module.now_iso()
        conversation_id = 'synthetic-memory-question'
        connection.execute(
            'INSERT INTO conversations (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)',
            (conversation_id, '架空質問', timestamp, timestamp),
        )
        connection.executemany(
            'INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)',
            [
                (conversation_id, 'user', '祖父と青い風車に行った話、覚えてる？', timestamp),
                (conversation_id, 'assistant', '祖父との思い出を確認するね。', timestamp),
            ],
        )
        connection.commit()
        connection.close()

        batch = chat_module.claim_memory_extraction_batch()
        connection = database_module.open_database()
        state = connection.execute(
            'SELECT last_processed_message_id FROM memory_extraction_state WHERE id = 1'
        ).fetchone()
        connection.close()

    assert batch is None
    assert state['last_processed_message_id'] == 2


def test_memory_search_matches_paraphrases_and_rejects_unrelated_queries(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(memory_module, 'embed_texts', fake_memory_embeddings)
    with TestClient(norn_app.app):
        grandmother = memory_module.save_memory_record(
            'episode', '子どもの頃、祖母と青い風車を見に行った。', 'explicit'
        )
        grandfather = memory_module.save_memory_record(
            'episode', '子どもの頃、祖父と長野の温泉に出かけた。', 'explicit'
        )
        work = memory_module.save_memory_record(
            'episode', '仕事で会った上司と風車の設計を話し合った。', 'explicit'
        )
        paraphrase = memory_module.search_memory_records(
            'おばあちゃんと昔見に行ったものは何？',
            query_variants=['祖母と青い風車を見に行った思い出'],
        )
        wrong_grandfather = memory_module.search_memory_records('祖父と昔見に行った青いものは何？')
        wrong_grandmother = memory_module.search_memory_records('祖母と長野で入った温泉はどこ？')
        variant_search = memory_module.execute_memory_tool('search_memories', {
            'query': '昔の家族旅行について',
            'query_variants': ['じいちゃんと旅行した場所'],
        })
        unrelated = memory_module.search_memory_records('今日の夕飯は何にする？')

    assert [memory['id'] for memory in paraphrase] == [grandmother['id']]
    assert wrong_grandfather == []
    assert wrong_grandmother == []
    assert paraphrase[0]['similarity'] >= memory_module.MIN_MEMORY_SIMILARITY
    assert variant_search['memories'][0]['id'] == grandfather['id']
    assert work['id'] not in {memory['id'] for memory in paraphrase}
    assert unrelated == []

    memory_module.update_memory_record(
        grandmother['id'], '祖父と青い風車を見に行った。', source_type='explicit'
    )
    corrected_search = memory_module.search_memory_records('おばあちゃんと昔見に行ったものは何？')
    assert grandmother['id'] not in {memory['id'] for memory in corrected_search}


def test_memory_search_falls_back_to_lexical_matching(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(memory_module, 'embed_texts', lambda texts: None)
    with TestClient(norn_app.app):
        saved = memory_module.save_memory_record(
            'episode', '祖母と温泉に行った。', 'explicit'
        )
        results = memory_module.search_memory_records('祖母と温泉に行った')

    assert [memory['id'] for memory in results] == [saved['id']]


def test_goals_projects_and_task_progress_api(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        goal_response = client.post('/api/goals', json={
            'title': '自作曲を完成させる',
            'description': '来年までに公開する',
            'target_date': '2027-12-31',
        })
        goal = goal_response.json()
        project_response = client.post('/api/projects', json={
            'goal_id': goal['id'],
            'title': 'アルバム制作',
            'next_action': '歌詞の下書きを作る',
        })
        project = project_response.json()
        first_task = client.post('/api/tasks', json={
            'title': 'Aメロを書く',
            'project_id': project['id'],
        }).json()
        second_task = client.post('/api/tasks', json={
            'title': 'コード進行を決める',
            'project_id': project['id'],
        }).json()
        completed = client.patch(f"/api/tasks/{first_task['id']}", json={'status': 'done'})
        projects = client.get('/api/projects', params={'goal_id': goal['id']})
        goals = client.get('/api/goals')
        updated_project = client.patch(f"/api/projects/{project['id']}", json={
            'next_action': 'サビのメロディを録る',
            'status': 'paused',
        })
        invalid_project = client.post('/api/projects', json={
            'goal_id': str(uuid.uuid4()),
            'title': '存在しない目標への案件',
        })
        deleted_project = client.delete(f"/api/projects/{project['id']}")
        unassigned_tasks = client.get('/api/tasks')
        goal_after_delete = client.get('/api/goals').json()[0]

    assert goal_response.status_code == 201
    assert project_response.status_code == 201
    assert first_task['project_id'] == project['id']
    assert completed.status_code == 200
    assert projects.json()[0]['task_count'] == 2
    assert projects.json()[0]['completed_task_count'] == 1
    assert projects.json()[0]['progress'] == 50
    assert goals.json()[0]['progress'] == 50
    assert goals.json()[0]['projects'][0]['next_action'] == '歌詞の下書きを作る'
    assert updated_project.json()['next_action'] == 'サビのメロディを録る'
    assert invalid_project.status_code == 422
    assert deleted_project.status_code == 204
    assert all(task['project_id'] is None for task in unassigned_tasks.json())
    assert goal_after_delete['projects'] == []


def test_gemini_creates_goal_and_linked_project_without_approval(monkeypatch, tmp_path):
    request_payloads = []

    def tool_response(call_id, name, arguments):
        return {'choices': [{'message': {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)},
            }],
        }}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            request_payloads.append(json)
            if len(request_payloads) == 1:
                reply = tool_response('call-create-goal', 'create_goal', {
                    'title': '自作曲を完成させる',
                    'target_date': '2027-12-31',
                })
            elif len(request_payloads) == 2:
                previous_result = json_module.loads(next(
                    message['content'] for message in reversed(json['messages'])
                    if message.get('role') == 'tool'
                ))
                reply = tool_response('call-create-project', 'create_project', {
                    'title': 'アルバム制作',
                    'goal_id': previous_result['goal']['id'],
                    'next_action': '歌詞の下書きを作る',
                })
            else:
                reply = {'choices': [{'message': {
                    'role': 'assistant', 'content': '目標に結びつけてプロジェクトを作成したよ。',
                }}]}
            return SimpleNamespace(is_error=False, json=lambda: reply)

    import json as json_module

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={
            'content': '「自作曲を完成させる」を目標にして、「アルバム制作」をプロジェクトとして追加して',
        })
        goals = client.get('/api/goals').json()

    assert response.status_code == 200
    assert len(request_payloads) == 3
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert {'create_goal', 'create_project', 'list_goals', 'list_projects'} <= tool_names
    assert len(goals) == 1
    assert goals[0]['projects'][0]['title'] == 'アルバム制作'
    assert goals[0]['projects'][0]['goal_id'] == goals[0]['id']


class FakeGoogleRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeGoogleCalendarService:
    def __init__(self, items=None, calendars=None):
        self.items = items or []
        self.calendars = calendars or [
            {'id': 'user@example.com', 'summary': '個人用', 'primary': True, 'accessRole': 'owner'},
            {'id': 'work@example.com', 'summary': '仕事', 'accessRole': 'writer'},
            {'id': 'readonly@example.com', 'summary': '閲覧のみ', 'accessRole': 'reader'},
        ]
        self.inserted = []
        self.inserted_calendar_ids = []
        self.list_options = []
        self.deleted = []

    def events(self):
        return self

    def calendarList(self):
        return self

    def list(self, **kwargs):
        if 'minAccessRole' in kwargs:
            return FakeGoogleRequest({'items': self.calendars})
        self.list_options.append(kwargs)
        return FakeGoogleRequest({'items': self.items, 'nextSyncToken': 'test-sync-token'})

    def insert(self, calendarId, body):
        self.inserted.append(body)
        self.inserted_calendar_ids.append(calendarId)
        return FakeGoogleRequest({
            **body, 'id': f'google-event-{len(self.inserted)}', 'etag': 'etag-1',
            'updated': '2026-11-01T00:00:00.000Z',
        })

    def update(self, **kwargs):
        return FakeGoogleRequest(kwargs['body'])

    def get(self, calendarId):
        for item in self.calendars:
            if calendarId in {item['id'], 'primary' if item.get('primary') else item['id']}:
                return FakeGoogleRequest(item)
        raise ValueError('calendar not found')

    def delete(self, **kwargs):
        self.deleted.append(kwargs)
        return FakeGoogleRequest({})


def test_google_calendar_imports_all_day_and_expands_recurring_series(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService([
        {
            'id': 'all-day', 'summary': '祝日', 'start': {'date': '2026-11-02'},
            'end': {'date': '2026-11-03'}, 'etag': 'etag-a',
        },
        {
            'id': 'weekly', 'summary': '週次会議', 'start': {'dateTime': '2026-10-26T10:00:00+09:00'},
            'end': {'dateTime': '2026-10-26T11:00:00+09:00'},
            'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'], 'etag': 'etag-b',
        },
    ])
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        result = google_calendar_module.sync_calendar(service)
        response = client.get('/api/events', params={
            'start': '2026-11-02T00:00:00+09:00', 'end': '2026-11-09T00:00:00+09:00',
        })

    assert result['ok'] is True
    events = response.json()
    assert [(event['title'], event['is_all_day']) for event in events] == [
        ('祝日', True), ('週次会議', False),
    ]
    assert events[0]['end_at'] == '2026-11-03T00:00:00+09:00'
    assert events[1]['start_at'] == '2026-11-02T10:00:00+09:00'
    assert events[1]['series_id']
    assert str(uuid.UUID(events[1]['id'])) == events[1]['id']
    assert events[1]['occurrence_id'] != events[1]['id']


def test_google_calendar_lists_only_writable_calendars(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService()
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(google_calendar_module, '_calendar_service', lambda: service)
    with TestClient(norn_app.app) as client:
        response = client.get('/api/google-calendar/calendars')

    assert response.status_code == 200
    result = response.json()
    assert result['selected_calendar_id'] == 'primary'
    assert [calendar['id'] for calendar in result['calendars']] == ['primary', 'work@example.com']
    assert result['calendars'][0]['primary'] is True


def test_google_calendar_selection_persists_and_syncs_selected_calendar(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService(items=[{
        'id': 'work-event', 'summary': '仕事の予定',
        'start': {'dateTime': '2026-11-02T10:00:00+09:00'},
        'end': {'dateTime': '2026-11-02T11:00:00+09:00'},
    }])
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(google_calendar_module, '_calendar_service', lambda: service)
    with TestClient(norn_app.app) as client:
        local_event = client.post('/api/events', json={
            'title': 'NORNから仕事へ', 'start_at': '2026-11-02T12:00:00+09:00',
            'end_at': '2026-11-02T13:00:00+09:00',
        })
        selected = client.put('/api/google-calendar/calendar', json={'calendar_id': 'work@example.com'})
        status = client.get('/api/google-calendar/status').json()
        events = client.get('/api/events').json()

    assert selected.status_code == 200
    assert selected.json()['calendar_id'] == 'work@example.com'
    assert selected.json()['sync']['ok'] is True
    assert status['calendar_id'] == 'work@example.com'
    assert service.list_options[0]['calendarId'] == 'work@example.com'
    assert local_event.status_code == 201
    assert service.inserted_calendar_ids == ['work@example.com']
    assert events[0]['title'] == '仕事の予定'


def test_google_calendar_rejects_read_only_calendar_selection(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService()
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(google_calendar_module, '_calendar_service', lambda: service)
    with TestClient(norn_app.app) as client:
        response = client.put('/api/google-calendar/calendar', json={'calendar_id': 'readonly@example.com'})
        status = client.get('/api/google-calendar/status').json()

    assert response.status_code == 403
    assert status['calendar_id'] == 'primary'


def test_google_calendar_cancelled_series_occurrence_is_not_displayed(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService([
        {
            'id': 'weekly', 'summary': '週次会議',
            'start': {'dateTime': '2026-11-02T10:00:00+09:00'},
            'end': {'dateTime': '2026-11-02T11:00:00+09:00'},
            'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'],
        },
        {
            'id': 'weekly-cancelled-occurrence', 'status': 'cancelled',
            'recurringEventId': 'weekly',
            'originalStartTime': {'dateTime': '2026-11-09T10:00:00+09:00'},
        },
    ])
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        sync_result = google_calendar_module.sync_calendar(service)
        cancelled_week = client.get('/api/events', params={
            'start': '2026-11-09T00:00:00+09:00', 'end': '2026-11-16T00:00:00+09:00',
        })
        active_week = client.get('/api/events', params={
            'start': '2026-11-16T00:00:00+09:00', 'end': '2026-11-23T00:00:00+09:00',
        })
        with database_module.open_database() as connection:
            mapped_events = [dict(row) for row in connection.execute(
                'SELECT google_event_id, recurring_event_id, original_start_at, deleted_at '
                'FROM events ORDER BY google_event_id',
            )]

    assert sync_result['ok'] is True, sync_result
    assert cancelled_week.json() == []
    assert any(
        row['google_event_id'] == 'weekly-cancelled-occurrence'
        and row['recurring_event_id'] == 'weekly'
        and row['deleted_at'] is not None
        for row in mapped_events
    )
    assert [event['start_at'] for event in active_week.json()] == ['2026-11-16T10:00:00+09:00']


def test_google_calendar_recurring_timezone_preserves_dst_wall_time(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService([{
        'id': 'los-angeles-weekly', 'summary': '西海岸の定例',
        'start': {'dateTime': '2026-03-01T09:00:00-08:00', 'timeZone': 'America/Los_Angeles'},
        'end': {'dateTime': '2026-03-01T10:00:00-08:00', 'timeZone': 'America/Los_Angeles'},
        'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=SU'],
    }])
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        result = google_calendar_module.sync_calendar(service)
        response = client.get('/api/events', params={
            'start': '2026-03-01T00:00:00+09:00', 'end': '2026-03-17T00:00:00+09:00',
        })
        with database_module.open_database() as connection:
            row = connection.execute(
                'SELECT * FROM events WHERE google_event_id = ?', ('los-angeles-weekly',),
            ).fetchone()
            outbound = google_calendar_module._google_payload(row)

    assert result['ok'] is True
    assert [event['start_at'] for event in response.json()] == [
        '2026-03-02T02:00:00+09:00',
        '2026-03-09T01:00:00+09:00',
        '2026-03-16T01:00:00+09:00',
    ]
    assert {event['recurrence_timezone'] for event in response.json()} == {'America/Los_Angeles'}
    assert outbound['start'] == {
        'dateTime': '2026-03-01T09:00:00-08:00', 'timeZone': 'America/Los_Angeles',
    }


def test_google_calendar_all_day_exdate_uses_date_only_boundaries(monkeypatch, tmp_path):
    service = FakeGoogleCalendarService([{
        'id': 'daily-holiday', 'summary': '連休',
        'start': {'date': '2026-11-02'}, 'end': {'date': '2026-11-03'},
        'recurrence': ['RRULE:FREQ=DAILY', 'EXDATE;VALUE=DATE:20261103'],
    }])
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        google_calendar_module.sync_calendar(service)
        response = client.get('/api/events', params={
            'start': '2026-11-02T00:00:00+09:00', 'end': '2026-11-05T00:00:00+09:00',
        })

    assert [event['start_at'] for event in response.json()] == [
        '2026-11-02T00:00:00+09:00', '2026-11-04T00:00:00+09:00',
    ]


def test_google_calendar_exports_all_day_and_recurrence(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    service = FakeGoogleCalendarService()
    with TestClient(norn_app.app) as client:
        created = client.post('/api/events', json={
            'title': '毎週の休み', 'start_at': '2026-11-02T00:00:00+09:00',
            'end_at': '2026-11-03T00:00:00+09:00', 'is_all_day': True,
            'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'],
        })
        result = google_calendar_module.sync_calendar(service)

    assert created.status_code == 201
    assert result['ok'] is True
    assert service.inserted == [{
        'start': {'date': '2026-11-02'}, 'end': {'date': '2026-11-03'},
        'summary': '毎週の休み', 'description': '',
        'recurrence': ['RRULE:FREQ=WEEKLY;BYDAY=MO'],
    }]


def test_google_calendar_first_sync_maps_one_exact_local_match(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    service = FakeGoogleCalendarService([{
        'id': 'existing-google-event', 'summary': '同じ予定', 'description': '同じメモ',
        'start': {'dateTime': '2026-11-02T10:00:00+09:00'},
        'end': {'dateTime': '2026-11-02T11:00:00+09:00'},
    }])
    with TestClient(norn_app.app) as client:
        created = client.post('/api/events', json={
            'title': '同じ予定', 'description': '同じメモ',
            'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        }).json()
        result = google_calendar_module.sync_calendar(service)
        events = client.get('/api/events').json()

    assert result['ok'] is True
    assert len(events) == 1
    assert events[0]['id'] == created['id']
    assert events[0]['google_event_id'] == 'existing-google-event'
    assert service.inserted == []


def test_google_calendar_conflict_pauses_until_user_resolution(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        created = client.post('/api/events', json={
            'title': '元の予定', 'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        }).json()
        with database_module.open_database() as connection:
            row = connection.execute('SELECT * FROM events WHERE id = ?', (created['id'],)).fetchone()
            snapshot = google_calendar_module._snapshot(google_calendar_module._canonical_local(row))
            connection.execute(
                "UPDATE events SET google_event_id = 'google-1', google_calendar_id = 'primary', "
                "sync_snapshot = ?, sync_status = 'synced' WHERE id = ?",
                (snapshot, created['id']),
            )
            connection.commit()
        client.put(f"/api/events/{created['id']}", json={
            'title': 'NORN側の変更', 'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        })
        service = FakeGoogleCalendarService([{
            'id': 'google-1', 'summary': 'Google側の変更',
            'start': {'dateTime': '2026-11-02T10:00:00+09:00'},
            'end': {'dateTime': '2026-11-02T11:00:00+09:00'}, 'etag': 'etag-2',
        }])

        result = google_calendar_module.sync_calendar(service)
        status = client.get('/api/google-calendar/status').json()
        paused_sync = google_calendar_module.sync_calendar(service)
        blocked_update = client.put(f"/api/events/{created['id']}", json={
            'title': '上書きしない', 'start_at': '2026-11-02T10:00:00+09:00',
            'end_at': '2026-11-02T11:00:00+09:00',
        })
        resolved = client.post(f"/api/google-calendar/conflicts/{created['id']}/resolve", json={'choice': 'google'})
        final_event = client.get('/api/events').json()[0]

    assert result['ok'] is True
    assert len(status['conflicts']) == 1
    assert paused_sync['paused'] is True
    assert len(service.list_options) == 1
    assert blocked_update.status_code == 409
    assert resolved.status_code == 200
    assert final_event['title'] == 'Google側の変更'


def test_google_calendar_recovers_expired_sync_token(monkeypatch, tmp_path):
    from googleapiclient.errors import HttpError

    class ExpiredRequest:
        def execute(self):
            raise HttpError(SimpleNamespace(status=410, reason='Gone'), b'expired', uri='https://example.test')

    class ExpiredTokenService(FakeGoogleCalendarService):
        def list(self, **kwargs):
            self.list_options.append(kwargs)
            if kwargs.get('syncToken'):
                return ExpiredRequest()
            return FakeGoogleRequest({'items': self.items, 'nextSyncToken': 'recovered-token'})

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    service = ExpiredTokenService([{
        'id': 'still-active', 'summary': '残っている予定',
        'start': {'dateTime': '2026-11-02T10:00:00+09:00'},
        'end': {'dateTime': '2026-11-02T11:00:00+09:00'},
    }])
    with TestClient(norn_app.app) as client:
        client.post('/api/events', json={
            'title': 'Google側で削除済み', 'start_at': '2026-11-03T10:00:00+09:00',
            'end_at': '2026-11-03T11:00:00+09:00',
        })
        with database_module.open_database() as connection:
            row = connection.execute('SELECT * FROM events').fetchone()
            snapshot = google_calendar_module._snapshot(google_calendar_module._canonical_local(row))
            connection.execute(
                "UPDATE events SET google_event_id = 'deleted-remotely', google_calendar_id = 'primary', "
                "sync_snapshot = ?, sync_status = 'synced' WHERE id = ?",
                (snapshot, row['id']),
            )
            connection.execute(
                "UPDATE google_calendar_sync_state SET sync_token = 'expired' WHERE calendar_id = 'primary'",
            )
            connection.commit()
        result = google_calendar_module.sync_calendar(service)
        events = client.get('/api/events').json()
        with database_module.open_database() as connection:
            deleted_mapping = connection.execute(
                'SELECT is_cancelled, deleted_at FROM events WHERE google_event_id = ?',
                ('deleted-remotely',),
            ).fetchone()

    assert result['ok'] is True
    assert service.list_options[0]['syncToken'] == 'expired'
    assert 'syncToken' not in service.list_options[1]
    assert service.deleted == []
    assert deleted_mapping['is_cancelled'] == 1
    assert deleted_mapping['deleted_at'] is not None
    assert [event['title'] for event in events] == ['残っている予定']


def test_google_calendar_authorize_uses_pkce_and_persists_state(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(google_calendar_module, 'GOOGLE_CLIENT_ID', 'desktop-client-id')
    monkeypatch.setattr(google_calendar_module, 'GOOGLE_CLIENT_SECRET', 'desktop-client-secret')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/google-calendar/authorize')
        query = parse_qs(urlparse(response.json()['authorization_url']).query)
        with database_module.open_database() as connection:
            state = connection.execute(
                'SELECT code_verifier FROM google_calendar_oauth_states WHERE state = ?',
                (query['state'][0],),
            ).fetchone()

    assert response.status_code == 200
    assert query['redirect_uri'] == ['http://127.0.0.1:8000/']
    assert query['code_challenge_method'] == ['S256']
    assert 'https://www.googleapis.com/auth/calendar.events' in query['scope'][0].split()
    assert 'https://www.googleapis.com/auth/calendar.calendarlist.readonly' in query['scope'][0].split()
    assert state is not None
    assert len(state['code_verifier']) >= 43


def test_google_calendar_token_file_is_owner_only_even_when_rewritten(monkeypatch, tmp_path):
    token_path = tmp_path / 'google-calendar-token.json'
    token_path.write_text('old token', encoding='utf-8')
    token_path.chmod(0o644)
    monkeypatch.setattr(google_calendar_module, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(google_calendar_module, 'GOOGLE_TOKEN_PATH', token_path)

    google_calendar_module._write_credentials(SimpleNamespace(to_json=lambda: 'new token'))

    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_legacy_event_table_migrates_google_fields_without_losing_rows(monkeypatch, tmp_path):
    database_path = tmp_path / 'legacy-events.sqlite3'
    connection = sqlite3.connect(database_path)
    connection.execute('''
        CREATE TABLE events (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            start_at TEXT NOT NULL, end_at TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    ''')
    connection.execute(
        'INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)',
        ('legacy-event', '既存予定', '', '2026-11-02T10:00:00+09:00',
         '2026-11-02T11:00:00+09:00', '2026-01-01', '2026-01-01'),
    )
    connection.commit()
    connection.close()
    monkeypatch.setattr(database_module, 'DB_PATH', database_path)

    database_module.initialize_database()
    with database_module.open_database() as migrated:
        row = migrated.execute('SELECT * FROM events WHERE id = ?', ('legacy-event',)).fetchone()
        columns = {column['name'] for column in migrated.execute('PRAGMA table_info(events)')}
        calendar = migrated.execute(
            'SELECT calendar_id FROM google_calendar_preferences WHERE id = 1',
        ).fetchone()

    assert row['title'] == '既存予定'
    assert row['is_all_day'] == 0
    assert row['recurrence_json'] == '[]'
    assert row['google_event_id'] is None
    assert 'conflict_payload' in columns
    assert calendar['calendar_id'] == 'primary'


def test_gemini_generates_multiple_tasks_for_new_project(monkeypatch, tmp_path):
    request_payloads = []
    json_module = json

    def tool_call(call_id, name, arguments):
        return {
            'id': call_id,
            'type': 'function',
            'function': {'name': name, 'arguments': json.dumps(arguments)},
        }

    def tool_response(call_id, name, arguments):
        return {'choices': [{'message': {
            'role': 'assistant', 'content': None,
            'tool_calls': [tool_call(call_id, name, arguments)],
        }}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            request_payloads.append(json)
            if len(request_payloads) == 1:
                reply = tool_response('call-create-project', 'create_project', {
                    'title': '架空ポッドキャスト制作',
                })
            elif len(request_payloads) == 2:
                created_project = json_module.loads(next(
                    message['content'] for message in reversed(json['messages'])
                    if message.get('role') == 'tool'
                ))
                reply = tool_response('call-list-project-tasks', 'list_tasks', {
                    'project_id': created_project['project']['id'],
                })
            elif len(request_payloads) == 3:
                list_arguments = json_module.loads(next(
                    tool_call['function']['arguments'] for message in reversed(json['messages'])
                    if message.get('role') == 'assistant'
                    for tool_call in message.get('tool_calls', [])
                    if tool_call['function']['name'] == 'list_tasks'
                ))
                project_id = list_arguments['project_id']
                reply = {'choices': [{'message': {
                    'role': 'assistant',
                    'content': None,
                    'tool_calls': [
                        tool_call('call-task-outline', 'create_task', {
                            'title': '番組テーマと対象を決める', 'project_id': project_id,
                        }),
                        tool_call('call-task-record', 'create_task', {
                            'title': '最初の台本を作る', 'project_id': project_id,
                        }),
                        tool_call('call-task-publish', 'create_task', {
                            'title': '収録して公開する', 'project_id': project_id,
                        }),
                    ],
                }}]}
            else:
                reply = {'choices': [{'message': {
                    'role': 'assistant', 'content': '最初の3つの作業に分けて登録したよ。',
                }}]}
            return SimpleNamespace(is_error=False, json=lambda: reply)

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={
            'content': 'ポッドキャストを始めるプロジェクトを作って、最初の作業もいくつか登録して',
        })
        projects = client.get('/api/projects').json()
        project = next(project for project in projects if project['title'] == '架空ポッドキャスト制作')
        tasks = client.get('/api/tasks', params={'project_id': project['id']}).json()

    assert response.status_code == 200
    assert '最初の3つ' in response.json()['assistant_message']['content']
    assert len(request_payloads) == 4
    assert len(tasks) == 3
    assert all(task['project_id'] == project['id'] for task in tasks)
    assert all(task['due_at'] is None and task['estimated_minutes'] is None for task in tasks)


def test_weekly_goal_review_runs_once_after_seven_days(monkeypatch, tmp_path):
    review_contexts = []

    async def fake_reply(messages, conversation_id=None):
        review_contexts.append([
            message['content'] for message in messages
            if message.get('role') == 'system' and '週次目標レビュー' in message.get('content', '')
        ])
        return '今週の目標を振り返ると、次は歌詞の下書きがよさそう。'

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'generate_llm_reply', fake_reply)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        active_goal = client.post('/api/goals', json={
            'title': 'アルバムを完成する',
            'target_date': '2027-12-31',
        }).json()
        paused_goal = client.post('/api/goals', json={
            'title': '保留中の動画制作',
            'status': 'paused',
        }).json()
        project = client.post('/api/projects', json={
            'goal_id': active_goal['id'],
            'title': 'アルバム制作',
            'next_action': '歌詞の下書きを作る',
        }).json()
        client.post('/api/tasks', json={
            'title': 'Aメロを書く',
            'project_id': project['id'],
            'due_at': '2026-10-25T18:00:00+09:00',
        })
        connection = database_module.open_database()
        connection.execute(
            "UPDATE goals SET created_at = '2000-01-01T00:00:00+00:00' WHERE id IN (?, ?)",
            (active_goal['id'], paused_goal['id']),
        )
        connection.commit()
        connection.close()

        first = client.post('/api/chat', json={'content': 'こんにちは'})
        reviewed_goal = next(
            goal for goal in client.get('/api/goals').json() if goal['id'] == active_goal['id']
        )
        second = client.post('/api/chat', json={'content': 'ありがとう'})

    assert first.status_code == second.status_code == 200
    assert len(review_contexts[0]) == 1
    assert 'アルバムを完成する' in review_contexts[0][0]
    assert '歌詞の下書きを作る' in review_contexts[0][0]
    assert '保留中の動画制作' not in review_contexts[0][0]
    assert reviewed_goal['last_reviewed_at'] is not None
    assert review_contexts[1] == []


def test_initialize_database_migrates_legacy_tasks(monkeypatch, tmp_path):
    database_path = tmp_path / 'legacy.sqlite3'
    connection = sqlite3.connect(database_path)
    connection.execute(
        '''
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            due_at TEXT,
            estimated_minutes INTEGER,
            priority TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT
        )
        '''
    )
    connection.execute(
        '''
        INSERT INTO tasks (id, title, priority, status, created_at, updated_at)
        VALUES ('legacy-task', '既存タスク', 'normal', 'open', '2026-01-01', '2026-01-01')
        '''
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(database_module, 'DB_PATH', database_path)
    database_module.initialize_database()
    with database_module.open_database() as migrated:
        columns = {row['name'] for row in migrated.execute('PRAGMA table_info(tasks)').fetchall()}
        task = migrated.execute('SELECT * FROM tasks WHERE id = ?', ('legacy-task',)).fetchone()

    assert 'project_id' in columns
    assert task['title'] == '既存タスク'
    assert task['project_id'] is None


def test_gemini_creates_goal_and_linked_project_without_approval(monkeypatch, tmp_path):
    request_payloads = []

    def tool_response(call_id, name, arguments):
        return {'choices': [{'message': {
            'role': 'assistant',
            'content': None,
            'tool_calls': [{
                'id': call_id,
                'type': 'function',
                'function': {'name': name, 'arguments': json.dumps(arguments)},
            }],
        }}]}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            request_payloads.append(json)
            if len(request_payloads) == 1:
                response = tool_response('call-create-goal', 'create_goal', {
                    'title': '自作曲を完成させる',
                    'target_date': '2027-12-31',
                })
            elif len(request_payloads) == 2:
                prior = json_module.loads(next(
                    message['content'] for message in reversed(json['messages'])
                    if message.get('role') == 'tool'
                ))
                response = tool_response('call-create-project', 'create_project', {
                    'title': 'アルバム制作',
                    'goal_id': prior['goal']['id'],
                    'next_action': '歌詞の下書きを作る',
                })
            else:
                response = {'choices': [{'message': {
                    'role': 'assistant', 'content': '目標に結びつけてプロジェクトを作成したよ。',
                }}]}
            return SimpleNamespace(is_error=False, json=lambda: response)

    import json as json_module

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={
            'content': '「自作曲を完成させる」を目標にして、「アルバム制作」をプロジェクトとして追加して',
        })
        goals = client.get('/api/goals').json()

    assert response.status_code == 200
    assert len(request_payloads) == 3
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert {'create_goal', 'create_project', 'list_goals', 'list_projects'} <= tool_names
    assert len(goals) == 1
    assert goals[0]['projects'][0]['title'] == 'アルバム制作'
    assert goals[0]['projects'][0]['goal_id'] == goals[0]['id']