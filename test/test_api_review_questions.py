"""推荐问题 → 用户选改 → 综述，固定分析版本并核验跨论文证据。"""
from contextlib import contextmanager
from copy import deepcopy
import json

from langchain_core.messages import AIMessage
import pytest
from sqlalchemy import delete

from api import documents, reviews, tasks
from api.analysis_results import complete_analysis, load_result
from storage.db_models import PaperDocumentRecord, DocumentChunkRecord
from test.test_api_reviews import client
from test.test_api_analyses import client as analysis_client, model as analysis_model
from test.test_api_documents import client as database_client
from test.test_codex_model import cli


@pytest.fixture
def model(monkeypatch):
    state = {'calls': [], 'mode': 'valid', 'hook': None}
    class Model:
        def with_structured_output(self, schema, **kwargs):
            state['schema'] = schema
            return self
        def invoke(self, messages):
            payload = json.loads(messages[1][1])
            state['calls'].append(payload)
            if state.get('exception'):
                raise state['exception']
            if state['hook']:
                state['hook']()
            refs = [finding['evidence'][0]['ref'] for paper in payload['papers']
                    for findings in paper['findings'].values() for finding in findings]
            if state['schema'].__name__ == 'ReviewDraft':
                return {'title': '用户指定的问题', 'background': [], 'common_methods': [],
                        'comparisons': [{'claim': '比较所选论文', 'evidence_refs': refs}],
                        'limitations': [], 'tentative_directions': []}
            values = [{'question': question, 'rationale': '两篇论文均有带证据的相关分析，可比较所述维度。',
                       'evidence_refs': list(refs)} for question in ('两篇论文的方法有哪些差异？', '实验设计有哪些不同？', '各自有哪些适用条件与局限？')]
            mode = state['mode']
            if mode == 'empty':
                values = []
            elif mode == 'bad_ref':
                values[0]['evidence_refs'][-1] = 'P999:E999'
            elif mode == 'one_paper':
                values[0]['evidence_refs'] = [refs[0], refs[0]]
            elif mode == 'duplicate':
                values[1]['question'] = values[0]['question']
            elif mode == 'blank':
                values[0]['question'] = '  '
            elif mode == 'too_many':
                values.append(values[0])
            elif mode == 'bad_structure':
                return {'raw': None, 'parsed': None}
            result = {'questions': values}
            if mode == 'truncated':
                return {'raw': AIMessage(content='', response_metadata={'finish_reason': 'length'}), 'parsed': result}
            if mode == 'raw_json':
                return {'raw': AIMessage(content='```json\n' + json.dumps(result) + '\n```'), 'parsed': None}
            return result
    @contextmanager
    def factory(**kwargs):
        yield Model()
    monkeypatch.setattr(reviews, 'analysis_llm', factory)
    return state


def selection(client):
    return client.post('/api/v1/reviews/compare', json={'paper_ids': [1, 3]}).json()


def recommend(client, chosen=None):
    chosen = chosen or selection(client)
    response = client.post('/api/v1/reviews/questions', json={key: chosen[key] for key in ('paper_ids', 'analysis_ids')})
    assert response.status_code == 202, response.text
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


def test_questions_are_pinned_persisted_and_selected_text_can_be_edited(client, model):
    chosen = selection(client)
    result = recommend(client, chosen)
    assert result['type'] == 'review_questions' and result['status'] == 'succeeded'
    assert result['analysis_ids'] == chosen['analysis_ids'] and result['paper_ids'] == [1, 3]
    questions = result['question_suggestions']
    assert len(questions) == 3
    assert all({ref.split(':')[0] for ref in question['evidence_refs']} == {'P1', 'P3'} for question in questions)
    assert '/private/' not in json.dumps(result) and 'private-test-key' not in json.dumps(result)
    # 推荐任务不创建正式报告；用户选中后可修改，再走原有综述入口。
    assert result['result_id'] is None and len(model['calls']) == 1
    topic = questions[1]['question'] + '重点关注评价指标。'
    response = client.post('/api/v1/reviews', json={'paper_ids': [1, 3], 'analysis_ids': chosen['analysis_ids'], 'topic': topic})
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    report = client.get('/api/v1/reviews/' + task['result_id']).json()
    assert report['topic'] == topic and report['analysis_ids'] == chosen['analysis_ids']
    assert model['calls'][-1]['review_focus'] == topic


@pytest.mark.parametrize('mode,code', [('bad_ref', 'QUESTION_VALIDATION_FAILED'), ('one_paper', 'QUESTION_VALIDATION_FAILED'),
                                     ('duplicate', 'QUESTION_VALIDATION_FAILED'), ('blank', 'QUESTION_VALIDATION_FAILED'),
                                     ('too_many', 'QUESTION_STRUCTURE_FAILED'), ('bad_structure', 'QUESTION_STRUCTURE_FAILED'),
                                     ('truncated', 'QUESTION_STRUCTURE_FAILED')])
def test_invalid_questions_never_published(client, model, mode, code):
    model['mode'] = mode
    task = recommend(client)
    assert task['status'] == 'failed' and task['error']['code'] == code
    assert 'question_suggestions' not in task


@pytest.mark.parametrize('mode', ['empty', 'raw_json'])
def test_empty_or_complete_json_results_are_handled(client, model, mode):
    model['mode'] = mode
    result = recommend(client)
    assert result['status'] == 'succeeded'
    assert len(result['question_suggestions']) == (0 if mode == 'empty' else 3)


@pytest.mark.parametrize('body,status', [({'paper_ids': [1]}, 422), ({'paper_ids': [1, 1]}, 422),
                                       ({'paper_ids': [1, 2]}, 422), ({'paper_ids': [1, 999]}, 404)])
def test_selection_rejected_before_model_request(client, model, body, status):
    assert client.post('/api/v1/reviews/questions', json=body).status_code == status
    assert model['calls'] == []


def test_deduplicate_active_tasks_and_recover_after_service_restart(client, model, monkeypatch):
    monkeypatch.setattr(reviews, 'run_question_suggestions', lambda *a, **kw: None)
    chosen = selection(client)
    body = {key: chosen[key] for key in ('paper_ids', 'analysis_ids')}
    first = client.post('/api/v1/reviews/questions', json=body).json()
    second = client.post('/api/v1/reviews/questions', json={key: list(reversed(value)) for key, value in body.items()}).json()
    assert second == {'task_id': first['task_id'], 'reused': True}
    tasks.recover_interrupted_tasks()
    task = client.get('/api/v1/tasks/' + first['task_id']).json()
    assert task['error']['code'] == 'SERVICE_RESTARTED'
    assert model['calls'] == []


@pytest.mark.parametrize('change', ['hash', 'chunks'])
def test_source_changes_during_recommendation_prevent_publish(client, model, change):
    _, Session = documents.make_db('sqlite://')
    def hook():
        with Session.begin() as session:
            if change == 'hash':
                session.get(PaperDocumentRecord, 10).content_sha256 = 'changed'
            else:
                session.execute(delete(DocumentChunkRecord).where(DocumentChunkRecord.document_id == 10))
    model['hook'] = hook
    task = recommend(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'REVIEW_INPUTS_CHANGED'
    assert 'question_suggestions' not in task


def test_new_analysis_does_not_replace_preview_versions(client, model):
    chosen = selection(client)
    snapshot = deepcopy(load_result(chosen['analysis_ids'][0]))
    snapshot['analysis']['research_questions'][0]['statement'] = '不应混入推荐的新分析'
    pending = tasks.create_task(1, task_type='paper_analysis')
    complete_analysis(pending['id'], snapshot)
    result = recommend(client, chosen)
    assert result['analysis_ids'] == chosen['analysis_ids']
    assert model['calls'][0]['papers'][0]['findings']['research_questions'][0]['statement'] != '不应混入推荐的新分析'


def test_codex_quota_error_remains_safe_and_distinct(client, model, caplog):
    from agent.codex_model import CodexExecutionError
    model['exception'] = CodexExecutionError('MODEL_QUOTA_EXCEEDED', 'Codex 账号额度不足', reason='quota')
    task = recommend(client)
    assert task['error']['code'] == 'MODEL_QUOTA_EXCEEDED' and task['error']['retryable'] is False
    assert 'question_suggestions' not in task


def test_codex_cli_recommends_using_same_account_adapter(client, cli, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    result = recommend(client)
    assert result['status'] == 'succeeded' and result['http_backend'] == 'codex'
    assert len(result['question_suggestions']) == 1
    assert {ref.split(':')[0] for ref in result['question_suggestions'][0]['evidence_refs']} == {'P1', 'P3'}
