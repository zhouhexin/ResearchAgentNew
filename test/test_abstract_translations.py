"""原文保持、翻译缓存、结构校验、后台任务恢复与隔离。"""
from contextlib import contextmanager
import json
from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest

from agent.abstract_translation import AbstractTranslationOutput
from api import abstract_translations as translations, tasks
from test.test_codex_model import cli

BODY = {'title': 'Depth estimation', 'abstract': '  We evaluate depth estimation.\nAccuracy improves by 2.5%.\n'}
ZH = '我们评估深度估计。\n准确率提高了2.5%。'


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path/'tasks.sqlite3')
    for key, value in {'LLM_PROVIDER': 'openai', 'LLM_MODEL': 'test-model', 'LLM_API_KEY': 'private-key',
                       'LLM_MAX_RETRIES': '0', 'STRUCTURED_OUTPUT_METHOD': 'json_schema'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr('api.analyses.make_db', lambda *a: pytest.fail('摘要翻译不应修改或访问论文库'))
    app = FastAPI();app.include_router(translations.router);app.include_router(tasks.router)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def model(monkeypatch):
    state = {'calls': [], 'mode': 'valid'}
    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert schema is AbstractTranslationOutput
            state['method'] = kwargs['method']
            return self
        def invoke(self, messages):
            state['calls'].append(json.loads(messages[1][1]))
            mode = state['mode']
            value = {'translation_zh': ' ' if mode == 'blank' else 'English only' if mode == 'english' else ZH}
            if mode in {'length', 'refusal', 'raw_json', 'bad_json'}:
                return {'parsed': value if mode in {'length', 'refusal'} else None,
                        'raw': AIMessage(content='```json\n'+json.dumps(value)+'\n```' if mode == 'raw_json' else '{',
                            response_metadata={'finish_reason': 'length'} if mode == 'length' else {},
                            additional_kwargs={'refusal': 'no'} if mode == 'refusal' else {})}
            if mode == 'secret_exception':raise RuntimeError('private-upstream-secret')
            return value
    @contextmanager
    def factory(**kwargs):
        state['settings'] = kwargs
        yield Model()
    monkeypatch.setattr(translations, 'analysis_llm', factory)
    return state


URL = '/api/v1/translations/abstract'


def start(client, body=None):
    response = client.post(URL, json=body or BODY)
    assert response.status_code == 202, response.text
    result = response.json()
    return result, client.get(URL+'/'+result['translation_id']).json()


@pytest.mark.parametrize('method', ['json_schema', 'function_calling'])
def test_translate_keeps_exact_original_and_reuses_cache(client, model, monkeypatch, method):
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', method)
    assert client.post(URL+'/lookup', json=BODY).json() is None
    response, result = start(client)
    assert result['record']['original_abstract'] == BODY['abstract']
    assert result['record']['translation_zh'] == ZH and result['task']['status'] == 'succeeded'
    assert result['record']['model_name'] == 'test-model' and result['task']['type'] == 'abstract_translation'
    assert model['calls'] == [BODY] and model['method'] == method
    assert model['settings']['max_output_tokens'] == 8000
    assert 'private-key' not in json.dumps(result)
    assert client.post(URL+'/lookup', json=BODY).json()['record']['translation_zh'] == ZH
    second, _ = start(client)
    assert second == {**response, 'reused': True} and len(model['calls']) == 1


def test_changed_abstract_or_model_never_uses_stale_translation(client, model, monkeypatch):
    first, _ = start(client)
    changed = {**BODY, 'abstract': BODY['abstract']+' Additional findings.'}
    assert client.post(URL+'/lookup', json=changed).json() is None
    second, result = start(client, changed)
    assert first['translation_id'] != second['translation_id'] and result['record']['original_abstract'] == changed['abstract']
    monkeypatch.setenv('LLM_MODEL', 'another-model')
    assert client.post(URL+'/lookup', json=BODY).json() is None
    third, _ = start(client)
    assert third['translation_id'] != first['translation_id'] and len(model['calls']) == 3
    assert client.get(URL+'/'+first['translation_id']).json()['record']['translation_zh'] == ZH


@pytest.mark.parametrize('mode', ['length', 'refusal', 'blank', 'english', 'bad_json', 'secret_exception'])
def test_failure_preserves_original_and_retryable(client, model, mode):
    model['mode'] = mode
    first, result = start(client)
    assert result['task']['status'] == 'failed' and result['record']['translation_zh'] is None
    assert result['record']['original_abstract'] == BODY['abstract']
    assert 'private-upstream-secret' not in json.dumps(result)
    model['mode'] = 'valid'
    second, result = start(client)
    assert first['translation_id'] == second['translation_id'] and first['task_id'] != second['task_id']
    assert result['task']['status'] == 'succeeded' and result['record']['translation_zh'] == ZH


def test_raw_json_fallback(client, model):
    model['mode'] = 'raw_json'
    assert start(client)[1]['record']['translation_zh'] == ZH


@pytest.mark.parametrize('body', [{'abstract': ''}, {'abstract': '   '}, {'abstract': 'x'*24001},
    {'abstract': 'Real abstract', 'api_key': 'browser-secret'}])
def test_invalid_requests_dont_call_model(client, model, body):
    assert client.post(URL, json=body).status_code == 422
    assert client.post(URL+'/lookup', json=body).status_code == 422
    assert model['calls'] == []


def test_active_dedup_restart_and_stale_worker_guard(client, model, monkeypatch):
    runner = translations.run_translation
    monkeypatch.setattr(translations, 'run_translation', lambda *a: None)
    first, result = start(client)
    assert result['task']['status'] == 'queued'
    assert start(client)[0] == {**first, 'reused': True} and model['calls'] == []
    tasks.recover_interrupted_tasks()
    assert client.get(URL+'/'+first['translation_id']).json()['task']['error']['code'] == 'SERVICE_RESTARTED'
    monkeypatch.setattr(translations, 'run_translation', runner)
    second, result = start(client)
    assert result['task']['status'] == 'succeeded'
    with pytest.raises(RuntimeError):
        translations.complete_translation(first['translation_id'], first['task_id'], '陈旧响应')
    assert client.get(URL+'/'+second['translation_id']).json()['record']['translation_zh'] == ZH


def test_missing_record(client, model):
    assert client.get(URL+'/missing').status_code == 404 and model['calls'] == []


def test_codex_uses_same_translation_schema(client, cli, monkeypatch):
    from agent.llm_runtime import analysis_llm
    monkeypatch.setattr(translations, 'analysis_llm', analysis_llm)
    _, result = start(client)
    assert result['task']['status'] == 'succeeded', result
    assert result['task']['http_backend'] == 'codex'
    assert result['record']['original_abstract'] == BODY['abstract']
    assert json.loads(cli.read_text())['schema']['properties']['translation_zh']
    assert 'private-key' not in cli.read_text()
