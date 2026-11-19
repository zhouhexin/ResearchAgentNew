"""已有文献库真实业务适配，固定输入；网络/模型与数据库使用隔离替身。"""
from copy import deepcopy
import json

import pytest

from api import analyses, research_execution, research_plans, tasks
from storage.db_models import PaperRecord, PaperDocumentRecord, DocumentChunkRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_research_planning import GOAL, proposal


@pytest.fixture
def library(workspace, monkeypatch):
    client, state, Session = workspace
    with Session.begin() as session:
        session.add(PaperRecord(id=4, title='尚未上传全文的论文'))
    plan = proposal()
    plan['steps'] = [dict(number=i, action=action, title=action, purpose='按已有文献进行研究',
        inputs=['已有材料'], outputs=['待生成材料'], depends_on=[i-1] if i>1 else [])
        for i, action in enumerate(['select_library_papers', 'analyze_papers', 'compare_papers', 'generate_review'], 1)]
    plan['search_queries'] = []
    with tasks.connection() as db:
        research_plans.initialize(db)
        db.execute('INSERT INTO research_plans VALUES (?,?)', ('library-plan', json.dumps({
            'id': 'library-plan', 'revision': 1, 'goal': GOAL, 'plan': plan, 'planning_date': '2026-11-18'})))
    def forbidden(*args, **kwargs):
        pytest.fail('文献库执行不得调用外部检索、收藏或全文下载')
    monkeypatch.setattr('api.research_search.search_arxiv_page', forbidden)
    monkeypatch.setattr(research_execution.papers, 'save_papers_batch', forbidden)
    monkeypatch.setattr(research_execution.fulltext, 'fetch_fulltext', forbidden)
    return client, state, Session


BASE = '/api/v1/agent/plans/library-plan'


def launch(client, papers=None, **kwargs):
    return post(client, BASE + '/library-executions', {
        'expected_revision': 1, 'papers': papers or [{'paper_id': 1}, {'paper_id': 2}], **kwargs})


def current(client):
    return client.get(BASE + '/executions').json()['items'][0]


def test_library_missing_analysis_to_review_report_and_export_without_search(library, monkeypatch):
    client, state, _ = library
    worker = research_execution.run_execution
    queued = []
    monkeypatch.setattr(research_execution, 'run_execution', queued.append)
    response, task = launch(client, review_after_analysis=True)
    assert task['status'] == 'queued'
    defaults_b(monkeypatch)
    worker(queued[0])
    run = current(client)
    assert run['source'] == 'library' and run['search_run_id'] is None
    assert run['status'] == 'succeeded', run
    assert all(p['analysis']['status'] == p['review']['status'] == 'succeeded' for p in run['papers'])
    assert all(json.loads(req.content)['model'] == 'model-A' for req, _, _ in state['requests'])
    assert not state['fetches']
    _, report = post(client, BASE + '/executions/' + response['execution_id'] + '/reports', {
        'research_question': '方法怎样比较？', 'comparison_dimensions': ['方法', '证据']})
    assert report['status'] == 'succeeded', report
    result = client.get('/api/v1/reviews/' + report['result_id']).json()
    assert result['research_context']['selection_source'] == 'library'
    assert len(result['research_context']['auxiliary_reviews']) == 2
    assert client.get('/api/v1/reviews/' + report['result_id'] + '/files/md').status_code == 200
    trace = client.get('/api/v1/tasks/' + task['id'] + '/trace').json()
    assert {t['type'] for t in trace['tasks']} >= {'research_execution', 'paper_analysis', 'claim_review', 'review_generation'}


def test_explicit_analysis_version_is_fixed_even_when_newer_result_exists(library, monkeypatch):
    client, state, _ = library
    _, first = post(client, '/api/v1/analyses', {'paper_id': 1})
    _, other = post(client, '/api/v1/analyses', {'paper_id': 2})
    old = first['result_id']
    _, newer = post(client, '/api/v1/analyses', {'paper_id': 1, 'force_restart': True})
    assert newer['result_id'] != old
    worker, queued = research_execution.run_execution, []
    monkeypatch.setattr(research_execution, 'run_execution', queued.append)
    before = len(state['requests'])
    response, _ = launch(client, papers=[{'paper_id': 1, 'analysis_id': old},
                                         {'paper_id': 2, 'analysis_id': other['result_id']}])
    worker(queued[0])
    run = current(client)
    assert run['status'] == 'succeeded', run
    assert run['papers'][0]['analysis']['analysis_id'] == old
    assert all(p['analysis']['reused'] for p in run['papers'])
    assert len(state['requests']) == before
    again, _ = launch(client, papers=[{'paper_id': 1, 'analysis_id': old},
                                      {'paper_id': 2, 'analysis_id': other['result_id']}])
    assert again['reused'] is True and again['execution_id'] == response['execution_id']


def test_invalid_library_selection_rejected_before_queue(library):
    client, _, _ = library
    for selection in ([{'paper_id': 1}], [{'paper_id': 1}, {'paper_id': 1}],
                      [{'paper_id': 1}, {'paper_id': 999}],
                      [{'paper_id': 1}, {'paper_id': 4}],
                      [{'paper_id': 1, 'analysis_id': 'foreign-id'}, {'paper_id': 2}]):
        response = client.post(BASE + '/library-executions', json={'expected_revision': 1, 'papers': selection})
        assert response.status_code in {404, 422}, response.text
    assert client.get(BASE + '/executions').json()['items'] == []
    assert client.post(BASE + '/library-executions', json={'expected_revision': 2,
        'papers': [{'paper_id': 1}, {'paper_id': 2}]}).status_code == 409


@pytest.mark.parametrize('change', ['document', 'chunks'])
def test_library_changed_material_stops_without_download_or_model(library, monkeypatch, change):
    client, state, Session = library
    worker, queued = research_execution.run_execution, []
    monkeypatch.setattr(research_execution, 'run_execution', queued.append)
    _, _ = launch(client)
    with Session.begin() as session:
        if change == 'document':
            session.get(PaperDocumentRecord, 10).content_sha256 = 'changed'
        else:
            chunk = session.query(DocumentChunkRecord).filter_by(document_id=10).first()
            chunk.text = '材料变化但文档哈希未变化'
    worker(queued[0])
    run = current(client)
    assert run['status'] == 'failed'
    assert run['papers'][0]['fulltext']['error']['code'] == 'RUNTIME_SNAPSHOT_UNAVAILABLE'
    assert not any(payload.get('paper_title') == '测试论文' for _, schema, payload in state['requests'] if schema == 'PaperAnalysis')


def test_library_review_resume_preserves_fixed_analysis_and_original_roles(library, monkeypatch):
    client, state, _ = library
    state['fail_schema'] = 'ReferenceClaimReview'
    response, failed = launch(client, review_after_analysis=True)
    assert failed['status'] == 'failed'
    first = current(client)
    ids = [p['analysis']['analysis_id'] for p in first['papers']]
    before = sum(schema == 'PaperAnalysis' for _, schema, _ in state['requests'])
    defaults_b(monkeypatch)
    state['fail_schema'] = None
    _, resumed = post(client, BASE + '/executions/' + response['execution_id'] + '/resume')
    assert resumed['status'] == 'succeeded', resumed
    assert [p['analysis']['analysis_id'] for p in current(client)['papers']] == ids
    assert sum(schema == 'PaperAnalysis' for _, schema, _ in state['requests']) == before
    assert json.loads(state['requests'][-1][0].content)['model'] == 'model-A'
