import json
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

import app as norn_app
import chat as chat_module
import database as database_module
import events as events_module
import settings as settings_module
import tasks as tasks_module
import tasks as tasks_module
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
    assert '予定を追加しました' in response.json()['assistant_message']['content']
    assert len(events) == 1
    assert events[0]['title'] == 'Gemini連携テスト'
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert tool_names == {
        'list_events', 'create_event', 'update_event', 'delete_event',
        'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task',
        'find_task_slots', 'schedule_task', 'unschedule_task',
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
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '見積30分、期限は指定日の18時のタスクを追加して予定に割り当てて'})
        tasks = client.get('/api/tasks').json()

    assert response.status_code == 200
    assert '最短の空き時間に割り当てました' in response.json()['assistant_message']['content']
    assert len(calls) == 4
    assert len(tasks) == 1
    assert tasks[0]['scheduled_start'].endswith('T09:00:00+09:00')