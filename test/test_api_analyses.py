"""复用真实分析/证据核验流程，模型回复可控，数据库隔离。"""
from contextlib import contextmanager
import json

import pytest
from sqlalchemy import delete

from agent import paper_context
from api import analyses, documents, tasks, uploads
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_codex_model import cli


@pytest.fixture
def client(database_client, monkeypatch, tmp_path):
    monkeypatch.setattr('agent.batch_analysis.sleep', lambda seconds: None)
    factory = documents.make_db
    monkeypatch.setattr(analyses, 'make_db', factory)
    monkeypatch.setattr(paper_context, 'make_db', factory)
    monkeypatch.setattr(uploads, 'make_db', factory)
    monkeypatch.setattr(analyses, 'ERROR_DIR', tmp_path / 'errors')
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    monkeypatch.setenv('LLM_API_KEY', 'private-test-key')
    monkeypatch.setenv('LLM_READ_TIMEOUT', '400')
    monkeypatch.setenv('LLM_MAX_RETRIES', '0')
    monkeypatch.setenv('ANALYSIS_MAX_CHARS', '9000')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')
    database_client.app.include_router(analyses.router)
    database_client.app.include_router(tasks.router)
    return database_client


@pytest.fixture
def model(monkeypatch):
    state = {'calls': 0, 'mode': 'valid', 'hook': None}
    class FakeModel:
        def with_structured_output(self, schema, **kwargs):
            return self
        def invoke(self, messages):
            state['calls'] += 1
            if state.get('exception'):
                raise state['exception']
            if state['hook']:
                state['hook']()
            if state['mode'] == 'empty':
                return {'raw': None, 'parsed': None, 'parsing_error': None}
            evidence = json.loads(messages[1][1])['evidence'][0]
            quote = 'This quote is not in the paper' if state['mode'] == 'bad_quote' else evidence['text']
            evidence_id = 'E99999' if state['mode'] == 'foreign_evidence' else evidence['evidence_id']
            result = {'research_questions': [{'statement': '根据原文提出研究问题', 'evidence': [{'evidence_id': evidence_id, 'quote': quote}]}],
                    'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}
            if state['mode'] == 'missing_quote':
                del result['research_questions'][0]['evidence'][0]['quote']
            return result
    @contextmanager
    def factory(**kwargs):
        state['settings'] = kwargs
        yield FakeModel()
    monkeypatch.setattr(analyses, 'analysis_llm', factory)
    return state


def start(client):
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'max_chunks': 6, 'force_restart': True})
    assert response.status_code == 202
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


def test_start_analysis_history_detail_and_library_flags(client, model):
    assert client.get('/api/v1/papers/1').json()['has_analysis'] is False
    task = start(client)
    assert task['type'] == 'paper_analysis'
    assert task['status'] == 'succeeded' and task['progress'] == 100
    assert task['selected_chunks'] == 2 and model['calls'] == 1
    response = client.get('/api/v1/analyses/' + task['result_id'])
    result = response.json()
    assert result['is_current'] is True
    assert result['paper']['id'] == 1 and result['document']['id'] == 10
    assert result['document']['selected_chunks'] == 2
    assert result['model_name'] == 'test-model'
    assert '/private/' not in response.text and 'private-test-key' not in response.text
    evidence_id = next(iter(result['evidence_index']))
    evidence = client.get('/api/v1/evidence/' + evidence_id).json()
    assert evidence['paper_id'] == 1
    assert result['analysis']['research_questions'][0]['evidence'][0]['quote'] == evidence['quote']
    assert client.get('/api/v1/analyses?paper_id=1').json()[0]['id'] == result['id']
    assert client.get('/api/v1/papers/1').json()['has_analysis'] is True
    assert [p['id'] for p in client.get('/api/v1/papers?has_analysis=true').json()['items']] == [1]
    assert 1 not in [p['id'] for p in client.get('/api/v1/papers?has_analysis=false').json()['items']]
    assert client.get('/api/v1/analyses/missing').status_code == 404
    assert client.get('/api/v1/analyses?paper_id=2').json() == []


def test_reanalysis_preserves_old_results_and_survives_requery(client, model):
    first = start(client)
    second = start(client)
    assert first['result_id'] != second['result_id']
    history = client.get('/api/v1/analyses?paper_id=1').json()
    assert [a['id'] for a in history] == [second['result_id'], first['result_id']]
    assert client.get('/api/v1/analyses/' + first['result_id']).status_code == 200


@pytest.mark.parametrize('mode,code', [('bad_quote', 'EVIDENCE_VALIDATION_FAILED'), ('foreign_evidence', 'EVIDENCE_VALIDATION_FAILED'), ('empty', 'STRUCTURED_ANALYSIS_FAILED')])
def test_invalid_model_output_never_published(client, model, mode, code):
    model['mode'] = mode
    task = start(client)
    assert task['status'] == 'failed' and task['result_id'] is None
    assert task['error']['code'] == code
    assert client.get('/api/v1/analyses?paper_id=1').json() == []
    assert client.get('/api/v1/papers/1').json()['has_analysis'] is False
    assert (analyses.ERROR_DIR / (task['id'] + '.json')).exists()
    if mode in {'bad_quote', 'foreign_evidence'}:
        assert model['calls'] == 2  # 仅修正一次，仍失败就不发布。


def test_codex_cli_analysis_is_verified_saved_and_labeled(client, cli, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    task = start(client)
    assert task['status'] == 'succeeded' and task['http_backend'] == 'codex'
    report = client.get('/api/v1/analyses/' + task['result_id']).json()
    assert report['model_name'] == 'Codex（CLI 默认模型）'
    assert report['analysis']['research_questions']


def test_provider_switch_cannot_resume_api_batches(client, model, cli, monkeypatch):
    monkeypatch.setenv('LLM_PROVIDER', 'openai')
    previous = start(client)
    monkeypatch.setenv('LLM_PROVIDER', 'codex')
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'resume_task_id': previous['id']})
    assert response.status_code == 409


@pytest.mark.parametrize('change', ['hash', 'chunk_ids'])
def test_changes_during_model_call_prevent_publishing(client, model, change):
    _, Session = documents.make_db('sqlite://')
    def hook():
        with Session.begin() as session:
            if change == 'hash':
                session.get(PaperDocumentRecord, 10).content_sha256 = 'changed'
            else:
                session.execute(delete(DocumentChunkRecord).where(DocumentChunkRecord.document_id == 10))
                session.add_all([
                    DocumentChunkRecord(id=101, document_id=10, chunk_index=0, text='第一段', start_char=0, end_char=3, page_number=1),
                    DocumentChunkRecord(id=102, document_id=10, chunk_index=1, text='第二段', start_char=3, end_char=6, page_number=2),
                ])
    model['hook'] = hook
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'DOCUMENT_CHANGED'
    assert client.get('/api/v1/analyses?paper_id=1').json() == []


def test_saved_analysis_marked_historical_when_original_changes(client, model):
    task = start(client)
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).content_sha256 = 'new version'
    assert client.get('/api/v1/analyses/' + task['result_id']).json()['is_current'] is False
    assert client.get('/api/v1/papers/1').json()['has_analysis'] is False
    assert client.get('/api/v1/papers?has_analysis=true').json()['items'] == []


def test_running_analysis_is_reused_instead_of_double_model_call(client, model):
    pending = tasks.create_task(1, task_type='paper_analysis', deduplicate=True)
    response = client.post('/api/v1/analyses', json={'paper_id': 1, 'max_chunks': 6})
    assert response.status_code == 202
    assert response.json() == {'task_id': pending['id'], 'reused': True}
    assert model['calls'] == 0


@pytest.mark.parametrize('payload,status', [({'paper_id': 999}, 404), ({'paper_id': 2}, 422), ({'paper_id': 1, 'max_chunks': 1}, 422), ({'paper_id': 1, 'max_chunks': 51}, 422)])
def test_preflight_validation(client, model, payload, status):
    assert client.post('/api/v1/analyses', json=payload).status_code == status
    assert model['calls'] == 0
    assert client.get('/api/v1/tasks').json()['total'] == 0


def test_model_configuration_checked_before_task_created(client, model, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY')
    response = client.post('/api/v1/analyses', json={'paper_id': 1})
    assert response.status_code == 422
    assert 'LLM_API_KEY' in response.json()['detail']
    assert client.get('/api/v1/tasks').json()['total'] == 0


def test_model_timeout_is_a_failed_task(client, model):
    import httpx
    from openai import APITimeoutError
    model['exception'] = APITimeoutError(request=httpx.Request('POST', 'https://model.example.org'))
    model['exception'].__cause__ = httpx.ReadTimeout('private-test-key')
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'MODEL_TIMEOUT'
    assert client.get('/api/v1/analyses?paper_id=1').json() == []
    diagnostic = json.loads((analyses.ERROR_DIR / f'{task["id"]}.json').read_text())
    assert diagnostic['phase'] == 'read' and diagnostic['read_timeout'] == 400
    assert '400' in task['error']['message']
    assert 'private-test-key' not in json.dumps(diagnostic)


def test_structure_failure_reports_fields_in_task_and_logs(client, model, caplog):
    model['mode'] = 'missing_quote'
    task = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'STRUCTURED_ANALYSIS_FAILED'
    assert 'research_questions.0.evidence.0.quote' in task['error']['message']
    assert 'validation_errors' in caplog.text and 'missing' in caplog.text
    assert 'diagnostic=None' not in caplog.text and 'raw_response' not in caplog.text
    diagnostic = json.loads((analyses.ERROR_DIR / f'{task["id"]}.json').read_text())
    assert diagnostic['validation_errors'][0]['type'] == 'missing'
    assert client.get('/api/v1/analyses?paper_id=1').json() == []


def test_evidence_failure_reports_all_issue_locations(client, model, caplog):
    model['mode'] = 'bad_quote'
    task = start(client)
    assert task['error']['code'] == 'EVIDENCE_VALIDATION_FAILED'
    assert '1 处' in task['error']['message'] and 'E1' in task['error']['message']
    assert 'quote_mismatch' in caplog.text and 'issue_count' in caplog.text
    assert 'diagnostic=None' not in caplog.text


def test_failed_reanalysis_keeps_previous_success(client, model):
    first = start(client)
    model['mode'] = 'bad_quote'
    failed = start(client)
    assert failed['status'] == 'failed'
    history = client.get('/api/v1/analyses?paper_id=1').json()
    assert len(history) == 1 and history[0]['id'] == first['result_id']
    assert client.get('/api/v1/papers/1').json()['has_analysis'] is True


def test_connection_failure_records_dns_cause_and_preserves_report(client, model, caplog):
    import socket
    import httpx
    from openai import APIConnectionError

    first = start(client)
    exc = APIConnectionError(request=httpx.Request('POST', 'https://model.example.org/private?key=private-test-key'))
    exc.__cause__ = socket.gaierror('private-test-key private DNS details')
    model['exception'] = exc
    failed = start(client)
    assert failed['error']['code'] == 'MODEL_CONNECTION_ERROR'
    assert '域名解析失败' in failed['error']['message']
    diagnostic = json.loads((analyses.ERROR_DIR / f'{failed["id"]}.json').read_text())
    assert diagnostic['reason'] == 'dns'
    assert 'gaierror' in diagnostic['causes']
    assert 'gaierror' in caplog.text
    assert 'private-test-key' not in json.dumps(diagnostic) + json.dumps(failed) + caplog.text
    assert client.get('/api/v1/analyses?paper_id=1').json()[0]['id'] == first['result_id']
