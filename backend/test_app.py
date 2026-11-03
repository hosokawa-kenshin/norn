import asyncio
import json
import sqlite3
import stat
import struct
import uuid
from contextlib import closing
from datetime import datetime, time, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient
import httpx
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
import reminders as reminders_module
import schedule_actions as actions_module
import routines as routines_module
import llm_limits as llm_limits_module
import voice as voice_module
import tts as tts_module
import wake as wake_module
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(autouse=True)
def isolated_llm_limits(monkeypatch):
    monkeypatch.setattr(llm_limits_module, 'blocked_until', 0.0)


@pytest.fixture(autouse=True)
def isolated_planning_hours(monkeypatch):
    for module in (tasks_module, reminders_module, actions_module):
        monkeypatch.setattr(module, 'PLANNING_DAY_START', '09:00')
        monkeypatch.setattr(module, 'PLANNING_DAY_END', '18:00')
        monkeypatch.setattr(module, 'PLANNING_WORKDAYS', frozenset(range(5)))


@pytest.fixture(autouse=True)
def disable_real_speech_worker(monkeypatch, tmp_path):
    monkeypatch.setenv('NORN_TTS_ENABLED', '0')
    monkeypatch.setattr(wake_module, 'PROFILE_PATH', tmp_path / 'wake-profile.npz')
    monkeypatch.setattr(chat_module, 'CHARACTER_PATH', tmp_path / 'character.json')
    original = chat_module.prepare_automatic_tasks
    monkeypatch.setattr(chat_module, 'prepare_automatic_tasks', no_op_automatic_tasks)
    return original


@pytest.fixture(autouse=True)
def mock_notice_generation(monkeypatch):
    original = reminders_module.generate_notice_text

    def fake_notice(item, boundary, following):
        return f'「{item["title"]}」の' + ('開始時刻です。' if boundary == 'start_at' else '終了時刻です。延長しますか？')

    monkeypatch.setattr(reminders_module, 'generate_notice_text', fake_notice)
    return original


@pytest.fixture(autouse=True)
def mock_schedule_grounding(monkeypatch):
    original = actions_module.verify_schedule_reply

    async def accept_mock_reply(*args, **kwargs):
        return True

    monkeypatch.setattr(chat_module, 'verify_schedule_reply', accept_mock_reply)
    return original


@pytest.mark.parametrize('bad_claim', ['wrong_time', 'no_operation', 'missing_record', 'unsupported', 'wrong_entity', 'quota'])
def test_schedule_reply_checks_claims_against_database(monkeypatch, tmp_path, future_task_start, bad_claim):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'grounding.sqlite3')
    encode_json = json.dumps
    verdict = {'supported': True, 'claims': []}

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            if verdict.get('quota'):
                response = httpx.Response(429, request=httpx.Request('POST', url))
                response.raise_for_status()
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': encode_json(verdict)}}]})

    monkeypatch.setattr(actions_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        allocation = client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': future_task_start.isoformat()}).json()
        results = [{'tool': 'schedule_task', 'ok': True, 'allocation': allocation}]
        verdict['claims'] = [{'kind': 'allocation', 'id': task['id'], 'fields': {'start_at': allocation['start_at']}, 'changed': True}]
        assert asyncio.run(actions_module.verify_schedule_reply('練習枠を保存したよ。', results, True))
        verdict['claims'][0]['fields'] = {'scheduled_start': allocation['start_at']}
        assert asyncio.run(actions_module.verify_schedule_reply('練習枠を保存したよ。', results, True))
        verdict['claims'][0]['fields'] = {'start_at': allocation['start_at']}
        if bad_claim == 'wrong_time':
            verdict['claims'][0]['fields']['start_at'] = (future_task_start + timedelta(hours=1)).isoformat()
        elif bad_claim == 'no_operation':
            results = []
        elif bad_claim == 'missing_record':
            verdict['claims'][0]['id'] = str(uuid.uuid4())
        elif bad_claim == 'wrong_entity':
            results = [{'tool': 'update_task', 'ok': True, 'task': task, 'summary': '名前を変更しました。'}]
        elif bad_claim == 'quota':
            verdict['quota'] = True
        else:
            verdict['supported'] = False
        if bad_claim == 'quota':
            with pytest.raises(actions_module.HTTPException) as error:
                asyncio.run(actions_module.verify_schedule_reply('練習枠を保存したよ。', results, True))
            assert error.value.status_code == 429
        else:
            assert not asyncio.run(actions_module.verify_schedule_reply('練習枠を保存したよ。', results, True))


@pytest.mark.parametrize(('text', 'expected'), [
    ('予定に入れました。', True),
    ('作業枠を移動しておいたよ。', True),
    ('保存に失敗しました。', False),
    ('予定はまだ未確定だよ。', False),
    ('タスクは保存しましたけど、作業枠の登録には失敗しました。', True),
])
def test_schedule_completion_detection_distinguishes_failure(text, expected):
    assert chat_module.has_action_completion(text) == expected


@pytest.mark.parametrize('boundary', ['start_at', 'end_at'])
def test_schedule_notices_use_gemini_text(monkeypatch, tmp_path, future_task_start, mock_notice_generation, boundary):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'gemini-notice.sqlite3')
    monkeypatch.setattr(reminders_module, 'generate_notice_text', mock_notice_generation)
    monkeypatch.setattr(reminders_module, 'LLM_MODEL', 'test-model')
    chat_module.CHARACTER_PATH.write_text(json.dumps({'口調': '落ち着いた架空のテスト口調'}), encoding='utf-8')
    payloads = []
    generated = 'Geminiが生成した開始の声かけ。' if boundary == 'start_at' else 'Geminiが生成した延長の問いかけ。'

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            payloads.append(json)
            with closing(database_module.open_database()) as connection:
                connection.execute('BEGIN IMMEDIATE')
                connection.rollback()
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': generated}}]})

    monkeypatch.setattr(reminders_module.httpx, 'Client', FakeClient)
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        client.post('/api/events', json={'title': '架空の作業', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat(), 'description': '送信しない詳細'})
        client.post('/api/events', json={'title': '次の架空予定', 'start_at': end.isoformat(), 'end_at': (end + timedelta(minutes=30)).isoformat()})
        result = reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start if boundary == 'start_at' else end - timedelta(microseconds=1))
        if boundary == 'end_at':
            result = reminders_module.claim_notices(reminders_module.NoticeRequest(conversation_id=result['conversation_id']), end)
        assert result['messages'][0]['content'] == generated
        stored = client.get(f'/api/conversations/{result["conversation_id"]}/messages').json()
        assert stored[-1]['content'] == generated
        count = len(payloads)
        assert reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start if boundary == 'start_at' else end)['messages'] == []
        assert len(payloads) == count
    facts = json.loads(payloads[0]['messages'][1]['content'])
    assert facts['boundary'] == ('start' if boundary == 'start_at' else 'end')
    assert facts['event']['title'] == '架空の作業'
    assert facts['following_event']['title'] == '次の架空予定'
    assert 'description' not in facts['event']
    assert '落ち着いた架空のテスト口調' in payloads[0]['messages'][0]['content']
    assert 'tools' not in payloads[0]


@pytest.mark.parametrize('failure', ['empty', 'timeout', 'http_error'])
def test_schedule_notice_generation_failure_can_retry(monkeypatch, tmp_path, future_task_start, mock_notice_generation, failure):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'notice-retry.sqlite3')
    monkeypatch.setattr(reminders_module, 'generate_notice_text', mock_notice_generation)
    monkeypatch.setattr(reminders_module, 'LLM_MODEL', 'test-model')
    attempts = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def post(self, url, json, headers):
            attempts.append(json)
            if len(attempts) == 1:
                if failure == 'timeout':
                    raise httpx.ReadTimeout('test timeout')
                if failure == 'http_error':
                    raise httpx.ConnectError('test connection failure')
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': '  '}}]})
            return SimpleNamespace(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': '再生成できた声かけ'}}]})

    monkeypatch.setattr(reminders_module.httpx, 'Client', FakeClient)
    with TestClient(norn_app.app) as client:
        client.post('/api/events', json={'title': '架空の作業', 'start_at': future_task_start.isoformat(), 'end_at': (future_task_start + timedelta(minutes=30)).isoformat()})
        with pytest.raises(reminders_module.HTTPException) as error:
            reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start)
        assert error.value.status_code == (504 if failure == 'timeout' else 502)
        assert client.get('/api/conversations').json() == []
        with closing(database_module.open_database()) as connection:
            assert connection.execute('SELECT COUNT(*) FROM schedule_notices').fetchone()[0] == 0
        result = reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start)
        assert result['messages'][0]['content'] == '再生成できた声かけ'


def test_schedule_notices_boundaries_deduplicated(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'notices.sqlite3')
    with TestClient(norn_app.app) as client:
        event = client.post('/api/events', json={
            'title': '練習', 'start_at': future_task_start.isoformat(),
            'end_at': (future_task_start + timedelta(minutes=30)).isoformat(),
        }).json()
        first = reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start)
        assert len(first['messages']) == 1 and '開始時刻' in first['messages'][0]['content']
        assert reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start)['messages'] == []
        ending = reminders_module.claim_notices(reminders_module.NoticeRequest(conversation_id=first['conversation_id']), datetime.fromisoformat(event['end_at']))
        assert '延長しますか' in ending['messages'][0]['content']
        assert len(client.get(f'/api/conversations/{first["conversation_id"]}/messages').json()) == 2


def test_extend_schedule_moves_task_and_rejects_stale_retry(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'extension.sqlite3')
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '練習', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat()}).json()
        task = client.post('/api/tasks', json={'title': '片付け', 'estimated_minutes': 30}).json()
        assert client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': end.isoformat()}).status_code == 200
        arguments = {'kind': 'event', 'item_id': event['id'], 'expected_end': event['end_at'], 'minutes': 30}
        result = reminders_module.execute_reminder_tool(arguments)
        assert result['ok'] and result['moved_tasks'][0]['start_at'] == (end + timedelta(minutes=30)).isoformat()
        assert not reminders_module.execute_reminder_tool(arguments)['ok']


def test_extend_schedule_rolls_back_without_slot(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'rollback.sqlite3')
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '練習', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat()}).json()
        task = client.post('/api/tasks', json={'title': '期限付き', 'estimated_minutes': 30, 'due_at': (end + timedelta(minutes=30)).isoformat()}).json()
        assert client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': end.isoformat()}).status_code == 200
        result = reminders_module.execute_reminder_tool({'kind': 'event', 'item_id': event['id'], 'expected_end': end.isoformat(), 'minutes': 30})
        assert not result['ok']
        assert client.get('/api/events').json()[0]['end_at'] == event['end_at']
        assert client.get('/api/tasks').json()[0]['scheduled_start'] == end.isoformat()


def test_extend_schedule_preserves_normal_event(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'fixed.sqlite3')
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '練習', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat()}).json()
        client.post('/api/events', json={'title': '面談', 'start_at': end.isoformat(), 'end_at': (end + timedelta(hours=1)).isoformat()})
        result = reminders_module.execute_reminder_tool({'kind': 'event', 'item_id': event['id'], 'expected_end': end.isoformat(), 'minutes': 30})
        assert not result['ok'] and '面談' in result['error'] and '確認' in result['error']
        assert client.get('/api/events').json()[0]['end_at'] == end.isoformat()


def test_extend_schedule_recurring_only_changes_one_occurrence(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'occurrence.sqlite3')
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '定例', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat(), 'recurrence': ['RRULE:FREQ=DAILY']}).json()
        arguments = {'kind': 'event', 'item_id': event['id'], 'expected_end': (end + timedelta(days=1)).isoformat(), 'minutes': 15}
        assert reminders_module.execute_reminder_tool(arguments)['ok']
        assert not reminders_module.execute_reminder_tool(arguments)['ok']
        occurrences = events_module.query_events(future_task_start, future_task_start + timedelta(days=3))
        assert len(occurrences) == 3
        assert occurrences[1]['end_at'] == (end + timedelta(days=1, minutes=15)).isoformat()
        assert occurrences[2]['end_at'] == (end + timedelta(days=2)).isoformat()


def test_extend_schedule_recurring_cannot_overlap_next_occurrence(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'recurrence-conflict.sqlite3')
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '定例', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat(), 'recurrence': ['RRULE:FREQ=DAILY']}).json()
        result = reminders_module.execute_reminder_tool({'kind': 'event', 'item_id': event['id'], 'expected_end': end.isoformat(), 'minutes': 1440})
        assert not result['ok'] and '定例' in result['error']
        assert len(client.get('/api/events').json()) == 1


def test_extend_schedule_task_keeps_estimate_and_moves_next_task(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'task-extension.sqlite3')
    with TestClient(norn_app.app) as client:
        first = client.post('/api/tasks', json={'title': '作業', 'estimated_minutes': 30}).json()
        second = client.post('/api/tasks', json={'title': '次の作業', 'estimated_minutes': 30}).json()
        end = future_task_start + timedelta(minutes=30)
        first_allocation = client.put(f'/api/tasks/{first["id"]}/schedule', json={'start_at': future_task_start.isoformat()}).json()
        client.put(f'/api/tasks/{second["id"]}/schedule', json={'start_at': end.isoformat()})
        result = reminders_module.execute_reminder_tool({'kind': 'task', 'item_id': first_allocation['id'], 'expected_end': end.isoformat(), 'minutes': 15})
        assert result['ok'] and result['end_at'] == (end + timedelta(minutes=15)).isoformat()
        tasks = {task['id']: task for task in client.get('/api/tasks').json()}
        assert tasks[first['id']]['estimated_minutes'] == 30
        assert tasks[second['id']]['scheduled_start'] == result['end_at']


def test_legacy_task_schedule_replaces_split_allocations(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'replace-allocations.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '分割練習', 'estimated_minutes': 60, 'splittable': True,
        }).json()
        client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': future_task_start.isoformat(),
            'end_at': (future_task_start + timedelta(minutes=30)).isoformat(),
        })
        client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': (future_task_start + timedelta(hours=1)).isoformat(),
            'end_at': (future_task_start + timedelta(hours=1, minutes=30)).isoformat(),
        })

        replacement = client.put(
            f"/api/tasks/{task['id']}/schedule",
            json={'start_at': (future_task_start + timedelta(hours=2)).isoformat()},
        )
        allocations = client.get(f"/api/tasks/{task['id']}/allocations").json()

    assert replacement.status_code == 200
    assert len(allocations) == 1
    assert allocations[0]['start_at'] == (future_task_start + timedelta(hours=2)).isoformat()


def test_task_extension_targets_and_moves_the_notified_allocation(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'split-task-extension.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '分割練習', 'estimated_minutes': 60, 'splittable': True,
        }).json()
        first_end = future_task_start + timedelta(minutes=30)
        first = client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': future_task_start.isoformat(), 'end_at': first_end.isoformat(),
        }).json()
        second = client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': first_end.isoformat(),
            'end_at': (first_end + timedelta(minutes=30)).isoformat(),
        }).json()

        schedule_items = reminders_module.schedule_items(
            future_task_start - timedelta(minutes=1),
            first_end + timedelta(hours=1),
        )
        result = reminders_module.execute_reminder_tool({
            'kind': 'task', 'item_id': first['id'],
            'expected_end': first_end.isoformat(), 'minutes': 15,
        })
        allocations = {item['id']: item for item in client.get(f"/api/tasks/{task['id']}/allocations").json()}

    assert {item['item_id'] for item in schedule_items if item['kind'] == 'task'} == {first['id'], second['id']}
    assert result['ok']
    assert allocations[first['id']]['end_at'] == (first_end + timedelta(minutes=15)).isoformat()
    assert allocations[second['id']]['start_at'] == allocations[first['id']]['end_at']


def test_schedule_notices_ignore_all_day_and_expired_boundaries(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'notice-window.sqlite3')
    with TestClient(norn_app.app) as client:
        day = future_task_start.replace(hour=0)
        client.post('/api/events', json={'title': '終日', 'start_at': day.isoformat(), 'end_at': (day + timedelta(days=1)).isoformat(), 'is_all_day': True})
        assert reminders_module.claim_notices(reminders_module.NoticeRequest(), day)['messages'] == []
        client.post('/api/events', json={'title': '翌日の作業', 'start_at': (future_task_start + timedelta(days=1)).isoformat(), 'end_at': (future_task_start + timedelta(days=1, minutes=30)).isoformat()})
        assert reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start + timedelta(days=1, minutes=36))['messages'] == []


def test_schedule_notices_recurring_and_restart(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'repeat.sqlite3')
    with TestClient(norn_app.app) as client:
        client.post('/api/events', json={'title': '定例', 'start_at': future_task_start.isoformat(),
                                       'end_at': (future_task_start + timedelta(minutes=30)).isoformat(), 'recurrence': ['RRULE:FREQ=DAILY']})
        first = reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start + timedelta(days=1))
        assert len(first['messages']) == 1
    with TestClient(norn_app.app):
        assert reminders_module.claim_notices(reminders_module.NoticeRequest(), future_task_start + timedelta(days=1))['messages'] == []


def test_extend_schedule_chat_tool(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'chat-extension.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    monkeypatch.setattr(reminders_module, 'generate_notice_text', lambda *args: 'もう少し続ける？何分にする？')

    async def unexpected_automatic_tasks(*args):
        raise AssertionError('通知への返答を通常のタスク見積変更として判定してはいけません。')

    monkeypatch.setattr(chat_module, 'prepare_automatic_tasks', unexpected_automatic_tasks)
    payloads = []
    tool_arguments = {}
    encode_json = json.dumps

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            message = {'content': '30分延長しました。'} if len(payloads) > 1 else {
                'content': None, 'tool_calls': [{'id': 'extension-1', 'type': 'function', 'function': {
                    'name': 'extend_schedule', 'arguments': encode_json(tool_arguments),
                }}],
            }
            return SimpleNamespace(is_error=False, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        end = future_task_start + timedelta(minutes=30)
        event = client.post('/api/events', json={'title': '練習', 'start_at': future_task_start.isoformat(), 'end_at': end.isoformat()}).json()
        notice = reminders_module.claim_notices(reminders_module.NoticeRequest(), end)
        tool_arguments.update(kind='event', item_id=event['id'], expected_end=end.isoformat(), minutes=30)
        response = client.post('/api/chat', json={'conversation_id': notice['conversation_id'], 'content': '30分延長して'})
        assert response.status_code == 200
        assert client.get('/api/events').json()[0]['end_at'] == (end + timedelta(minutes=30)).isoformat()
        assert event['id'] in payloads[0]['messages'][0]['content']


def configure_automatic_task_test(monkeypatch, tmp_path, original, actions):
    payloads = []
    encode_json = json.dumps

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            if 'response_format' in json:
                content = actions if isinstance(actions, str) else encode_json({'actions': actions})
            else:
                content = '確認しました。'
            return SimpleNamespace(
                is_error=False, raise_for_status=lambda: None,
                json=lambda: {'choices': [{'message': {'content': content}}]},
            )

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'prepare_automatic_tasks', original)
    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    return payloads


def test_automatic_task_saves_intent_without_scheduling(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = [{
        'title': '楽器を練習する', 'evidence': '今日は楽器を練習しなきゃ',
        'due_date': '2026-11-02', 'due_evidence': '今日',
    }]
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '今日は楽器を練習しなきゃ'})
        tasks = client.get('/api/tasks').json()
        assert client.get('/api/events').json() == []
    assert response.status_code == 200
    assert len(tasks) == 1
    assert tasks[0]['title'] == '楽器を練習する'
    assert tasks[0]['due_at'] == '2026-11-02T23:59:59+09:00'
    assert tasks[0]['estimated_minutes'] is None
    assert tasks[0]['scheduled_start'] is None
    assert len(payloads) == 1
    assert '追加しました' in response.json()['assistant_message']['content']
    assert '何分くらい' in response.json()['assistant_message']['content']


@pytest.fixture
def future_task_start():
    start = datetime.now(ZoneInfo('Asia/Tokyo')) + timedelta(days=1)
    while start.weekday() not in tasks_module.PLANNING_WORKDAYS:
        start += timedelta(days=1)
    return start.replace(hour=14, minute=0, second=0, microsecond=0)


def test_chat_rate_limit_is_429_and_blocks_repeated_calls(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'rate-limit.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            return httpx.Response(429, headers={'Retry-After': '120'}, request=httpx.Request('POST', url))

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        first = client.post('/api/chat', json={'content': '毎日のルーティンを組んで'})
        assert first.status_code == 429 and first.headers['retry-after'] == '120'
        assert 'Geminiの利用制限' in first.json()['detail']
        second = client.post('/api/chat', json={'content': 'お願いします'})
        assert second.status_code == 429 and len(calls) == 1
        assert client.get('/api/tasks').json() == []


def test_action_reply_rate_limit_stays_429(monkeypatch):
    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            return httpx.Response(429, request=httpx.Request('POST', url))

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    monkeypatch.setattr(chat_module, "schedule_context", lambda *args: "")
    with pytest.raises(chat_module.HTTPException) as error:
        asyncio.run(chat_module.generate_action_reply('test', [], []))
    assert error.value.status_code == 429


def test_llm_cooldown_stops_other_generation_paths(monkeypatch, disable_real_speech_worker, mock_notice_generation, mock_schedule_grounding):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append(True)
        raise AssertionError('クールダウン中はGeminiへ送信してはいけません。')

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', forbidden)
    monkeypatch.setattr(reminders_module.httpx, 'Client', forbidden)
    with pytest.raises(llm_limits_module.HTTPException):
        llm_limits_module.check_llm_response(httpx.Response(429, request=httpx.Request('POST', 'https://example.invalid')))
    with pytest.raises(llm_limits_module.HTTPException):
        asyncio.run(disable_real_speech_worker([{'role': 'user', 'content': '練習したい'}]))
    with pytest.raises(llm_limits_module.HTTPException):
        asyncio.run(mock_schedule_grounding('返答', []))
    with pytest.raises(llm_limits_module.HTTPException):
        mock_notice_generation({}, 'start_at', None)
    asyncio.run(chat_module.extract_due_memories())
    assert calls == []


@pytest.mark.parametrize(('retry_after', 'expected'), [('120', 120), ('', 60), ('invalid', 60), ('0', 1)])
def test_llm_cooldown_honors_retry_after_and_expires(monkeypatch, retry_after, expected):
    clock = [1000.0]
    monkeypatch.setattr(llm_limits_module.time, 'monotonic', lambda: clock[0])
    with pytest.raises(llm_limits_module.HTTPException) as error:
        llm_limits_module.check_llm_response(httpx.Response(429, headers={'Retry-After': retry_after}))
    assert error.value.headers['Retry-After'] == str(expected)
    with pytest.raises(llm_limits_module.HTTPException):
        llm_limits_module.check_llm_available()
    clock[0] += expected
    llm_limits_module.check_llm_available()


def test_routine_applies_each_day_immediately_and_includes_last_day(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine.sqlite3')
    monkeypatch.setattr(tasks_module, 'PLANNING_WORKDAYS', frozenset(range(7)))
    with TestClient(norn_app.app) as client:
        sources = [client.post('/api/tasks', json={'title': title, 'estimated_minutes': 60}).json()
                   for title in ('笛の練習', '制作練習')]
        first = future_task_start.date()
        last = first + timedelta(days=4)
        arguments = {
            'start_date': first.isoformat(), 'through_date': last.isoformat(),
            'items': [{'task_id': source['id']} for source in sources],
        }
        applied = routines_module.schedule_routine(arguments)
        assert applied['ok'] and applied['state'] == 'applied'
        assert len(applied['occurrences']) == 10 and applied['missing'] == []
        assert {item['date'] for item in applied['occurrences']} == {(first + timedelta(days=index)).isoformat() for index in range(5)}
        saved = client.get('/api/tasks').json()
        assert len(saved) == 10
        assert all(task['scheduled_start'] and task['estimated_minutes'] == 60 for task in saved)
        repeated = routines_module.schedule_routine(arguments)
        assert repeated['state'] == 'unchanged' and len(repeated['occurrences']) == 10
        assert len(client.get('/api/tasks').json()) == 10
        client.patch(f'/api/tasks/{sources[0]["id"]}', json={'status': 'done'})
        after_completion = routines_module.schedule_routine(arguments)
        assert after_completion['state'] == 'unchanged'
        assert sum(item['state'] == 'done' for item in after_completion['occurrences']) == 1
        assert len(client.get('/api/tasks').json()) == 10
        assert sum(bool(task['scheduled_start']) for task in client.get('/api/tasks').json()) == 9


def test_routine_applies_available_days_and_reports_missing_days(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-missing.sqlite3')
    monkeypatch.setattr(tasks_module, 'PLANNING_WORKDAYS', frozenset(range(7)))
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        day = future_task_start.replace(hour=0)
        client.post('/api/events', json={'title': '終日外出', 'start_at': day.isoformat(), 'end_at': (day + timedelta(days=1)).isoformat(), 'is_all_day': True})
        result = routines_module.schedule_routine({
            'start_date': day.date().isoformat(), 'through_date': (day + timedelta(days=1)).date().isoformat(),
            'items': [{'task_id': source['id']}],
        })
        assert result['ok'] and result['state'] == 'applied'
        assert len(result['missing']) == 1 and result['missing'][0]['date'] == day.date().isoformat()
        assert len(result['occurrences']) == 1
        tasks = client.get('/api/tasks').json()
        assert len(tasks) == 1
        assert datetime.fromisoformat(tasks[0]['scheduled_start']).date() == (day + timedelta(days=1)).date()


def test_routine_chat_keeps_unknown_duration_until_answer_then_applies(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-chat.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    calls = []
    request = {}
    encode_json = json.dumps

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            if 'tools' in json:
                message = {'content': None, 'tool_calls': [{'id': f'routine-{len(calls)}', 'type': 'function', 'function': {'name': 'schedule_routine', 'arguments': encode_json(request)}}]}
            else:
                message = {'content': '時間を教えて。' if len(calls) == 2 else '日ごとの作業枠を保存したよ。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習'}).json()
        request.update(start_date=future_task_start.date().isoformat(), through_date=(future_task_start + timedelta(days=1)).date().isoformat(), items=[{'task_id': source['id']}])
        first = client.post('/api/chat', json={'content': 'この練習を毎日のルーティンにして'}).json()
        assert client.get('/api/tasks').json()[0]['estimated_minutes'] is None
        assert len(client.get('/api/tasks').json()) == 1
        request['items'] = [{'task_id': source['id'], 'estimated_minutes': 30, 'estimate_evidence': '30分'}]
        second = client.post('/api/chat', json={'conversation_id': first['conversation_id'], 'content': '30分で'})
        assert second.status_code == 200
        saved = client.get('/api/tasks').json()
        assert len(saved) == 2 and all(task['estimated_minutes'] == 30 and task['scheduled_start'] for task in saved)
        assert all(task['routine_source_task_id'] == source['id'] for task in saved)
    assert 'schedule_routine' in {tool['function']['name'] for tool in calls[0]['tools']}
    assert 'tools' not in calls[-1]


def test_routine_respects_recurring_events(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-recurrence.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        start = future_task_start.replace(hour=9)
        client.post('/api/events', json={'title': '毎日の用事', 'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat(), 'recurrence': ['RRULE:FREQ=DAILY']})
        result = routines_module.schedule_routine({'start_date': start.date().isoformat(), 'through_date': (start + timedelta(days=1)).date().isoformat(), 'items': [{'task_id': source['id']}]})
        assert result['state'] == 'applied'
        assert len(result['occurrences']) == 2
        assert all(datetime.fromisoformat(item['start_at']).hour == 10 for item in result['occurrences'])


@pytest.mark.parametrize('bad_range', ['reverse', 'too_long', 'past'])
def test_routine_rejects_invalid_range_without_changes(monkeypatch, tmp_path, future_task_start, bad_range):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-range.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 30}).json()
        start = future_task_start.date()
        end = start + timedelta(days=31) if bad_range == 'too_long' else start - timedelta(days=1) if bad_range == 'reverse' else start
        if bad_range == 'past':
            start = end = datetime.now(tasks_module.LOCAL_TIMEZONE).date() - timedelta(days=1)
        result = routines_module.schedule_routine({'start_date': start.isoformat(), 'through_date': end.isoformat(), 'items': [{'task_id': source['id']}]})
        assert not result['ok']
        assert len(client.get('/api/tasks').json()) == 1 and client.get('/api/tasks').json()[0]['scheduled_start'] is None


def test_routine_existing_source_allocation_stays_on_its_day(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-existing.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 30}).json()
        allocated = client.put(f'/api/tasks/{source["id"]}/schedule', json={'start_at': future_task_start.isoformat()}).json()
        result = routines_module.schedule_routine({'start_date': future_task_start.date().isoformat(), 'through_date': (future_task_start + timedelta(days=1)).date().isoformat(), 'items': [{'task_id': source['id']}]})
        assert result['occurrences'][0]['state'] == 'existing'
        assert result['occurrences'][0]['start_at'] == allocated['start_at']
        assert result['occurrences'][1]['state'] == 'scheduled'
        assert next(task for task in client.get('/api/tasks').json() if task['id'] == source['id'])['scheduled_start'] == allocated['start_at']


def test_routine_reports_nonworking_day_without_moving_it(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-weekend.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 30}).json()
        weekend = future_task_start.date()
        while weekend.weekday() < 5:
            weekend += timedelta(days=1)
        result = routines_module.schedule_routine({'start_date': weekend.isoformat(), 'through_date': weekend.isoformat(), 'items': [{'task_id': source['id']}]})
        assert result['state'] == 'needs_input' and result['occurrences'] == []
        assert result['missing'][0]['date'] == weekend.isoformat()
        assert '作業曜日' in result['missing'][0]['reason']
        assert client.get('/api/tasks').json()[0]['scheduled_start'] is None


def test_routine_does_not_invent_unknown_duration(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-unknown.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習'}).json()
        result = routines_module.schedule_routine({
            'start_date': future_task_start.date().isoformat(), 'through_date': future_task_start.date().isoformat(),
            'items': [{'task_id': source['id'], 'estimated_minutes': 120}],
        }, 'はい')
        assert not result['ok'] and '根拠' in result['error']
        assert client.get('/api/tasks').json()[0]['estimated_minutes'] is None
        assert client.get('/api/tasks').json()[0]['scheduled_start'] is None


def test_routine_inherits_same_one_hour_when_user_only_answers_period(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-period-answer.sqlite3')
    with TestClient(norn_app.app) as client:
        sources = [client.post('/api/tasks', json={'title': title, 'estimated_minutes': 60}).json()
                   for title in ('笛の練習', '制作練習', 'ギター練習')]
        start = future_task_start.date()
        result = routines_module.schedule_routine({
            'start_date': start.isoformat(), 'through_date': (start + timedelta(days=6)).isoformat(),
            'items': [{'task_id': source['id'], 'estimated_minutes': 60} for source in sources],
        }, 'ここから1週間ぐらいで')
        assert result['ok'] and result['state'] == 'applied'
        assert len(result['occurrences']) == 15 and len(result['missing']) == 6
        assert all('作業曜日' in item['reason'] for item in result['missing'])
        saved = client.get('/api/tasks').json()
        assert len(saved) == 15
        assert all(task['estimated_minutes'] == 60 and task['scheduled_start'] for task in saved)


def test_failed_routine_reply_cannot_promise_later_approval(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'failed-routine-reply.sqlite3')
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': None}}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app):
        result = [{'tool': 'schedule_routine', 'ok': False, 'error': '所要時間が不明です。'}]
        with pytest.raises(chat_module.HTTPException) as error:
            asyncio.run(chat_module.generate_action_reply('test', [], result))
        assert error.value.status_code == 502
        assert '変更は反映していません' in error.value.detail
        assert '所要時間が不明' in error.value.detail
    assert len(calls) == 1


def test_routine_period_only_reply_uses_routine_tool_not_automatic_detection(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-period-chat.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    calls = []
    request = {}
    encode_json = json.dumps

    async def forbidden_detection(*args):
        raise AssertionError('期間への返答はルーティンの会話を継続してください。')

    monkeypatch.setattr(chat_module, 'prepare_automatic_tasks', forbidden_detection)

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            latest = next(item['content'] for item in reversed(json['messages']) if item['role'] == 'user')
            if 'tools' in json and latest == 'ここから1週間ぐらいで':
                message = {'content': None, 'tool_calls': [{'id': 'week-routine', 'type': 'function', 'function': {'name': 'schedule_routine', 'arguments': encode_json(request)}}]}
            else:
                message = {'content': '毎日のルーティンは何日間にする？期間を教えて。' if len(calls) == 1 else '平日5日分の作業枠を保存したよ。土日分はまだ入っていません。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        sources = [client.post('/api/tasks', json={'title': title, 'estimated_minutes': 60}).json() for title in ('笛', 'DTM', 'ギター')]
        request.update(start_date=future_task_start.date().isoformat(), through_date=(future_task_start + timedelta(days=6)).date().isoformat(), items=[{'task_id': source['id'], 'estimated_minutes': 60} for source in sources])
        initial = client.post('/api/chat', json={'content': '全部1時間ぐらいで毎日やりたい'}).json()
        response = client.post('/api/chat', json={'conversation_id': initial['conversation_id'], 'content': 'ここから1週間ぐらいで'})
        assert response.status_code == 200
        saved = client.get('/api/tasks').json()
        assert len(saved) == 15 and all(task['estimated_minutes'] == 60 and task['scheduled_start'] for task in saved)
    assert len(calls) == 3


def test_failed_routine_changes_nothing(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'failed-routine.sqlite3')
    with TestClient(norn_app.app) as client:
        source = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        arguments = {'start_date': future_task_start.date().isoformat(), 'through_date': future_task_start.date().isoformat(), 'items': [{'task_id': source['id'], 'estimated_minutes': 120}]}
        result = routines_module.schedule_routine(arguments, 'ここから1週間ぐらいで')
        assert not result['ok']
        tasks = client.get('/api/tasks').json()
        assert len(tasks) == 1 and tasks[0]['scheduled_start'] is None and tasks[0]['estimated_minutes'] == 60


def test_routine_explicit_one_hour_overrides_old_duration_immediately(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-new-duration.sqlite3')
    with TestClient(norn_app.app) as client:
        sources = [client.post('/api/tasks', json={'title': title, 'estimated_minutes': minutes}).json()
                   for title, minutes in [('笛', 120), ('DTM', 120), ('ギター', 60)]]
        for index, source in enumerate(sources):
            start = future_task_start.replace(hour=10 + index * 2)
            assert client.put(f'/api/tasks/{source["id"]}/schedule', json={'start_at': start.isoformat()}).status_code == 200
        result = routines_module.schedule_routine({
            'start_date': future_task_start.date().isoformat(), 'through_date': (future_task_start + timedelta(days=1)).date().isoformat(),
            'items': [{'task_id': source['id'], 'estimated_minutes': 60, 'estimate_evidence': '全部1時間'} for source in sources],
        }, '全部1時間で毎日やりたい')
        assert result['ok'] and result['state'] == 'applied' and result['missing'] == []
        saved = client.get('/api/tasks').json()
        assert len(saved) == 6 and all(task['estimated_minutes'] == 60 for task in saved)
        assert all(datetime.fromisoformat(task['scheduled_end']) - datetime.fromisoformat(task['scheduled_start']) == timedelta(hours=1) for task in saved)


def test_chat_rejects_success_text_without_saved_operation(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'ungrounded.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
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
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None,
                                   json=lambda: {'choices': [{'message': {'content': '17時から19時に予定を入れました。'}}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '練習を17時から19時に移動して'})
        assert response.status_code == 502
        assert client.get('/api/tasks').json() == []
        assert client.get('/api/events').json() == []
    assert payloads[1]['tool_choice'] == 'required'
    assert len(payloads) == 3


def test_chat_invalid_tool_json_does_not_create_records(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'invalid-tool-json.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    calls = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            calls.append(json)
            message = {'content': '日時をもう一度教えて。'} if len(calls) > 1 else {
                'content': None, 'tool_calls': [{'id': 'invalid', 'type': 'function', 'function': {'name': 'create_event', 'arguments': '{invalid'}}],
            }
            return SimpleNamespace(is_error=False, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '予定を追加して'})
        assert response.status_code == 200
        assert client.get('/api/events').json() == []


def test_apply_schedule_applies_atomically_without_approval(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'apply.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': future_task_start.isoformat()})
        result = actions_module.apply_schedule({'actions': [
            {'name': 'schedule_task', 'arguments_json': json.dumps({'task_id': task['id'], 'start_at': (future_task_start + timedelta(hours=1)).isoformat()})},
            {'name': 'create_event', 'arguments_json': json.dumps({'title': '食事', 'start_at': future_task_start.isoformat(), 'end_at': (future_task_start + timedelta(hours=1)).isoformat()})},
        ]})
        assert result['ok'] and result['state'] == 'applied' and len(result['results']) == 2
        assert len(client.get('/api/events').json()) == 1
        assert client.get('/api/tasks').json()[0]['scheduled_start'] == (future_task_start + timedelta(hours=1)).isoformat()


def test_apply_schedule_invalid_work_hours_rolls_back(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'apply-rollback.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '作曲', 'estimated_minutes': 120}).json()
        result = actions_module.apply_schedule({'actions': [
            {'name': 'create_event', 'arguments_json': json.dumps({'title': '食事', 'start_at': future_task_start.isoformat(), 'end_at': (future_task_start + timedelta(hours=1)).isoformat()})},
            {'name': 'schedule_task', 'arguments_json': json.dumps({'task_id': task['id'], 'start_at': future_task_start.replace(hour=19).isoformat()})},
        ]})
        assert not result['ok'] and '作業時間' in result['error']
        assert client.get('/api/events').json() == []
        assert client.get('/api/tasks').json()[0]['scheduled_start'] is None


def test_init_database_drops_legacy_schedule_proposals_table(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'legacy.sqlite3')
    with TestClient(norn_app.app):
        pass
    with closing(database_module.open_database()) as connection:
        connection.execute('CREATE TABLE schedule_proposals (id TEXT)')
        connection.commit()
    with TestClient(norn_app.app):
        pass
    with closing(database_module.open_database()) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='schedule_proposals'").fetchone() is None


def test_chat_applies_schedule_in_one_turn_and_later_reply_changes_nothing(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'apply-chat.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    payloads = []
    encode_json = json.dumps
    actions = [{'name': 'create_event', 'arguments_json': encode_json({'title': '食事', 'start_at': future_task_start.isoformat(), 'end_at': (future_task_start + timedelta(hours=1)).isoformat()})}]

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            latest = next(item['content'] for item in reversed(json['messages']) if item['role'] == 'user')
            if 'tools' in json and latest == '食事の予定を入れて':
                message = {'content': None, 'tool_calls': [{'id': 'apply', 'type': 'function', 'function': {'name': 'apply_schedule', 'arguments': encode_json({'actions': actions})}}]}
            elif 'tools' in json:
                message = {'content': '特に変更はないよ。'}
            else:
                message = {'content': '食事の予定を保存したよ。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        initial = client.post('/api/chat', json={'content': '食事の予定を入れて'})
        assert initial.status_code == 200
        assert len(client.get('/api/events').json()) == 1
        assert client.get('/api/events').json()[0]['title'] == '食事'
        later = client.post('/api/chat', json={'conversation_id': initial.json()['conversation_id'], 'content': 'お願いします'})
        assert later.status_code == 200 and len(client.get('/api/events').json()) == 1


def test_apply_schedule_can_create_task_and_allocate_reference(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'apply-reference.sqlite3')
    with TestClient(norn_app.app) as client:
        result = actions_module.apply_schedule({'actions': [
            {'name': 'create_task', 'arguments_json': json.dumps({'title': '練習', 'estimated_minutes': 30})},
            {'name': 'schedule_task', 'arguments_json': json.dumps({'task_id': '@0', 'start_at': future_task_start.isoformat()})},
        ]})
        assert result['ok'] and result['state'] == 'applied'
        saved = client.get('/api/tasks').json()
        assert len(saved) == 1 and saved[0]['scheduled_start'] == future_task_start.isoformat()


def test_automatic_task_preferred_date_is_not_deadline(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = [{'title': '練習', 'evidence': '練習を30分したい', 'estimated_minutes': 30, 'estimate_evidence': '30分',
                'preferred_date': future_task_start.date().isoformat(), 'preferred_date_evidence': '明日'}]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '明日、練習を30分したい'})
        assert response.status_code == 200
        saved = client.get('/api/tasks').json()[0]
        assert saved['due_at'] is None
        assert datetime.fromisoformat(saved['scheduled_start']).date() == future_task_start.date()


def test_automatic_task_does_not_apply_a_requested_proposal(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = []
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 60}).json()
        client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': future_task_start.isoformat()})
        actions[:] = [{'title': '練習', 'task_id': task['id'], 'evidence': 'ずらす',
                       'start_at': (future_task_start + timedelta(hours=1)).isoformat(), 'start_evidence': 'その後'}]
        result = asyncio.run(disable_real_speech_worker([{'role': 'user', 'content': '食事のその後にずらす案を作って'}]))
        assert result == [] and payloads == []
        assert client.get('/api/tasks').json()[0]['scheduled_start'] == future_task_start.isoformat()


def test_automatic_task_does_not_skip_other_event_operation(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = []
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    encode_json = json.dumps

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            if 'response_format' in json:
                message = {'content': encode_json({'actions': actions})}
            elif 'tools' in json and not any(item['role'] == 'tool' for item in json['messages']):
                message = {'content': None, 'tool_calls': [{'id': 'meal', 'type': 'function', 'function': {'name': 'create_event', 'arguments': encode_json({'title': '食事', 'start_at': future_task_start.replace(hour=12).isoformat(), 'end_at': future_task_start.replace(hour=13).isoformat()})}}]}
            else:
                message = {'content': '練習名と食事の予定を確認したよ。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '元の練習名', 'estimated_minutes': 30}).json()
        actions[:] = [{'title': '正しい練習名', 'task_id': task['id'], 'evidence': '正しい練習名', 'continue_dialogue': True}]
        response = client.post('/api/chat', json={'content': '正しい練習名に直して、明日12時から13時の食事も追加して'})
        assert response.status_code == 200
        assert client.get('/api/tasks').json()[0]['title'] == '正しい練習名'
        assert client.get('/api/events').json()[0]['title'] == '食事'


def test_automatic_task_rename_keeps_latest_allocation(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = []
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '誤認識された練習名', 'estimated_minutes': 60}).json()
        client.put(f'/api/tasks/{task["id"]}/schedule', json={'start_at': future_task_start.isoformat()})
        actions[:] = [{'title': '正しい楽器の練習', 'task_id': task['id'], 'evidence': '正しい楽器の練習'}]
        response = client.post('/api/chat', json={'content': '名前は正しい楽器の練習のことだよ'})
        assert response.status_code == 200
        saved = client.get('/api/tasks').json()
        assert len(saved) == 1 and saved[0]['id'] == task['id']
        assert saved[0]['scheduled_start'] == future_task_start.isoformat()
        assert saved[0]['estimated_minutes'] == 60


@pytest.mark.parametrize('failure', ['empty', 'timeout', 'duplicate', 'summary_failure'])
def test_chat_generates_final_action_reply_without_tools(monkeypatch, tmp_path, failure):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'final-reply.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    payloads = []
    encode_json = json.dumps

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            payloads.append(json)
            if 'tools' not in json:
                message = {'content': '' if failure == 'summary_failure' else '作業を追加したよ。何分くらいにする？'}
            elif len(payloads) == 1 or failure == 'duplicate':
                message = {'content': None, 'tool_calls': [{'id': f'call-{len(payloads)}', 'type': 'function', 'function': {
                    'name': 'create_task', 'arguments': encode_json({'title': '架空の作業'}),
                }}]}
            elif failure == 'timeout':
                raise httpx.ReadTimeout('test timeout')
            else:
                message = {'content': ''}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '架空の作業を追加して'})
        assert response.status_code == (502 if failure == 'summary_failure' else 200)
        if failure == 'summary_failure':
            assert '操作を再送せず' in response.json()['detail']
        else:
            assert response.json()['assistant_message']['content'] == '作業を追加したよ。何分くらいにする？'
        assert len(client.get('/api/tasks').json()) == 1
    assert len(payloads) == 3 and 'tools' not in payloads[-1]
    assert 'サーバー実行結果' in payloads[-1]['messages'][0]['content']


@pytest.mark.parametrize('naive_start', [False, True])
def test_automatic_task_preserves_time_before_task_name_and_explains_unaligned_start(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start, naive_start):
    preferred = future_task_start.replace(minute=29)
    actions = [{'title': '架空の楽器練習', 'evidence': '楽器練習を追加して',
                'start_at': preferred.isoformat(), 'start_evidence': '14時29分に'}]
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        conversation_id = str(uuid.uuid4())
        with closing(database_module.open_database()) as connection:
            timestamp = database_module.now_iso()
            connection.execute('INSERT INTO conversations VALUES (?, ?, ?, ?)', (conversation_id, '架空の日時指定', timestamp, timestamp))
            connection.executemany('INSERT INTO messages (conversation_id, role, content, created_at) VALUES (?, ?, ?, ?)', [
                (conversation_id, 'user', '14時29分に', timestamp),
                (conversation_id, 'assistant', 'その時間に何をする？', timestamp),
            ])
            connection.commit()
        initial = client.post('/api/chat', json={'conversation_id': conversation_id, 'content': '楽器練習を追加して'})
        assert initial.status_code == 200
        task = client.get('/api/tasks').json()[0]
        assert len(payloads) == 1
        assert preferred.isoformat() in initial.json()['assistant_message']['content']
        requested = preferred.replace(minute=31)
        actions[:] = [{'title': task['title'], 'task_id': task['id'], 'evidence': '5分',
                       'estimated_minutes': 5, 'estimate_evidence': '5分',
                   'start_at': (requested.replace(tzinfo=None) if naive_start else requested).isoformat(), 'start_evidence': '14時31分'}]
        response = client.post('/api/chat', json={'conversation_id': conversation_id, 'content': '14時31分に5分くらい時間取って'})
        assert response.status_code == 200
        tasks = client.get('/api/tasks').json()
        assert len(tasks) == 1 and tasks[0]['id'] == task['id'] and tasks[0]['estimated_minutes'] == 5
        assert tasks[0]['scheduled_start'] is None
        result_prompt = payloads[-1]['messages'][0]['content']
        assert '5分刻み' in result_prompt and 'alternatives' in result_prompt
        assert preferred.replace(minute=45).isoformat() in result_prompt
        assert 'tools' not in payloads[-1]
        actions[:] = [{'title': task['title'], 'task_id': task['id'], 'evidence': '14時45分',
                   'start_at': preferred.replace(minute=45).isoformat(), 'start_evidence': '14時45分'}]
        confirmed = client.post('/api/chat', json={'conversation_id': conversation_id, 'content': '14時45分でお願い'})
        assert confirmed.status_code == 200
        tasks = client.get('/api/tasks').json()
        assert len(tasks) == 1 and tasks[0]['estimated_minutes'] == 5
        assert tasks[0]['scheduled_start'] == preferred.replace(minute=45).isoformat()
        assert tasks[0]['scheduled_end'] == preferred.replace(minute=50).isoformat()
        assert tasks[0]['scheduled_start'] in confirmed.json()['assistant_message']['content']


def test_automatic_task_reply_assigns_preferred_time(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = [{'title': '楽器を練習する', 'evidence': '練習しなきゃ'}]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        initial = client.post('/api/chat', json={'content': '楽器を練習しなきゃ'}).json()
        task = client.get('/api/tasks').json()[0]
        actions[:] = [{
            'task_id': task['id'], 'title': task['title'], 'evidence': '30分',
            'estimated_minutes': 30, 'estimate_evidence': '30分',
            'start_at': future_task_start.isoformat(), 'start_evidence': '14時',
        }]
        response = client.post('/api/chat', json={
            'conversation_id': initial['conversation_id'], 'content': '30分、14時からがいいな',
        })
        tasks = client.get('/api/tasks').json()
        assert client.get('/api/events').json() == []
    assert response.status_code == 200
    assert len(tasks) == 1 and tasks[0]['id'] == task['id']
    assert tasks[0]['estimated_minutes'] == 30
    assert tasks[0]['scheduled_start'] == future_task_start.isoformat()
    assert tasks[0]['scheduled_end'] == (future_task_start + timedelta(minutes=30)).isoformat()


def test_automatic_task_keeps_previously_requested_time(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = [{
        'title': '楽器を練習する', 'evidence': '練習しなきゃ',
        'start_at': future_task_start.isoformat(), 'start_evidence': '14時',
    }]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        initial = client.post('/api/chat', json={'content': '14時から楽器を練習しなきゃ'}).json()
        task = client.get('/api/tasks').json()[0]
        actions[:] = [{**actions[0], 'task_id': task['id'], 'evidence': '30分',
                       'estimated_minutes': 30, 'estimate_evidence': '30分'}]
        assert client.post('/api/chat', json={
            'conversation_id': initial['conversation_id'], 'content': '30分で',
        }).status_code == 200
        updated = client.get('/api/tasks').json()[0]
    assert updated['scheduled_start'] == future_task_start.isoformat()


def test_automatic_task_preferred_conflict_does_not_choose_another_time(monkeypatch, tmp_path, disable_real_speech_worker, future_task_start):
    actions = [{
        'title': '楽器を練習する', 'evidence': '30分練習しなきゃ',
        'estimated_minutes': 30, 'estimate_evidence': '30分',
        'start_at': future_task_start.isoformat(), 'start_evidence': '14時',
    }]
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        client.post('/api/events', json={
            'title': '既存の予定', 'start_at': future_task_start.isoformat(),
            'end_at': (future_task_start + timedelta(hours=1)).isoformat(),
        })
        assert client.post('/api/chat', json={'content': '14時から30分練習しなきゃ'}).status_code == 200
        task = client.get('/api/tasks').json()[0]
    assert task['scheduled_start'] is None
    assert 'unavailable' in payloads[1]['messages'][0]['content']
    assert '重複' in payloads[1]['messages'][0]['content']


def test_automatic_task_assigns_earliest_slot_when_time_unspecified(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = [{'title': '楽器を練習する', 'evidence': '30分練習しなきゃ',
                'estimated_minutes': 30, 'estimate_evidence': '30分'}]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        assert client.post('/api/chat', json={'content': '30分練習しなきゃ'}).status_code == 200
        task = client.get('/api/tasks').json()[0]
    assert task['scheduled_start'] is not None
    assert datetime.fromisoformat(task['scheduled_end']) - datetime.fromisoformat(task['scheduled_start']) == timedelta(minutes=30)


def test_automatic_task_allows_deferring_scheduling(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = [{'title': '楽器を練習する', 'evidence': '30分練習しなきゃ',
                'estimated_minutes': 30, 'estimate_evidence': '30分',
                'defer_scheduling': True, 'defer_evidence': '時間は後で'}]
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/chat', json={'content': '30分練習しなきゃ、時間は後で'})
        assert response.status_code == 200
        task = client.get('/api/tasks').json()[0]
    assert task['scheduled_start'] is None
    assert len(payloads) == 1
    assert '保留' in response.json()['assistant_message']['content']


@pytest.mark.parametrize(('value', 'expected'), [
    ('2026-11-02T14:00:00+09:00', '2026-11-02T14:00:00+09:00'),
    ('2026-11-02T14:00:01+09:00', '2026-11-02T14:15:00+09:00'),
    ('2026-11-02T14:14:59+09:00', '2026-11-02T14:15:00+09:00'),
    ('2026-11-02T23:59:59+09:00', '2026-11-03T00:00:00+09:00'),
])
def test_task_slot_rounding_never_returns_past_time(value, expected):
    assert tasks_module.ceil_to_quarter_hour(datetime.fromisoformat(value)) == datetime.fromisoformat(expected)


def test_task_schedule_rejects_nonzero_seconds(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '練習', 'estimated_minutes': 30}).json()
        response = client.put(f'/api/tasks/{task["id"]}/schedule', json={
            'start_at': (future_task_start + timedelta(seconds=30)).isoformat(),
        })
    assert response.status_code == 409


def test_automatic_task_refines_existing_work(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = []
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '楽器を練習する'}).json()
        actions.append({'task_id': task['id'], 'title': '練習曲Aを楽器で練習する', 'evidence': '練習曲A'})
        response = client.post('/api/chat', json={'content': '新しい曲だね、練習曲Aってやつ'})
        tasks = client.get('/api/tasks').json()
    assert response.status_code == 200
    assert len(tasks) == 1
    assert tasks[0]['id'] == task['id']
    assert tasks[0]['title'] == '練習曲Aを楽器で練習する'


def test_automatic_task_does_not_duplicate_repeated_intent(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = [{'title': '楽器を練習する', 'evidence': '練習しなきゃ'}]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        for _ in range(2):
            assert client.post('/api/chat', json={'content': '楽器を練習しなきゃ'}).status_code == 200
        assert len(client.get('/api/tasks').json()) == 1


@pytest.mark.parametrize('action', [
    {'title': '練習する', 'evidence': '発言にない根拠'},
    {'title': '練習する', 'evidence': '練習しなきゃ', 'task_id': str(uuid.uuid4())},
    {'title': '練習する', 'evidence': '練習しなきゃ', 'estimated_minutes': 0},
])
def test_automatic_task_rejects_invalid_actions(monkeypatch, tmp_path, disable_real_speech_worker, action):
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, [action])
    with TestClient(norn_app.app) as client:
        assert client.post('/api/chat', json={'content': '練習しなきゃ'}).status_code == 200
        assert client.get('/api/tasks').json() == []


def test_automatic_task_ignores_unsupported_time_values(monkeypatch, tmp_path, disable_real_speech_worker):
    actions = [{'title': '楽器を練習する', 'evidence': '練習しなきゃ',
                'estimated_minutes': 30, 'due_date': '2026-11-02'}]
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, actions)
    with TestClient(norn_app.app) as client:
        assert client.post('/api/chat', json={'content': '楽器を練習しなきゃ'}).status_code == 200
        task = client.get('/api/tasks').json()[0]
    assert task['estimated_minutes'] is None
    assert task['due_at'] is None


@pytest.mark.parametrize('content', [
    '今日は練習する必要はない', '練習しなきゃいけないの？', 'もし発表会に出るなら練習しなきゃね',
    '友達が練習しなきゃって言ってた', '今日はもう練習を終えた', 'いつか楽器を吹けたらいいな',
    '練習しなきゃと思ったけど、タスクには登録しないで',
])
def test_automatic_task_empty_decision_preserves_chat(monkeypatch, tmp_path, disable_real_speech_worker, content):
    configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, [])
    with TestClient(norn_app.app) as client:
        assert client.post('/api/chat', json={'content': content}).status_code == 200
        assert client.get('/api/tasks').json() == []


def test_automatic_task_invalid_model_output_preserves_chat(monkeypatch, tmp_path, disable_real_speech_worker):
    payloads = configure_automatic_task_test(monkeypatch, tmp_path, disable_real_speech_worker, 'not JSON')
    with TestClient(norn_app.app) as client:
        assert client.post('/api/chat', json={'content': '練習しなきゃ'}).status_code == 200
        assert client.get('/api/tasks').json() == []
    assert '自動保存は行っていません' in payloads[1]['messages'][0]['content']


def test_missing_character_profile_uses_default_persona():
    prompt = chat_module.build_system_prompt()
    assert chat_module.DEFAULT_PERSONA in prompt
    assert chat_module.SYSTEM_PROMPT in prompt


def test_wake_status_uses_japanese_norn(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(wake_module, 'model_available', lambda: True)
    with TestClient(norn_app.app) as client:
        status = client.get('/api/wake/status').json()
    assert status['phrase'] == 'ねぇノルン'
    assert status['engine'] == 'vosk-ja-keyword-grammar'
    assert status['processing'] == 'local'


def calibration_audio():
    import io
    import wave

    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(bytes(32000))
    return buffer.getvalue()


def test_initial_calibration_persists_and_skips_future_startups(monkeypatch, tmp_path):
    import numpy as np

    calls = []

    def fake_enroll(recording, destination):
        assert recording.is_file()
        calls.append(recording)
        np.savez(destination, threshold=np.array(0.04),
                 **{f'template_{index}': np.eye(13, 20) for index in range(3)})
        return {'examples': 3}

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(wake_module, 'enroll', fake_enroll)
    with TestClient(norn_app.app) as client:
        assert client.get('/api/wake/status').json()['calibration_required'] is True
        response = client.post('/api/wake/calibration', data={'consent': 'true'},
                               files={'audio': ('sample.wav', calibration_audio(), 'audio/wav')})
        assert response.status_code == 200 and response.json()['calibrated'] is True
        assert client.get('/api/wake/status').json()['calibration_required'] is False
    with TestClient(norn_app.app) as client:
        assert client.get('/api/wake/status').json()['calibration_required'] is False
        assert client.post('/api/wake/calibration', data={'consent': 'true'},
                           files={'audio': ('sample.wav', calibration_audio(), 'audio/wav')}).status_code == 409
    assert len(calls) == 1
    assert not calls[0].exists()


def test_initial_calibration_failure_remains_required(monkeypatch, tmp_path):
    def failed_enroll(*args, **kwargs):
        raise ValueError('Not enough examples')

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(wake_module, 'enroll', failed_enroll)
    with TestClient(norn_app.app) as client:
        response = client.post('/api/wake/calibration', data={'consent': 'true'},
                               files={'audio': ('sample.wav', calibration_audio(), 'audio/wav')})
        assert response.status_code == 422
        assert client.get('/api/wake/status').json()['calibration_required'] is True
    assert not list((tmp_path / 'wake-calibration').glob('*.wav'))


def test_personal_wake_enrollment_keeps_prefix_and_name_together(monkeypatch, tmp_path):
    import numpy as np
    import personal_wake

    intervals = np.array([[base, base + length] for offset in (0, 32000, 64000, 96000)
                          for base, length in ((offset, 3200), (offset + 6400, 4800))])
    monkeypatch.setattr(personal_wake.sf, 'read', lambda path: (np.ones(112000, dtype=np.float32), 16000))
    monkeypatch.setattr(personal_wake.librosa.effects, 'split', lambda *args, **kwargs: intervals)
    lengths = []

    def fake_features(audio):
        lengths.append(len(audio))
        return np.eye(13, 30)

    monkeypatch.setattr(personal_wake, 'features', fake_features)
    monkeypatch.setattr(personal_wake, 'distance', lambda *args: 0.02)
    result = personal_wake.enroll(tmp_path / 'synthetic.wav', destination=tmp_path / 'profile.npz')
    assert result['examples'] == 4
    assert lengths == [11200] * 4


@pytest.mark.parametrize(('consent', 'mime', 'origin', 'expected'), [
    ('false', 'audio/wav', None, 400),
    ('true', 'text/plain', None, 415),
    ('true', 'audio/wav', 'https://example.invalid', 403),
])
def test_initial_calibration_requires_consent_and_local_audio(monkeypatch, tmp_path, consent, mime, origin, expected):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/wake/calibration', data={'consent': consent},
                               headers={'origin': origin} if origin else {},
                               files={'audio': ('sample.wav', calibration_audio(), mime)})
    assert response.status_code == expected
    assert not wake_module.PROFILE_PATH.exists()


def test_invalid_profile_does_not_skip_initial_calibration(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    wake_module.PROFILE_PATH.write_bytes(b'broken profile')
    with TestClient(norn_app.app) as client:
        assert client.get('/api/wake/status').json()['calibration_required'] is True


@pytest.mark.parametrize(('contents', 'expected'), [(b'not encoded audio', 422), (bytes(6 * 1024 * 1024 + 1), 413)])
def test_initial_calibration_rejects_bad_recording(monkeypatch, tmp_path, contents, expected):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        response = client.post('/api/wake/calibration', data={'consent': 'true'},
                               files={'audio': ('sample.webm', contents, 'audio/webm')})
        assert response.status_code == expected
        assert client.get('/api/wake/status').json()['calibration_required'] is True


def test_wake_waiting_audio_does_not_transcribe_or_chat(monkeypatch, tmp_path):
    calls = []

    class FakeDetector:
        def reset(self):
            calls.append('reset')

        def predict(self, frame):
            calls.append(frame)
            return frame == b'norn-frame'

    def forbidden(*args, **kwargs):
        raise AssertionError('Waiting audio must not reach STT or Gemini')

    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(wake_module, 'model_available', lambda: True)
    monkeypatch.setattr(wake_module, 'KeywordDetector', FakeDetector)
    monkeypatch.setattr(voice_module, 'transcribe_file', forbidden)
    monkeypatch.setattr(chat_module, 'generate_llm_reply', forbidden)
    with TestClient(norn_app.app) as client:
        with client.websocket_connect('/api/wake/listen', headers={'origin': settings_module.NORN_FRONTEND_URL}) as socket:
            assert socket.receive_json()['phrase'] == 'ねぇノルン'
            socket.send_bytes(b'silence')
            socket.send_text('reset')
            socket.send_bytes(b'norn-frame')
            assert socket.receive_json() == {'wake': True}
    assert calls == [b'silence', 'reset', b'norn-frame']


def test_wake_rejects_foreign_origin(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    with TestClient(norn_app.app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect('/api/wake/listen', headers={'origin': 'https://example.invalid'}):
                pass
    assert error.value.code == 1008


def test_wake_missing_model_stops_before_audio(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setattr(wake_module, 'model_available', lambda: False)
    with TestClient(norn_app.app) as client:
        with client.websocket_connect('/api/wake/listen', headers={'origin': settings_module.NORN_FRONTEND_URL}) as socket:
            assert 'error' in socket.receive_json()
            with pytest.raises(WebSocketDisconnect) as error:
                socket.receive_json()
    assert error.value.code == 1013


@pytest.mark.parametrize('frame', [b'', b'abc', bytes(32772), struct.pack('<f', float('nan')), struct.pack('<f', 2.0)])
def test_wake_rejects_invalid_frames(frame):
    detector = object.__new__(wake_module.KeywordDetector)
    with pytest.raises(ValueError):
        detector.predict(frame)


@pytest.mark.parametrize(('words', 'expected'), [
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': 'ノルン', 'conf': 1.0, 'start': 0.25, 'end': 0.6}], True),
    ([{'word': '[unk]', 'conf': 1.0, 'start': 0.0, 'end': 0.03}, {'word': 'ねえ', 'conf': 1.0, 'start': 0.1, 'end': 0.3}, {'word': 'ノルン', 'conf': 1.0, 'start': 0.35, 'end': 0.7}], True),
    ([{'word': 'ノルン', 'conf': 1.0, 'start': 0.1, 'end': 0.5}], False),
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.1, 'end': 0.5}], False),
    ([{'word': 'ねえ', 'conf': 0.5, 'start': 0.0, 'end': 0.2}, {'word': 'ノルン', 'conf': 1.0, 'start': 0.25, 'end': 0.6}], False),
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': 'ノルン', 'conf': 0.5, 'start': 0.25, 'end': 0.6}], False),
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': 'ノルン', 'conf': 1.0, 'start': 0.25, 'end': 0.3}], False),
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': 'ノルン', 'conf': 1.0, 'start': 1.5, 'end': 1.9}], False),
    ([{'word': 'ノルン', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': 'ねえ', 'conf': 1.0, 'start': 0.25, 'end': 0.6}], False),
    ([{'word': 'ねえ', 'conf': 1.0, 'start': 0.0, 'end': 0.2}, {'word': '[unk]', 'conf': 1.0, 'start': 0.2, 'end': 0.3}, {'word': 'ノルン', 'conf': 1.0, 'start': 0.35, 'end': 0.7}], False),
    ([{'word': '[unk]', 'conf': 1.0, 'start': 0.1, 'end': 0.5}], False),
    ([{'word': 'Hey Emma', 'conf': 1.0, 'start': 0.1, 'end': 0.5}], False),
    ([{'word': 'ノルン', 'conf': 0.5, 'start': 0.1, 'end': 0.5}], False),
    ([{'word': 'ノルン', 'conf': 1.0, 'start': 0.1, 'end': 0.12}], False),
])
def test_wake_requires_complete_confident_nee_norn_phrase(monkeypatch, words, expected):
    detector = object.__new__(wake_module.KeywordDetector)
    detector.recognizer = SimpleNamespace(
        AcceptWaveform=lambda frame: True,
        Result=lambda: json.dumps({'result': words}),
    )
    monkeypatch.setattr(detector, 'reset', lambda: None)
    assert detector.predict(struct.pack('<f', 0.1)) is expected


@pytest.mark.parametrize('matched', [True, False])
def test_personal_wake_result_combines_with_standard_detector(monkeypatch, matched):
    detector = object.__new__(wake_module.KeywordDetector)
    detector.personal = SimpleNamespace(predict=lambda frame: matched)
    detector.recognizer = SimpleNamespace(AcceptWaveform=lambda frame: True,
                                         Result=lambda: json.dumps({'result': []}))
    resets = []
    monkeypatch.setattr(detector, 'reset', lambda: resets.append(True))
    assert detector.predict(struct.pack('<f', 0.1)) is matched
    assert len(resets) == int(matched)


@pytest.mark.parametrize(('threshold', 'rows', 'count'), [(0.5, 13, 3), (0.05, 12, 3), (0.05, 13, 2)])
def test_personal_wake_rejects_invalid_profile(tmp_path, threshold, rows, count):
    import numpy as np
    from personal_wake import PersonalWakeDetector

    path = tmp_path / 'profile.npz'
    np.savez(path, threshold=np.array(threshold),
             **{f'template_{index}': np.zeros((rows, 20)) for index in range(count)})
    with pytest.raises(ValueError):
        PersonalWakeDetector(path)


def test_personal_wake_enrollment_rejects_insufficient_examples(tmp_path):
    import numpy as np
    import soundfile as sf
    from personal_wake import enroll

    recording = tmp_path / 'empty.wav'
    sf.write(recording, np.zeros(16000), 16000)
    with pytest.raises(ValueError):
        enroll(recording, tmp_path / 'profile.npz')
    assert not (tmp_path / 'profile.npz').exists()


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


@pytest.fixture
def speech_configuration_files(tmp_path):
    root = tmp_path / 'engine'
    model = tmp_path / 'voice-assets/custom-voice.safetensors'
    files = [root / '.venv/bin/python', root / '.venv/Scripts/python.exe', model,
             model.parent / 'config.json', model.parent / 'style_vectors.npy']
    for file in files:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b'test asset')
    return {'NORN_TTS_ROOT': 'engine', 'NORN_TTS_MODEL': 'voice-assets/custom-voice.safetensors'}


@pytest.mark.parametrize(('platform', 'python_path'), [('linux', '.venv/bin/python'), ('darwin', '.venv/bin/python'), ('win32', '.venv/Scripts/python.exe')])
def test_speech_configuration_is_not_tied_to_private_model(speech_configuration_files, tmp_path, platform, python_path):
    config = tts_module.speech_configuration(speech_configuration_files, base=tmp_path, platform=platform)
    assert config.root == tmp_path / 'engine'
    assert config.model == tmp_path / 'voice-assets/custom-voice.safetensors'
    assert config.python == config.root / python_path
    assert config.device == 'auto'


def test_speech_configuration_preserves_virtual_environment_python_symlink(speech_configuration_files, tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime with spaces'
    runtime.mkdir()
    executable = runtime / 'python'
    executable.symlink_to(tmp_path / 'engine/.venv/bin/python')
    speech_configuration_files['NORN_TTS_PYTHON'] = 'runtime with spaces/python'
    monkeypatch.chdir(tmp_path.parent)
    config = tts_module.speech_configuration(speech_configuration_files, base=tmp_path)
    assert config.python == executable
    assert config.python != executable.resolve()


def test_speech_configuration_accepts_explicit_asset_paths(speech_configuration_files, tmp_path):
    env = speech_configuration_files
    for key, name in [('NORN_TTS_PYTHON', 'python-custom'), ('NORN_TTS_CONFIG', 'voice-settings.json'), ('NORN_TTS_STYLE_VECTORS', 'styles.npy')]:
        (tmp_path / name).write_bytes(b'test')
        env[key] = name
    env['NORN_TTS_DEVICE'] = 'cpu'
    env['NORN_TTS_SPEAKER_ID'] = '2'
    env['NORN_TTS_STYLE'] = 'Happy'
    config = tts_module.speech_configuration(env, base=tmp_path)
    assert config.python == tmp_path / 'python-custom'
    assert config.config == tmp_path / 'voice-settings.json'
    assert config.style_vectors == tmp_path / 'styles.npy'
    assert (config.device, config.speaker_id, config.style) == ('cpu', 2, 'Happy')


@pytest.mark.parametrize(('key', 'value'), [('NORN_TTS_DEVICE', 'invalid'), ('NORN_TTS_SPEAKER_ID', '-1'), ('NORN_TTS_CONFIG', 'missing.json'), ('NORN_TTS_MODEL', 'missing.safetensors')])
def test_speech_configuration_rejects_bad_settings(speech_configuration_files, tmp_path, key, value):
    speech_configuration_files[key] = value
    with pytest.raises(ValueError):
        tts_module.speech_configuration(speech_configuration_files, base=tmp_path)


def test_speech_is_disabled_by_default(monkeypatch):
    monkeypatch.delenv('NORN_TTS_ENABLED', raising=False)
    service = tts_module.SpeechService()
    service.start()
    assert service.state['state'] == 'disabled'
    assert service.process is None


def test_speech_enabled_without_configuration_keeps_text_api_available(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'norn.sqlite3')
    monkeypatch.setenv('NORN_TTS_ENABLED', '1')
    monkeypatch.delenv('NORN_TTS_ROOT', raising=False)
    monkeypatch.delenv('NORN_TTS_MODEL', raising=False)
    with TestClient(norn_app.app) as client:
        assert client.get('/api/speech/status').json()['state'] == 'unconfigured'
        assert client.get('/api/status').status_code == 200
        assert client.post('/api/speech', json={'text': 'テスト'}).status_code == 503
        assert tts_module.speech.process is None


@pytest.mark.parametrize(('requested', 'available', 'expected'), [('auto', False, 'cpu'), ('auto', True, 'cuda'), ('cpu', True, 'cpu'), ('cuda:0', True, 'cuda:0')])
def test_speech_worker_device_selection(requested, available, expected):
    from tts_worker import select_device

    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: available, device_count=lambda: 1))
    assert select_device(requested, torch) == expected


@pytest.mark.parametrize('requested', ['cuda', 'cuda:0'])
def test_speech_worker_rejects_explicit_cuda_without_gpu(requested):
    from tts_worker import select_device

    torch = SimpleNamespace(cuda=SimpleNamespace(is_available=lambda: False))
    with pytest.raises(RuntimeError):
        select_device(requested, torch)


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


async def no_op_automatic_tasks(*args, **kwargs):
    return []


def test_deterministic_action_reply_requires_one_persisted_action(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'deterministic-reply.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '楽器の練習', 'estimated_minutes': 30}).json()
        arguments = {'task_id': task['id'], 'title': '新しい練習名'}
        action = tasks_module.execute_task_tool('update_task', arguments)
        result = {'tool': 'update_task', 'arguments': arguments, **action}

        assert chat_module.deterministic_action_reply([result]) == action['summary']

        stale_result = {**result, 'task': {**result['task'], 'title': '別の作業'}}
        assert chat_module.deterministic_action_reply([stale_result]) is None
        assert chat_module.deterministic_action_reply([result, result]) is None
        assert chat_module.deterministic_action_reply([{**result, 'ok': False}]) is None
        assert chat_module.deterministic_action_reply([{
            'tool': 'create_task', 'ok': True, 'task': task, 'summary': 'タスクを追加しました。',
        }]) is None


def test_automatic_task_reply_uses_database_checked_template_without_gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'automatic-deterministic-reply.sqlite3')

    def forbidden_client(*args, **kwargs):
        pytest.fail('Geminiを呼び出してはいけません。')

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', forbidden_client)
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '譜面を確認'}).json()
        result = {
            'ok': True,
            'task': task,
            'summary': 'タスク「譜面を確認」を追加しました。',
            'scheduling': {'state': 'needs_estimate'},
        }

        reply = asyncio.run(chat_module.generate_action_reply('test', [], [result]))

    assert reply == 'タスク「譜面を確認」を追加しました。 所要時間は何分くらいにしますか？'


def test_simple_tool_action_skips_gemini_claim_verification(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'simple-tool-reply.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    verification_calls = []

    async def record_verification(*args, **kwargs):
        verification_calls.append(args[1])
        return True

    monkeypatch.setattr(chat_module, 'verify_schedule_reply', record_verification)
    calls = []
    start_at = future_task_start.isoformat()
    end_at = (future_task_start + timedelta(hours=1)).isoformat()
    arguments = json.dumps({
        'title': '定型応答の確認',
        'start_at': start_at,
        'end_at': end_at,
    })

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
                message = {
                    'content': None,
                    'tool_calls': [{
                        'id': 'create-event',
                        'type': 'function',
                        'function': {
                            'name': 'create_event',
                            'arguments': arguments,
                        },
                    }],
                }
            else:
                message = {'content': '定型応答の確認を予定に登録しました。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app, raise_server_exceptions=False) as client:
        response = client.post('/api/chat', json={'content': '定型応答の確認を予定に追加して'})

    assert response.status_code == 200
    assert len(calls) == 2
    assert not verification_calls, verification_calls
    assert response.json()['assistant_message']['content'] == f'「定型応答の確認」を{start_at}に追加しました。'


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
    assert response.json()['assistant_message']['content'] == '「Gemini連携テスト」を2026-11-02T14:00:00+09:00に追加しました。'
    assert len(events) == 1
    assert events[0]['title'] == 'Gemini連携テスト'
    tool_names = {tool['function']['name'] for tool in request_payloads[0]['tools']}
    assert tool_names == {
        'list_events', 'create_event', 'update_event', 'delete_event',
        'extend_schedule',
        'apply_schedule',
        'schedule_week_plan',
        'schedule_routine', 'list_tasks', 'create_task', 'update_task', 'complete_task', 'delete_task',
        'find_task_slots', 'plan_task_allocations', 'plan_week_allocations', 'schedule_task', 'unschedule_task',
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
    database_module.initialize_database()
    with database_module.open_database() as migrated:
        columns = {row['name'] for row in migrated.execute('PRAGMA table_info(tasks)').fetchall()}
        task = migrated.execute('SELECT * FROM tasks WHERE id = ?', ('legacy-task',)).fetchone()

    assert 'project_id' in columns
    assert task['title'] == '既存タスク'
    assert task['project_id'] is None


def test_initialize_database_migrates_schedule_and_routine_models(monkeypatch, tmp_path):
    database_path = tmp_path / 'legacy-schedule.sqlite3'
    connection = sqlite3.connect(database_path)
    connection.executescript(
        '''
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            due_at TEXT, estimated_minutes INTEGER, priority TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, completed_at TEXT
        );
        CREATE TABLE task_allocations (
            id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
            start_at TEXT NOT NULL, end_at TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE routine_occurrences (
            source_task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            occurrence_date TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE CASCADE,
            PRIMARY KEY (source_task_id, occurrence_date)
        );
        INSERT INTO tasks (id, title, priority, status, created_at, updated_at)
        VALUES ('legacy-task', '既存タスク', 'normal', 'open', '2026-01-01', '2026-01-01');
        INSERT INTO task_allocations (id, task_id, start_at, end_at, created_at, updated_at)
        VALUES ('legacy-allocation', 'legacy-task', '2026-01-02T10:00:00+09:00',
                '2026-01-02T11:00:00+09:00', '2026-01-01', '2026-01-01');
        INSERT INTO routine_occurrences (source_task_id, occurrence_date, task_id)
        VALUES ('legacy-task', '2026-01-02', 'legacy-task');
        ''',
    )
    connection.commit()
    connection.close()

    monkeypatch.setattr(database_module, 'DB_PATH', database_path)
    database_module.initialize_database()
    with database_module.open_database() as migrated:
        task_columns = {row['name'] for row in migrated.execute('PRAGMA table_info(tasks)')}
        event_columns = {row['name'] for row in migrated.execute('PRAGMA table_info(events)')}
        routine_occurrence_columns = {
            row['name'] for row in migrated.execute('PRAGMA table_info(routine_occurrences)')
        }
        allocation_indexes = migrated.execute("PRAGMA index_list('task_allocations')").fetchall()
        allocation = migrated.execute(
            'SELECT * FROM task_allocations WHERE id = ?', ('legacy-allocation',),
        ).fetchone()
        legacy_link = migrated.execute(
            'SELECT * FROM task_occurrence_links WHERE source_task_id = ?', ('legacy-task',),
        ).fetchone()
        migrated.execute(
            '''INSERT INTO task_allocations (id, task_id, start_at, end_at, created_at, updated_at)
               VALUES ('second-allocation', 'legacy-task', '2026-01-03T10:00:00+09:00',
                       '2026-01-03T11:00:00+09:00', '2026-01-01', '2026-01-01')''',
        )

    assert {'remaining_minutes', 'focus_level', 'splittable', 'flexibility'} <= task_columns
    assert {'kind', 'source', 'task_id', 'is_fixed', 'is_important'} <= event_columns
    assert {'routine_id', 'plan_id', 'schedule_id', 'status'} <= routine_occurrence_columns
    assert allocation['id'] == 'legacy-allocation'
    assert legacy_link['occurrence_date'] == '2026-01-02'
    assert not any(index['unique'] and index['origin'] == 'u' for index in allocation_indexes)


def test_task_can_have_multiple_validated_allocations(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'multi-allocation.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '分割練習', 'estimated_minutes': 90, 'splittable': True,
            'min_session_minutes': 30, 'max_session_minutes': 60,
        }).json()
        first = client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': future_task_start.isoformat(),
            'end_at': (future_task_start + timedelta(minutes=45)).isoformat(),
        })
        second = client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': (future_task_start + timedelta(minutes=45)).isoformat(),
            'end_at': (future_task_start + timedelta(minutes=90)).isoformat(),
        })
        allocations = client.get(f"/api/tasks/{task['id']}/allocations")
        overscheduled = client.post(f"/api/tasks/{task['id']}/allocations", json={
            'start_at': (future_task_start + timedelta(hours=2)).isoformat(),
            'end_at': (future_task_start + timedelta(hours=2, minutes=30)).isoformat(),
        })

    assert first.status_code == second.status_code == 201
    assert len(allocations.json()) == 2
    assert overscheduled.status_code == 409
    assert overscheduled.json()['detail'] == '作業枠の合計が残り見積時間を超えます。'


def test_task_allocation_plan_splits_around_existing_events_without_saving(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'allocation-plan.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '分割計画', 'estimated_minutes': 90, 'splittable': True,
            'min_session_minutes': 30, 'max_session_minutes': 60,
        }).json()
        client.post('/api/events', json={
            'title': '固定予定',
            'start_at': (future_task_start + timedelta(minutes=45)).isoformat(),
            'end_at': (future_task_start + timedelta(minutes=60)).isoformat(),
        })

        plan = client.get(f"/api/tasks/{task['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(hours=4)).isoformat(),
        })
        allocations = client.get(f"/api/tasks/{task['id']}/allocations").json()
        expired_task = client.post('/api/tasks', json={
            'title': '期限切れ', 'estimated_minutes': 30, 'due_at': (future_task_start - timedelta(minutes=1)).isoformat(),
        }).json()
        expired_plan = client.get(f"/api/tasks/{expired_task['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(hours=1)).isoformat(),
        })
        non_splittable = client.post('/api/tasks', json={
            'title': '分割不可', 'estimated_minutes': 90, 'splittable': False,
        }).json()
        non_splittable_plan = client.get(f"/api/tasks/{non_splittable['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(hours=4)).isoformat(),
        })
        constrained = client.post('/api/tasks', json={
            'title': '上限超過', 'estimated_minutes': 90, 'splittable': False, 'max_session_minutes': 60,
        }).json()
        constrained_plan = client.get(f"/api/tasks/{constrained['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(hours=4)).isoformat(),
        })

    assert plan.status_code == 200
    assert plan.json()['complete'] is True
    assert plan.json()['allocated_minutes'] == 90
    assert plan.json()['unallocated_minutes'] == 0
    assert [slot['duration_minutes'] for slot in plan.json()['allocations']] == [45, 45]
    assert plan.json()['allocations'][0]['end_at'] <= plan.json()['allocations'][1]['start_at']
    assert allocations == []
    assert expired_plan.json()['complete'] is False
    assert expired_plan.json()['unallocated_minutes'] == 30
    assert expired_plan.json()['allocations'] == []
    assert len(non_splittable_plan.json()['allocations']) == 1
    assert non_splittable_plan.json()['allocations'][0]['duration_minutes'] == 90
    assert constrained_plan.json()['complete'] is False
    assert constrained_plan.json()['unallocated_minutes'] == 90


def test_tasks_are_splittable_by_default_and_legacy_rows_migrate_once(monkeypatch, tmp_path):
    db_path = tmp_path / 'splittable.sqlite3'
    monkeypatch.setattr(database_module, 'DB_PATH', db_path)
    with TestClient(norn_app.app) as client:
        created = client.post('/api/tasks', json={'title': '新規'}).json()
        assert created['splittable'] == 1
        legacy = client.post('/api/tasks', json={'title': '旧', 'splittable': False}).json()
    import sqlite3
    with sqlite3.connect(db_path) as connection:
        connection.execute('UPDATE tasks SET splittable = 0')
        connection.execute('PRAGMA user_version = 0')
    with TestClient(norn_app.app) as client:
        assert {task['splittable'] for task in client.get('/api/tasks').json()} == {1}
        client.patch(f"/api/tasks/{legacy['id']}", json={'splittable': False})
    with TestClient(norn_app.app) as client:
        assert next(task for task in client.get('/api/tasks').json() if task['id'] == legacy['id'])        ['splittable'] == 0


def test_allocation_plan_distributes_over_distinct_days_and_reports_expired(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'distinct-days.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '3日配分', 'estimated_minutes': 60}).json()
        plan = client.get(f"/api/tasks/{task['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=7)).isoformat(),
            'distinct_days': 3,
        }).json()
        expired = client.post('/api/tasks', json={
            'title': '期限切れ', 'estimated_minutes': 60, 'due_at': (future_task_start - timedelta(days=1)).isoformat(),
        }).json()
        expired_plan = client.get(f"/api/tasks/{expired['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=3)).isoformat(),
        }).json()
        invalid = client.get(f"/api/tasks/{task['id']}/allocation-plan", params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=3)).isoformat(),
            'distinct_days': 9,
        })

    assert plan['complete'] is True
    assert [slot['duration_minutes'] for slot in plan['allocations']] == [20, 20, 20]
    assert len({slot['start_at'][:10] for slot in plan['allocations']}) == 3
    assert expired_plan['reason'] == 'deadline_passed'
    assert invalid.status_code == 422


def test_week_plan_orders_by_priority_and_respects_density_and_breaks(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'week-plan.sqlite3')
    with TestClient(norn_app.app) as client:
        low = client.post('/api/tasks', json={'title': '低優先', 'estimated_minutes': 240, 'priority': 'low'}).json()
        high = client.post('/api/tasks', json={
            'title': '高優先', 'estimated_minutes': 240, 'priority': 'high',
            'due_at': (future_task_start + timedelta(days=3)).isoformat(),
        }).json()
        client.post('/api/tasks', json={'title': '見積なし'})
        response = client.get('/api/task-allocation-plan', params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=7)).isoformat(),
        })
        bad = client.get('/api/task-allocation-plan', params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=40)).isoformat(),
        })

    plan = response.json()
    assert bad.status_code == 422
    assert [item['task_id'] for item in plan['tasks']] == [high['id'], low['id']]
    assert plan['tasks'][0]['priority_score'] > plan['tasks'][1]['priority_score']
    assert all(item['complete'] for item in plan['tasks'])
    assert [item['reason'] for item in plan['skipped']] == ['missing_estimate']
    assert all(day['ratio'] <= 0.85 for day in plan['days'])
    blocks = sorted(
        (datetime.fromisoformat(slot['start_at']), datetime.fromisoformat(slot['end_at']), slot['duration_minutes'])
        for item in plan['tasks'] for slot in item['allocations']
    )
    assert all(second[0] - first[1] >= timedelta(minutes=10) for first, second in zip(blocks, blocks[1:]) if first[0].date() == second[0].date())
    assert any(second[0] - first[1] == timedelta(minutes=10) for first, second in zip(blocks, blocks[1:]) if first[0].date() == second[0].date())
    assert all(block[0].minute % 5 == 0 for block in blocks)
    assert all(duration <= 90 for _, _, duration in blocks)
    due = future_task_start + timedelta(days=3)
    assert all(datetime.fromisoformat(slot['end_at']) <= due for slot in plan['tasks'][0]['allocations'])


def test_prompt_snapshot_limits_events_to_window_and_split_reply_is_server_generated(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'compact.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    posts = []
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '分割案', 'estimated_minutes': 60}).json()
        for year_offset in range(1, 4):
            start = future_task_start + timedelta(days=365 * year_offset)
            client.post('/api/events', json={'title': f'遠い予定{year_offset}', 'start_at': start.isoformat(), 'end_at': (start + timedelta(hours=1)).isoformat()})
        client.post('/api/events', json={'title': '近い予定', 'start_at': future_task_start.isoformat(), 'end_at': (future_task_start + timedelta(hours=1)).isoformat()})
        snapshot = actions_module.schedule_snapshot(event_window_days=35)
        assert [event['title'] for event in snapshot['events']] == ['近い予定']
        assert 'recurrence_json' not in snapshot['events'][0]
        actions = [
            {'name': 'create_task_allocation', 'arguments_json': json.dumps({
                'task_id': task['id'],
                'start_at': (future_task_start + timedelta(days=day, hours=2)).isoformat(),
                'end_at': (future_task_start + timedelta(days=day, hours=2, minutes=20)).isoformat(),
            })}
            for day in range(3)
        ]

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json, headers):
                posts.append(json)
                message = {'content': None, 'tool_calls': [{'id': 'p', 'type': 'function', 'function': {'name': 'apply_schedule', 'arguments': globals()['json'].dumps({'actions': actions})}}]}
                return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

        monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
        reply = client.post('/api/chat', json={'content': '分割案を3日に分けて配置して'}).json()['assistant_message']['content']
        assert len(client.get(f"/api/tasks/{task['id']}/allocations").json()) == 3

    assert len(posts) == 1
    assert '「分割案」を3回に分けて配置したよ' in reply and reply.count('20分') == 3 and '保存はしていません' not in reply


def test_every_chat_tool_has_an_executor_route():
    def names(tools):
        return {tool['function']['name'] for tool in tools}

    special = {'extend_schedule', 'apply_schedule', 'schedule_week_plan', 'schedule_routine'}
    routed_elsewhere = names(chat_module.TASK_TOOLS) | names(chat_module.MEMORY_TOOLS) | names(chat_module.GOAL_PROJECT_TOOLS) | special
    everything = names([*chat_module.SCHEDULE_TOOLS, *chat_module.TASK_TOOLS, *chat_module.MEMORY_TOOLS, *chat_module.GOAL_PROJECT_TOOLS, *chat_module.REMINDER_TOOLS, *chat_module.BATCH_TOOLS, *chat_module.ROUTINE_TOOLS])
    fallback = everything - routed_elsewhere
    assert fallback == {'list_events', 'create_event', 'update_event', 'delete_event'}


def test_week_plan_dependencies_order_tasks_and_adaptive_break(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'week-deps.sqlite3')
    with TestClient(norn_app.app) as client:
        first = client.post('/api/tasks', json={'title': '前提', 'estimated_minutes': 30, 'priority': 'low', 'focus_level': 5}).json()
        second = client.post('/api/tasks', json={'title': '後続', 'estimated_minutes': 30, 'priority': 'high'}).json()
        no_estimate = client.post('/api/tasks', json={'title': '見積なし'}).json()
        blocked = client.post('/api/tasks', json={'title': '待ち', 'estimated_minutes': 30}).json()
        assert client.put(f"/api/tasks/{second['id']}/dependencies", json={'depends_on': [first['id']]}).json()['depends_on'] == [first['id']]
        assert client.put(f"/api/tasks/{first['id']}/dependencies", json={'depends_on': [second['id']]}).status_code == 409
        assert client.put(f"/api/tasks/{first['id']}/dependencies", json={'depends_on': [first['id']]}).status_code == 422
        assert client.put(f"/api/tasks/{first['id']}/dependencies", json={'depends_on': [str(uuid.uuid4())]}).status_code == 404
        assert client.put(f"/api/tasks/{blocked['id']}/dependencies", json={'depends_on': [no_estimate['id']]}).status_code == 200
        listed = {task['id']: task for task in client.get('/api/tasks').json()}
        plan = client.get('/api/task-allocation-plan', params={
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(days=3)).isoformat(),
        }).json()

    assert listed[second['id']]['depends_on'] == [first['id']]
    by_id = {item['task_id']: item for item in plan['tasks']}
    assert by_id[first['id']]['score_components']['dependency'] == 100
    first_block = by_id[first['id']]['allocations'][0]
    second_block = by_id[second['id']]['allocations'][0]
    assert second_block['start_at'] >= first_block['end_at']
    assert first_block['break_after_minutes'] == 15
    assert second_block['break_after_minutes'] == 10
    assert {item['task_id']: item['reason'] for item in plan['skipped']} == {no_estimate['id']: 'missing_estimate', blocked['id']: 'prerequisite_unplanned'}


def test_allocations_accept_five_minute_start_and_reject_others(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'five-minutes.sqlite3')
    with TestClient(norn_app.app) as client:
        task_id = client.post('/api/tasks', json={'title': '5分刻み', 'estimated_minutes': 20}).json()['id']
        start = future_task_start.replace(minute=5)
        accepted = client.post(f'/api/tasks/{task_id}/allocations', json={'start_at': start.isoformat(), 'end_at': (start + timedelta(minutes=20)).isoformat()})
        rejected = client.post(f'/api/tasks/{task_id}/allocations', json={'start_at': (start + timedelta(hours=1, minutes=2)).isoformat(), 'end_at': (start + timedelta(hours=1, minutes=22)).isoformat()})

    assert accepted.status_code == 201 or accepted.status_code == 200
    assert rejected.status_code == 409 and '5分刻み' in rejected.json()['detail']


def test_chat_schedule_week_plan_is_server_built_and_applies_immediately(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'week-chat.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    posts = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            posts.append(json)
            if len(posts) > 1:
                return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': '反映しました。'}}]})
            arguments = globals()['json'].dumps({'from_at': future_task_start.isoformat(), 'through_at': (future_task_start + timedelta(days=7)).isoformat()})
            message = {'content': None, 'tool_calls': [{'id': 'w', 'type': 'function', 'function': {'name': 'schedule_week_plan', 'arguments': arguments}}]}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        ids = [client.post('/api/tasks', json={'title': title, 'estimated_minutes': 120, 'priority': priority}).json()['id']
               for title, priority in (('資料作成', 'high'), ('片付け', 'low'))]
        client.post('/api/tasks', json={'title': '見積なし'})
        before = client.get('/api/task-allocations', params={'start': future_task_start.isoformat(), 'end': (future_task_start + timedelta(days=8)).isoformat()}).json()
        response = client.post('/api/chat', json={'content': '今週のタスクを全部配置して'})
        reply = response.json()['assistant_message']['content']
        applied = client.get('/api/task-allocations', params={'start': future_task_start.isoformat(), 'end': (future_task_start + timedelta(days=8)).isoformat()}).json()

    assert len(posts) == 1
    assert '2件のタスクを優先度順に配置したよ' in reply and '保存はしていません' not in reply and '「見積なし」は見積時間が未設定' in reply
    assert before == []
    assert {allocation['task_id'] for allocation in applied} == set(ids)
    assert sum(1 for _ in applied) >= 2


def test_chat_routes_plan_task_allocations_to_task_tools(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'plan-route.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    posts = []

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, json, headers):
            posts.append(json)
            if len(posts) == 1:
                arguments = globals()['json'].dumps({
                    'task_id': task_id, 'from_at': future_task_start.isoformat(),
                    'through_at': (future_task_start + timedelta(days=7)).isoformat(), 'distinct_days': 3,
                })
                message = {'content': None, 'tool_calls': [{'id': 'p', 'type': 'function', 'function': {'name': 'plan_task_allocations', 'arguments': arguments}}]}
            else:
                message = {'content': '分割案を確認したよ。'}
            return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

    monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
    with TestClient(norn_app.app) as client:
        task_id = client.post('/api/tasks', json={'title': '分割案', 'estimated_minutes': 60}).json()['id']
        client.post('/api/chat', json={'content': '分割案を3日に分けて配置案を作って'})

    tool_message = next(message for message in posts[1]['messages'] if message['role'] == 'tool')
    result = json.loads(tool_message['content'])
    assert result['ok'] is True and len(result['allocations']) == 3


@pytest.mark.parametrize('tool_name', ['unschedule_task', 'delete_task'])
def test_chat_verifies_claims_about_every_removed_allocation(monkeypatch, tmp_path, future_task_start, tool_name):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / f'remove-{tool_name}.sqlite3')
    monkeypatch.setattr(chat_module, 'LLM_MODEL', 'test-model')
    monkeypatch.setattr(chat_module, 'extract_due_memories', no_op_memory_extraction)
    first_day = future_task_start
    with TestClient(norn_app.app) as client:
        task_id = client.post('/api/tasks', json={'title': '消す対象', 'estimated_minutes': 60}).json()['id']
        allocation_ids = [
            client.post(f'/api/tasks/{task_id}/allocations', json={
                'start_at': (first_day + timedelta(hours=day)).isoformat(),
                'end_at': (first_day + timedelta(hours=day, minutes=20)).isoformat(),
            }).json()['id'] for day in range(3)
        ]
        calls = []

        class FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json, headers):
                calls.append(json)
                if 'response_format' in json:
                    claims = [{'kind': 'allocation', 'id': allocation_id, 'fields': {}, 'exists': False, 'changed': True} for allocation_id in allocation_ids]
                    message = {'content': globals()['json'].dumps({'supported': True, 'claims': claims})}
                elif 'tools' in json and not any(message['role'] == 'tool' for message in json['messages']):
                    message = {'content': None, 'tool_calls': [{'id': 'r', 'type': 'function', 'function': {'name': tool_name, 'arguments': globals()['json'].dumps({'task_id': task_id})}}]}
                else:
                    message = {'content': '3件の予定を消したよ。'}
                return SimpleNamespace(is_error=False, raise_for_status=lambda: None, json=lambda: {'choices': [{'message': message}]})

        monkeypatch.setattr(chat_module.httpx, 'AsyncClient', FakeAsyncClient)
        response = client.post('/api/chat', json={'content': '消す対象の予定をすべて消して'})
        assert response.status_code == 200
        remaining = [] if tool_name == 'delete_task' else client.get(f'/api/tasks/{task_id}/allocations').json()
        assert remaining == []
    assert response.json()['assistant_message']['content'] == '3件の予定を消したよ。'
    assert len(calls) == 2


def test_split_allocation_applies_all_blocks_immediately(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'split-allocation.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '分割練習', 'estimated_minutes': 90, 'splittable': True,
            'min_session_minutes': 30, 'max_session_minutes': 60,
        }).json()
        client.post('/api/events', json={
            'title': '固定予定',
            'start_at': (future_task_start + timedelta(minutes=45)).isoformat(),
            'end_at': (future_task_start + timedelta(minutes=60)).isoformat(),
        })
        plan = tasks_module.execute_task_tool('plan_task_allocations', {
            'task_id': task['id'],
            'from_at': future_task_start.isoformat(),
            'through_at': (future_task_start + timedelta(hours=4)).isoformat(),
        })
        actions = [{
            'name': 'create_task_allocation',
            'arguments_json': json.dumps({
                'task_id': task['id'], 'start_at': block['start_at'], 'end_at': block['end_at'],
            }),
        } for block in plan['allocations']]
        result = actions_module.apply_schedule({'actions': actions})
        allocations = client.get(f"/api/tasks/{task['id']}/allocations").json()

    assert result['ok'] and result['state'] == 'applied'
    assert len(allocations) == 2
    assert sum(
        int((datetime.fromisoformat(item['end_at']) - datetime.fromisoformat(item['start_at'])).total_seconds() // 60)
        for item in allocations
    ) == 90


def test_task_metadata_is_persisted_and_validated(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'task-metadata.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={
            'title': '集中作業', 'estimated_minutes': 90, 'remaining_minutes': 60,
            'focus_level': 4, 'preferred_period': 'morning', 'splittable': True,
            'min_session_minutes': 20, 'max_session_minutes': 45,
            'context': 'ノートPC', 'flexibility': 'flexible',
        })
        invalid_bounds = client.post('/api/tasks', json={
            'title': '不正な作業', 'estimated_minutes': 60,
            'min_session_minutes': 45, 'max_session_minutes': 30,
        })
        assert task.status_code == 201
        task_id = task.json()['id']
        updated = client.patch(f'/api/tasks/{task_id}', json={'remaining_minutes': 30, 'focus_level': 2})
        invalid_update = client.patch(
            f'/api/tasks/{task_id}', json={'min_session_minutes': 50},
        )

    assert task.json()['remaining_minutes'] == 60
    assert task.json()['focus_level'] == 4
    assert task.json()['splittable'] == 1
    assert task.json()['flexibility'] == 'flexible'
    assert invalid_bounds.status_code == 422
    assert updated.status_code == 200
    assert updated.json()['remaining_minutes'] == 30
    assert updated.json()['focus_level'] == 2
    assert invalid_update.status_code == 422


def test_routine_definitions_and_plans_are_persisted(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-models.sqlite3')
    with TestClient(norn_app.app) as client:
        routine = client.post('/api/routines', json={
            'title': '週の練習', 'routine_type': 'achievement', 'frequency': 'weekly',
            'target_count': 3, 'duration_minutes': 45, 'min_count': 2, 'max_count': 4,
            'adjustment_policy': 'flexible', 'flexible': True,
        })
        routine_id = routine.json()['id']
        plan = client.post(f'/api/routines/{routine_id}/plans', json={
            'period_start': '2026-11-09', 'period_end': '2026-11-15',
            'target_count': 2, 'duration_minutes': 30,
            'adjustment_reason': '今週だけ調整', 'adjustment_source': 'user',
        })
        overlapping_plan = client.post(f'/api/routines/{routine_id}/plans', json={
            'period_start': '2026-11-15', 'period_end': '2026-11-21',
            'target_count': 2, 'duration_minutes': 30,
        })
        listed = client.get('/api/routine-plans', params={
            'routine_id': routine_id, 'start': '2026-11-10', 'end': '2026-11-11',
        })

    assert routine.status_code == 201
    assert routine.json()['flexible'] == 1
    assert plan.status_code == 201
    assert plan.json()['target_count'] == 2
    assert overlapping_plan.status_code == 409
    assert [row['id'] for row in listed.json()] == [plan.json()['id']]


def test_routine_update_validates_and_deactivates(monkeypatch, tmp_path):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'routine-update.sqlite3')
    with TestClient(norn_app.app) as client:
        routine = client.post('/api/routines', json={
            'title': '朝の習慣', 'duration_minutes': 20,
            'target_count': 2, 'min_count': 1, 'max_count': 3,
        }).json()
        invalid_update = client.put(f"/api/routines/{routine['id']}", json={'target_count': 4})
        updated = client.put(f"/api/routines/{routine['id']}", json={'title': '朝の記録'})
        deactivated = client.delete(f"/api/routines/{routine['id']}")
        active = client.get('/api/routines', params={'active': True})
        inactive = client.get('/api/routines', params={'active': False})

    assert invalid_update.status_code == 422
    assert updated.status_code == 200
    assert updated.json()['title'] == '朝の記録'
    assert deactivated.status_code == 204
    assert all(item['id'] != routine['id'] for item in active.json())
    assert any(item['id'] == routine['id'] and item['is_active'] == 0 for item in inactive.json())


def test_execution_log_records_actuals_without_completing_task(monkeypatch, tmp_path, future_task_start):
    monkeypatch.setattr(database_module, 'DB_PATH', tmp_path / 'execution-log.sqlite3')
    with TestClient(norn_app.app) as client:
        task = client.post('/api/tasks', json={'title': '実績記録用', 'estimated_minutes': 60}).json()
        response = client.post('/api/execution-logs', json={
            'task_id': task['id'],
            'started_at': future_task_start.isoformat(),
            'ended_at': (future_task_start + timedelta(minutes=35)).isoformat(),
            'status': 'partial',
            'resume_point': '次はテストを実行する',
        })
        logs = client.get('/api/execution-logs', params={'task_id': task['id']})
        updated_task = client.get('/api/tasks').json()[0]

    assert response.status_code == 201
    assert response.json()['actual_minutes'] == 35
    assert response.json()['status'] == 'partial'
    assert response.json()['resume_point'] == '次はテストを実行する'
    assert len(logs.json()) == 1
    assert updated_task['status'] == 'open'


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