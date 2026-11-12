from contextlib import contextmanager
import importlib
import json
import os
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError
import pytest

from api import model_checks, tasks
from test.test_codex_model import cli


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path / 'tasks.sqlite3')
    for key, value in {'LLM_MODEL': 'test-model', 'LLM_API_KEY': 'private-model-key',
                       'LLM_BASE_URL': 'https://model.example.org/v1', 'LLM_HTTP_BACKEND': 'requests',
                       'LLM_ENABLE_THINKING': 'false', 'LLM_MAX_OUTPUT_TOKENS': '4096',
                       'LLM_READ_TIMEOUT': '400', 'LLM_MAX_RETRIES': '0',
                       'STRUCTURED_OUTPUT_METHOD': 'function_calling'}.items():
        monkeypatch.setenv(key, value)
    app = FastAPI()
    app.include_router(model_checks.router)
    app.include_router(tasks.router)
    with TestClient(app) as value:
        yield value


@pytest.fixture
def model(monkeypatch):
    state = {'calls': [], 'settings': [], 'exception': None, 'mode': 'valid'}

    class Model:
        structured = False
        def with_structured_output(self, *args, **kwargs):
            self.structured = True
            return self
        def invoke(self, messages):
            state['calls'].append(messages)
            if state['exception']:
                raise state['exception']
            if not self.structured:
                return SimpleNamespace(content='' if state['mode'] == 'empty' else 'OK')
            if state['mode'] == 'bad_structure':
                return {'raw': None, 'parsed': None}
            item = json.loads(messages[1][1])['evidence'][0]
            return {'research_questions': [{'statement': '样例研究问题', 'evidence': [{
                'evidence_id': item['evidence_id'], 'quote': 'fabricated quote' if state['mode'] == 'bad_quote' else item['text'],
            }]}], 'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}

    @contextmanager
    def factory(**kwargs):
        state['settings'].append(kwargs)
        yield Model()

    monkeypatch.setattr(model_checks, 'analysis_llm', factory)
    return state


def start(client, **body):
    response = client.post('/api/v1/system/model-check', json=body)
    assert response.status_code == 202, response.text
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


@pytest.mark.parametrize('mode', ['chat', 'structured'])
def test_explicit_check_is_persisted_without_paper_database(client, model, mode, monkeypatch):
    monkeypatch.setattr('api.analyses.make_db', lambda *args: pytest.fail('模型检测不能访问论文数据库'))
    task = start(client, mode=mode)
    assert task['status'] == 'succeeded' and task['type'] == 'model_diagnostic'
    assert task['http_backend'] == 'requests' and task['model_name'] == 'test-model'
    assert task['model_check_result']['structure_validated'] is (mode == 'structured')
    if mode == 'structured':
        assert task['model_check_result']['quotes'] == 1
    assert model['settings'] == [{'read_timeout': 60, 'retries': 0, 'trust_env': True, 'http_backend': 'requests'}]
    latest = client.get('/api/v1/system/model-check', params={'mode': mode}).json()
    assert latest['id'] == task['id']
    assert client.get('/api/v1/tasks').json()['total'] == 1
    assert 'private-model-key' not in json.dumps(task)


def test_backend_override_does_not_rewrite_current_configuration(client, model):
    task = start(client, mode='chat', http_backend='sdk', timeout=15)
    assert task['http_backend'] == 'sdk' and model['settings'][0]['http_backend'] == 'sdk'
    assert os.environ['LLM_HTTP_BACKEND'] == 'requests'
    assert client.get('/api/v1/system/model-check', params={'mode': 'chat'}).json() is None
    assert client.get('/api/v1/system/model-check', params={'mode': 'chat', 'http_backend': 'sdk'}).json()['id'] == task['id']


def test_old_success_hidden_when_model_or_key_changes(client, model, monkeypatch):
    start(client)
    monkeypatch.setenv('LLM_API_KEY', 'new-private-key')
    assert client.get('/api/v1/system/model-check').json() is None
    monkeypatch.setenv('LLM_API_KEY', 'private-model-key')
    monkeypatch.setenv('LLM_MODEL', 'another-model')
    assert client.get('/api/v1/system/model-check').json() is None


def test_active_duplicates_reused_and_restart_reported(client, model, monkeypatch):
    monkeypatch.setattr(model_checks, 'run_model_check', lambda *args, **kwargs: None)
    first = client.post('/api/v1/system/model-check', json={}).json()
    repeated = client.post('/api/v1/system/model-check', json={}).json()
    assert repeated == {'task_id': first['task_id'], 'reused': True}
    tasks.recover_interrupted_tasks()
    latest = client.get('/api/v1/system/model-check').json()
    assert latest['status'] == 'failed' and latest['error']['code'] == 'SERVICE_RESTARTED'
    assert model['calls'] == []


def test_reset_stage_is_safe_and_visible_in_task(client, model, caplog):
    exc = APIConnectionError(request=httpx.Request('POST', 'https://user:private-model-key@model.example.org/private?key=private-model-key'))
    exc.__cause__ = httpx.ConnectError('private-model-key')
    exc.__cause__.__cause__ = ConnectionResetError(54, 'private-model-key')
    exc.model_network_trace = {'backend': 'requests', 'stage': 'requests.send'}
    model['exception'] = exc
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'MODEL_CONNECTION_ERROR'
    diagnostic = task['error']['diagnostic']
    assert diagnostic['phase'] == 'connect' and diagnostic['network']['stage'] == 'requests.send'
    assert 'private-model-key' not in json.dumps(task) + caplog.text


def test_read_timeout_retains_check_timeout(client, model):
    exc = APITimeoutError(request=httpx.Request('POST', 'https://model.example.org'))
    exc.__cause__ = httpx.ReadTimeout('private-model-key')
    model['exception'] = exc
    task = start(client, timeout=15)
    assert task['error']['code'] == 'MODEL_TIMEOUT'
    assert task['error']['diagnostic']['phase'] == 'read'
    assert task['error']['diagnostic']['read_timeout'] == 15


@pytest.mark.parametrize('mode,code', [('bad_quote', 'EVIDENCE_VALIDATION_FAILED'), ('bad_structure', 'STRUCTURED_ANALYSIS_FAILED')])
def test_invalid_structured_output_cannot_be_reported_as_success(client, model, mode, code):
    model['mode'] = mode
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == code
    assert task['model_check_result'].get('evidence_validated') is not True


def test_http_error_does_not_expose_upstream_body(client, model, caplog):
    response = httpx.Response(429, request=httpx.Request('POST', 'https://model.example.org'))
    model['exception'] = APIStatusError('private-model-key', response=response, body={'secret': 'private-model-key'})
    task = start(client)
    assert task['error']['code'] == 'MODEL_HTTP_ERROR' and task['error']['diagnostic']['http_status'] == 429
    assert 'private-model-key' not in json.dumps(task) + caplog.text


@pytest.mark.parametrize('body', [{'mode': 'unknown'}, {'http_backend': 'other'}, {'timeout': 0}, {'timeout': 121},
                                  {'base_url': 'https://untrusted.example.org'}, {'api_key': 'browser-key'}])
def test_invalid_or_credential_override_payloads_rejected(client, model, body):
    assert client.post('/api/v1/system/model-check', json=body).status_code == 422
    assert model['calls'] == []


def test_missing_configuration_does_not_create_check_task(client, model, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY')
    assert client.post('/api/v1/system/model-check', json={}).status_code == 422
    assert client.get('/api/v1/tasks').json()['total'] == 0 and model['calls'] == []


def test_health_returns_backend_without_sending_model_request(client, model, monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda *args, **kwargs: False)
    main = importlib.import_module('api.main')
    monkeypatch.setenv('DATABASE_URL', 'unavailable')
    monkeypatch.setattr(main, 'make_db', lambda *args: (_ for _ in ()).throw(ValueError('unavailable')))
    health = main.system_health()
    assert health['model_http_backend'] == 'requests' and health['model_configured'] is True
    assert health['database'] is False and model['calls'] == []
    monkeypatch.setenv('LLM_HTTP_BACKEND', 'invalid')
    invalid = main.system_health()
    assert invalid['model_configured'] is False
    assert 'LLM_HTTP_BACKEND' in invalid['model_configuration_error']


def test_codex_diagnostic_uses_cli_without_api_key_or_paper_database(client, cli, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    monkeypatch.setattr('api.analyses.make_db', lambda *a: pytest.fail('检测不访问论文数据库'))
    task = start(client)
    assert task['status'] == 'succeeded' and task['http_backend'] == 'codex'
    assert task['model_name'] == 'Codex（CLI 默认模型）'
    assert task['model_check_result']['quotes'] == 1
    assert client.get('/api/v1/system/model-check').json()['id'] == task['id']
    assert client.post('/api/v1/system/model-check', json={'http_backend': 'sdk'}).status_code == 422
    monkeypatch.setenv('CODEX_MODEL', 'other-codex-model')
    assert client.get('/api/v1/system/model-check').json() is None


def test_codex_quota_error_is_persisted_without_secrets(client, cli, monkeypatch, caplog):
    monkeypatch.setenv('FAKE_CODEX_MODE', 'quota')
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'MODEL_QUOTA_EXCEEDED'
    assert task['error']['retryable'] is False
    assert task['error']['diagnostic']['network']['backend'] == 'codex'
    assert 'private-upstream-secret' not in json.dumps(task) + caplog.text


def test_codex_health_checks_configuration_without_invoking_cli(client, cli, monkeypatch):
    monkeypatch.setattr('dotenv.load_dotenv', lambda *a, **kw: False)
    main = importlib.import_module('api.main')
    monkeypatch.setenv('DATABASE_URL', 'unavailable')
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    monkeypatch.setattr(main, 'make_db', lambda *a: (_ for _ in ()).throw(ValueError('unavailable')))
    assert main.system_health()['model_configured'] is True
    assert main.system_health()['model_http_backend'] == 'codex'
    assert not cli.exists()
