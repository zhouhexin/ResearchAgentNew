"""网页分析 P0 第一条业务链路：断言实际 HTTP 请求，隔离持久层。"""
from importlib import import_module
import json

from openai import DefaultHttpxClient
import pytest
from sqlalchemy import delete

from api import analyses, documents
from storage.runtime_versions import RuntimeSnapshotRecord
from test.test_api_analyses import client
from test.test_api_documents import client as database_client


@pytest.fixture
def pinned_api(client, monkeypatch, tmp_path):
    import agent.runtime_snapshot as runtime
    monkeypatch.setattr(runtime, 'IDENTITY_KEY', tmp_path / 'identity.key')
    monkeypatch.setenv('ANALYSIS_RUNTIME_PINNING', 'true')
    monkeypatch.setenv('ANALYSIS_BATCH_MAX_CHARS', '4000')
    monkeypatch.setenv('LLM_BASE_URL', 'https://a.example/v1')
    monkeypatch.setenv('LLM_HTTP_BACKEND', 'sdk')
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '321')
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'false')
    for key in runtime.NETWORK_KEYS:
        monkeypatch.delenv(key, raising=False)
    with DefaultHttpxClient() as probe:
        name = next(t.__module__.split('.')[0] for t in type(probe).__mro__
                    if t.__module__.split('.')[0] in {'httpx', 'httpx2'})
    http = import_module(name)
    state = {'requests': [], 'fail_on': set(), 'hook': None}

    def handler(request):
        state['requests'].append(request)
        index = len(state['requests'])
        if state['hook']:
            state['hook'](index)
        if index in state['fail_on']:
            return http.Response(503, json={'error': {'message': 'controlled failure'}})
        body = json.loads(request.content)
        evidence = json.loads(body['messages'][1]['content'])['evidence'][0]
        result = {'research_questions': [{'statement': '原文研究问题', 'evidence': [
            {'evidence_id': evidence['evidence_id'], 'quote': evidence['text']}]}],
                  'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}
        return http.Response(200, json={'id': 'test', 'object': 'chat.completion', 'created': 0,
            'model': body['model'], 'choices': [{'index': 0, 'message': {
                'role': 'assistant', 'content': json.dumps(result)}, 'finish_reason': 'stop'}]})

    monkeypatch.setattr('agent.llm_runtime.DefaultHttpxClient', lambda **kwargs:
                        DefaultHttpxClient(transport=http.MockTransport(handler), **kwargs))
    return client, state


def launch(client, **kwargs):
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'batch_size': 1, **kwargs})
    assert response.status_code == 202, response.text
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


def change_defaults(monkeypatch):
    monkeypatch.setenv('LLM_MODEL', 'new-model')
    monkeypatch.setenv('LLM_BASE_URL', 'https://b.example/v1')
    monkeypatch.setenv('LLM_READ_TIMEOUT', '19')
    monkeypatch.setenv('LLM_MAX_OUTPUT_TOKENS', '999')
    monkeypatch.setenv('LLM_ENABLE_THINKING', 'true')
    monkeypatch.setenv('ANALYSIS_BATCH_RETRIES', '0')


def test_inflight_batches_and_retry_pin_actual_request_and_link_result(pinned_api, monkeypatch):
    client, state = pinned_api
    state['fail_on'] = {1}
    state['hook'] = lambda index: change_defaults(monkeypatch) if index == 1 else None
    first = launch(client)
    assert first['status'] == 'succeeded'
    assert first['runtime_snapshot_id'].startswith('runtime_')
    assert first['agent_version_id'].startswith('version_')
    assert len(state['requests']) == 3  # 第一批失败重试，第二批继续使用同一快照。
    for request in state['requests']:
        body = json.loads(request.content)
        assert body['model'] == 'test-model' and request.url.host == 'a.example'
        assert body['max_completion_tokens'] == 321 and body['enable_thinking'] is False
        assert request.extensions['timeout']['read'] == 400
    result = client.get('/api/v1/analyses/' + first['result_id']).json()
    assert result['runtime_snapshot_id'] == first['runtime_snapshot_id']
    assert result['agent_version_id'] == first['agent_version_id']
    assert result['source_task_id'] == first['id']
    assert result['model_name'] == 'test-model'
    assert first['cost'] is None and first['token_usage'] is None
    state['hook'] = None
    second = launch(client)
    assert second['status'] == 'succeeded'
    assert second['runtime_snapshot_id'] != first['runtime_snapshot_id']
    assert second['batch_run_key'] != first['batch_run_key']
    assert all(json.loads(r.content)['model'] == 'new-model' and r.url.host == 'b.example'
               for r in state['requests'][3:])


def test_restart_resume_restores_old_config_with_cache_and_disabled_flag(pinned_api, monkeypatch):
    client, state = pinned_api
    state['fail_on'] = {2, 3}
    first = launch(client)
    assert first['status'] == 'failed' and first['completed_batches'] == 1
    change_defaults(monkeypatch)
    monkeypatch.setenv('ANALYSIS_RUNTIME_PINNING', 'false')
    monkeypatch.setenv('LLM_PROVIDER', 'unavailable-global-default')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', 'invalid-new-default')
    # 新后台调用从数据库按 ID 重建旧快照；恢复 body 默认值也不能改变旧分批设置。
    resumed = launch(client, resume_task_id=first['id'], batch_size=2)
    assert resumed['status'] == 'succeeded' and resumed['resumed_batches'] == 1
    assert resumed['runtime_snapshot_id'] == first['runtime_snapshot_id']
    assert resumed['agent_version_id'] == first['agent_version_id']
    assert resumed['batch_run_key'] == first['batch_run_key']
    assert resumed['resumed_from_task_id'] == first['id']
    assert len(state['requests']) == 4
    request = state['requests'][-1]
    assert json.loads(request.content)['model'] == 'test-model' and request.url.host == 'a.example'
    assert request.extensions['timeout']['read'] == 400


@pytest.mark.parametrize('unavailable', ['credential', 'snapshot', 'prompt', 'code'])
def test_resume_never_silently_falls_back(pinned_api, monkeypatch, unavailable):
    client, state = pinned_api
    state['fail_on'] = {2, 3}
    first = launch(client)
    assert first['status'] == 'failed'
    before = len(state['requests'])
    if unavailable == 'credential':
        monkeypatch.setenv('LLM_API_KEY', 'rotated-credential')
    elif unavailable == 'snapshot':
        _, Session = documents.make_db('sqlite://')
        with Session.begin() as session:
            session.execute(delete(RuntimeSnapshotRecord))
    elif unavailable == 'prompt':
        monkeypatch.setattr('agent.paper_analyzer.SYSTEM_PROMPT', 'changed prompt')
    else:
        from agent.runtime_snapshot import RuntimeUnavailable
        def changed(*args, **kwargs):
            raise RuntimeUnavailable('运行代码已改变')
        monkeypatch.setattr('agent.agent_version.assert_code_available', changed)
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'resume_task_id': first['id']})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'RUNTIME_SNAPSHOT_UNAVAILABLE'
    assert len(state['requests']) == before
    retained = client.get('/api/v1/tasks/' + first['id']).json()
    assert retained['completed_batches'] == 1


def test_runtime_change_between_batches_records_explicit_nonretryable_error(pinned_api, monkeypatch):
    client, state = pinned_api
    state['hook'] = lambda index: monkeypatch.setenv('LLM_API_KEY', 'rotated-credential')
    result = launch(client)
    assert result['status'] == 'failed'
    assert result['error']['code'] == 'RUNTIME_SNAPSHOT_UNAVAILABLE'
    assert result['error']['retryable'] is False
    assert result['result_id'] is None and len(state['requests']) == 1


def test_missing_version_tables_give_actionable_error_before_enqueue(pinned_api):
    from storage.runtime_versions import TABLES
    client, state = pinned_api
    _, Session = documents.make_db('sqlite://')
    engine = Session.kw['bind']
    for table in reversed(TABLES):
        table.drop(engine)
    response = client.post('/api/v1/analyses', json={'paper_id': 1})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'RUNTIME_STORAGE_UNAVAILABLE'
    assert 'storage.init_runtime_versions' in response.json()['detail']['message']
    assert client.get('/api/v1/tasks').json()['total'] == 0
    assert state['requests'] == []
