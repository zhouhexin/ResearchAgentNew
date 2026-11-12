"""验证分批保存、仅重跑失败批次、恢复和合并。模型响应受控。"""
from contextlib import contextmanager
import hashlib
import json

import httpx
from openai import APITimeoutError
import pytest

from api import analyses, documents, tasks
from storage.analysis_batches import AnalysisBatchStore
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_api_analyses import client as analysis_client


@pytest.fixture
def analysis_model(monkeypatch):
    state = {'calls': [], 'fail_ids': set(), 'same_statement': False, 'factory': []}
    class Model:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            evidence = json.loads(messages[1][1])['evidence']
            ids = [item['evidence_id'] for item in evidence]
            state['calls'].append(ids)
            if state['fail_ids'].intersection(ids):
                raise APITimeoutError(request=httpx.Request('POST', 'https://model.example.org'))
            return {'research_questions': [{'statement': '共同问题' if state['same_statement'] else '问题：' + item['evidence_id'],
                                            'evidence': [{'evidence_id': item['evidence_id'], 'quote': item['text']}]} for item in evidence],
                    'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}
    @contextmanager
    def factory(**kwargs):
        state['factory'].append(kwargs)
        yield Model()
    monkeypatch.setattr(analyses, 'analysis_llm', factory)
    return state


@pytest.fixture
def client(analysis_client, monkeypatch):
    monkeypatch.setenv('ANALYSIS_BATCH_RETRIES', '1')
    monkeypatch.setenv('ANALYSIS_BATCH_MAX_CHARS', '4000')
    return analysis_client


def start(client, **extra):
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'scope': 'full', 'batch_size': 1, **extra})
    assert response.status_code == 202, response.text
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


@pytest.mark.parametrize('correct_on_retry', [True, False])
def test_structure_failure_retries_with_field_errors_and_never_saves_invalid_result(tmp_path, correct_on_retry):
    from agent.batch_analysis import analyze_batches, BatchAnalysisFailed
    from test.test_paper_analysis import make_context, make_analysis

    context = make_context()
    store = AnalysisBatchStore(tmp_path / 'batches.sqlite3')
    store.prepare('run', 'fingerprint', [context])
    calls = []

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            calls.append(list(messages))
            if len(calls) == 2:
                feedback = json.loads(json.loads(messages[-1][1])['validation_error'])
                assert feedback['validation_errors'][0]['loc'] == ['experiments', 0, 'evidence', 0, 'quote']
                assert feedback['validation_errors'][0]['type'] == 'missing'
                if correct_on_retry:
                    return make_analysis()
            result = make_analysis().model_dump()
            del result['experiments'][0]['evidence'][0]['quote']
            return result

    @contextmanager
    def factory(**kwargs):
        yield Model()

    def run():
        return analyze_batches(context, [context], run_key='run', store=store, llm_factory=factory,
                               read_timeout=10, method='function_calling', batch_retries=1)

    if correct_on_retry:
        analysis, summary = run()
        assert analysis == make_analysis() and summary['completed_batches'] == 1
        assert store.get_completed('run', 1) is not None
    else:
        with pytest.raises(BatchAnalysisFailed):
            run()
        assert store.get_completed('run', 1) is None
        assert store.steps('run')[0]['status'] == 'failed'
    assert len(calls) == 2


@pytest.mark.parametrize('recovers', [True, False])
def test_connection_reset_backs_off_with_fresh_clients_and_bounded_attempts(tmp_path, monkeypatch, recovers):
    from openai import APIConnectionError
    from agent.batch_analysis import analyze_batches, BatchAnalysisFailed
    from test.test_paper_analysis import make_context, make_analysis

    store = AnalysisBatchStore(tmp_path / 'batches.sqlite3')
    context = make_context()
    store.prepare('run', 'fingerprint', [context])
    delays, opened, closed, events = [], [], [], []
    monkeypatch.setattr('agent.batch_analysis.sleep', delays.append)

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            if len(opened) < 4 or not recovers:
                exc = APIConnectionError(request=httpx.Request('POST', 'https://model.example.org'))
                exc.__cause__ = httpx.ConnectError('connection reset')
                exc.__cause__.__cause__ = ConnectionResetError(54, 'connection reset')
                raise exc
            return make_analysis()

    @contextmanager
    def factory(**kwargs):
        assert kwargs['retries'] == 0
        model = Model()
        opened.append(model)
        try:
            yield model
        finally:
            closed.append(model)

    def run():
        return analyze_batches(context, [context], run_key='run', store=store, llm_factory=factory,
                               read_timeout=10, method='function_calling', batch_retries=3,
                               progress=lambda **event: events.append(event))

    if recovers:
        assert run()[0] == make_analysis()
        assert store.get_completed('run', 1) is not None
        # 恢复任务直接复用核验过的批次，不再建连。
        assert run()[1]['resumed_batches'] == 1
    else:
        with pytest.raises(BatchAnalysisFailed):
            run()
        assert store.get_completed('run', 1) is None
    assert len(opened) == 4 and opened == closed
    assert delays == [2, 4, 8]
    assert [event['retry_delay_seconds'] for event in events if event['state'] == 'retrying'] == delays
    assert store.steps('run')[0]['attempts'] == 4


@pytest.mark.parametrize('fresh', [False, True])
def test_second_batch_failure_keeps_first_and_resume_only_failed(client, analysis_model, fresh):
    analysis_model['fail_ids'] = {'E2'}
    failed = start(client, force_restart=fresh)
    assert failed['status'] == 'failed' and failed['error']['code'] == 'MODEL_TIMEOUT'
    assert failed['completed_batches'] == 1 and failed['failed_batch'] == 2
    assert analysis_model['calls'] == [['E1'], ['E2'], ['E2']]
    assert all(settings['retries'] == 0 for settings in analysis_model['factory'])
    steps = client.get(f'/api/v1/analyses/tasks/{failed["id"]}/batches').json()['batches']
    assert [step['status'] for step in steps] == ['completed', 'failed']
    assert [step['attempts'] for step in steps] == [1, 2]
    assert client.get('/api/v1/analyses?paper_id=1').json() == []
    # 使用新数据库连接读取，模拟刷新页面后的恢复。
    latest = client.get('/api/v1/analyses/progress/1').json()
    assert latest['id'] == failed['id']
    analysis_model['fail_ids'] = set()
    succeeded = start(client, resume_task_id=latest['id'])
    assert succeeded['status'] == 'succeeded'
    assert analysis_model['calls'] == [['E1'], ['E2'], ['E2'], ['E2']]
    result = client.get('/api/v1/analyses/' + succeeded['result_id']).json()
    assert result['batch_summary']['resumed_batches'] == 1
    assert result['batch_summary']['completed_batches'] == 2
    assert len(result['analysis']['research_questions']) == 2
    assert set(result['selected_evidence_ids']) == {'E1', 'E2'}


def test_merge_combines_identical_claim_and_keeps_both_sources(client, analysis_model):
    analysis_model['same_statement'] = True
    task = start(client)
    result = client.get('/api/v1/analyses/' + task['result_id']).json()
    findings = result['analysis']['research_questions']
    assert len(findings) == 1
    assert {e['evidence_id'] for e in findings[0]['evidence']} == {'E1', 'E2'}
    assert len(analysis_model['calls']) == 2  # 合并不再请求一次模型。


def add_more_chunks():
    _, Session = documents.make_db('sqlite://')
    content = '第一段第二段第三段第四段第五段'
    with Session.begin() as session:
        document = session.get(PaperDocumentRecord, 10)
        document.content = content
        document.content_sha256 = hashlib.sha256(content.encode()).hexdigest()
        document.page_count = 5
        for index in range(2, 5):
            session.add(DocumentChunkRecord(document_id=10, chunk_index=index, text=content[index*3:index*3+3], start_char=index*3, end_char=index*3+3, page_number=index+1))


@pytest.mark.parametrize('scope,selected,count', [('full', 5, 3), ('sample', 2, 1)])
def test_full_scope_reads_every_chunk_and_sample_is_explicit(client, analysis_model, scope, selected, count):
    add_more_chunks()
    task = start(client, scope=scope, max_chunks=2, batch_size=2)
    assert task['status'] == 'succeeded'
    result = client.get('/api/v1/analyses/' + task['result_id']).json()
    assert result['document']['selected_chunks'] == selected
    assert result['document']['total_chunks'] == 5
    assert result['batch_summary']['scope'] == scope
    assert len(analysis_model['calls']) == count
    assert sum(len(call) for call in analysis_model['calls']) == selected


def test_new_model_or_original_cannot_mix_old_checkpoint(client, analysis_model, monkeypatch):
    analysis_model['fail_ids'] = {'E2'}
    failed = start(client)
    monkeypatch.setenv('LLM_MODEL', 'different-model')
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'batch_size': 1, 'resume_task_id': failed['id']})
    assert response.status_code == 409
    analysis_model['fail_ids'] = set()
    before = len(analysis_model['calls'])
    completed = start(client)
    assert completed['status'] == 'succeeded'
    assert analysis_model['calls'][before:] == [['E1'], ['E2']]


def test_restart_keeps_completed_batches(client, analysis_model):
    analysis_model['fail_ids'] = {'E2'}
    failed = start(client)
    tasks.update_task(failed['id'], status='running', error=None)
    tasks.recover_interrupted_tasks()
    assert client.get('/api/v1/analyses/progress/1').json()['error']['code'] == 'SERVICE_RESTARTED'
    assert AnalysisBatchStore(tasks.TASK_DB_PATH).steps(failed['batch_run_key'])[0]['status'] == 'completed'
    analysis_model['fail_ids'] = set()
    succeeded = start(client, resume_task_id=failed['id'])
    assert succeeded['status'] == 'succeeded'
    assert succeeded['resumed_batches'] == 1


def test_force_restart_really_calls_model_again(client, analysis_model):
    first = start(client)
    second = start(client, force_restart=True)
    assert first['batch_run_key'] != second['batch_run_key']
    assert analysis_model['calls'] == [['E1'], ['E2'], ['E1'], ['E2']]


def test_saved_batch_is_revalidated_before_reuse(client, analysis_model):
    first = start(client)
    store = AnalysisBatchStore(tasks.TASK_DB_PATH)
    payload = store.get_completed(first['batch_run_key'], 1)
    payload['research_questions'][0]['evidence'][0]['quote'] = 'not in the original'
    with store.connection() as db:
        db.execute('UPDATE analysis_batches SET payload=? WHERE run_key=? AND batch_index=1', (json.dumps(payload), first['batch_run_key']))
    before = len(analysis_model['calls'])
    second = start(client)
    assert second['status'] == 'succeeded'
    assert analysis_model['calls'][before:] == [['E1']]


def test_too_small_batch_budget_rejects_without_skipping_text(client, analysis_model, monkeypatch):
    monkeypatch.setenv('ANALYSIS_BATCH_MAX_CHARS', '2')
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'scope': 'full'})
    assert response.status_code == 422
    assert '不会跳过' in response.json()['detail']
    assert analysis_model['calls'] == []
