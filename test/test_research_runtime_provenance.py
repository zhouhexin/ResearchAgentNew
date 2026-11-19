"""P0 网页多角色闭环：真实 HTTP 请求组装、临时业务库和检查点，不联网。"""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from importlib import import_module
import json

from openai import DefaultHttpxClient
import pytest
from sqlalchemy import select

from api import (analyses, claim_reviews, documents, papers, plan_reports, provenance,
                 research_execution, research_plans, research_search, reviews, runtime_bindings, tasks, uploads)
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_planning import GOAL, proposal


@pytest.fixture
def workspace(database_client, monkeypatch, tmp_path):
    import agent.runtime_snapshot as runtime
    monkeypatch.setattr(runtime, 'IDENTITY_KEY', tmp_path / 'identity.key')
    monkeypatch.setattr(claim_reviews, 'RUN_ROOT', tmp_path / 'claims')
    monkeypatch.setattr(analyses, 'ERROR_DIR', tmp_path / 'analysis_errors')
    monkeypatch.setattr(reviews, 'ERROR_DIR', tmp_path / 'review_errors')
    for key in runtime.NETWORK_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in {'RESEARCH_RUNTIME_PINNING': 'true', 'ANALYSIS_RUNTIME_PINNING': 'false',
                       'LLM_MODEL': 'model-A', 'LLM_API_KEY': 'isolated-private-key',
                       'LLM_BASE_URL': 'https://a.example/v1', 'LLM_HTTP_BACKEND': 'sdk',
                       'LLM_READ_TIMEOUT': '17', 'LLM_MAX_RETRIES': '0', 'LLM_ENABLE_THINKING': 'false',
                       'LLM_MAX_OUTPUT_TOKENS': '4321', 'RESEARCH_PLAN_MAX_OUTPUT_TOKENS': '6000',
                       'REVIEW_MAX_OUTPUT_TOKENS': '10000', 'CLAIM_REVIEW_MAX_OUTPUT_TOKENS': '2000',
                       'CLAIM_REVIEW_MAX_INPUT_CHARS': '24000', 'ANALYSIS_BATCH_RETRIES': '1',
                       'ANALYSIS_BATCH_MAX_CHARS': '4000', 'STRUCTURED_OUTPUT_METHOD': 'json_schema'}.items():
        monkeypatch.setenv(key, value)
    factory = documents.make_db
    for module in (analyses, claim_reviews, reviews, runtime_bindings, provenance, uploads):
        monkeypatch.setattr(module, 'make_db', factory)
    monkeypatch.setattr('agent.paper_context.make_db', factory)
    monkeypatch.setattr('agent.batch_analysis.sleep', lambda _: None)
    _, Session = factory('sqlite://')
    with Session.begin() as session:
        session.add(PaperDocumentRecord(id=20, paper_id=2, source_url='file:///private/two.pdf',
            source_type='pdf', parser='pymupdf', page_count=1, content='Another method.',
            content_sha256=sha256(b'Another method.').hexdigest()))
        session.flush()
        session.add(DocumentChunkRecord(document_id=20, chunk_index=0, text='Another method.',
            start_char=0, end_char=15, page_number=1))
    candidates = [{'source': 'arxiv', 'title': f'Paper {i}', 'authors': [], 'abstract': f'Robot vision method {i}.',
                   'doi': f'10.48550/arxiv.2601.0000{i}', 'url': f'https://arxiv.org/abs/2601.0000{i}',
                   'arxiv_id': f'2601.0000{i}', 'year': 2024, 'identity_verified': True} for i in (1, 2)]
    state = {'requests': [], 'fetches': [], 'hook': None, 'fail_schema': None}
    def fetch(query, **kwargs):
        state['fetches'].append((query, kwargs))
        return {'items': deepcopy(candidates), 'total': 2}
    monkeypatch.setattr(research_search, 'search_arxiv_page', fetch)
    monkeypatch.setattr(research_execution.papers, 'save_papers_batch', lambda body: {
        'results': [{'status': 'existing', 'paper_id': int(body.papers[0].doi[-1])}]})
    with DefaultHttpxClient() as probe:
        library = next(cls.__module__.split('.')[0] for cls in type(probe).__mro__
                       if cls.__module__.split('.')[0] in {'httpx', 'httpx2'})
    http = import_module(library)
    def handler(request):
        body = json.loads(request.content)
        schema = body['response_format']['json_schema']['name']
        payload = json.loads(body['messages'][1]['content'])
        state['requests'].append((request, schema, payload))
        if state['hook']:
            state['hook'](schema)
        if state['fail_schema'] == schema:
            return http.Response(503, json={'error': {'message': 'controlled failure'}})
        if schema == 'ResearchPlan':
            result = proposal()
        elif schema == 'ScreeningBatch':
            result = {'papers': [{'candidate_id': paper['candidate_id'], 'checks': [
                {'criterion_id': criterion['id'], 'verdict': 'fails' if criterion['kind'] == 'exclude' else 'meets',
                 'quote': paper['title'], 'explanation': '标题支持。'} for criterion in payload['criteria']]}
                for paper in payload['papers']]}
        elif schema == 'PaperAnalysis':
            evidence = payload['evidence'][0]
            result = {'research_questions': [{'statement': '研究方法 ' + evidence['evidence_id'], 'evidence': [
                {'evidence_id': evidence['evidence_id'], 'quote': evidence['text']}]}],
                      'methodologies': [], 'contributions': [], 'experiments': [], 'limitations': []}
        elif schema == 'ReferenceClaimReview':
            result = {'item_id': payload['sample_id'], 'verdict': state.get('verdict', 'supported'), 'reason': '受控判定。',
                      'evidence_assessments': [{'evidence_id': e['evidence_id'], 'relation': 'contradicts' if state.get('verdict') == 'contradicted' else 'supports',
                                               'reason': '原文支持。', 'span_ids': [e['spans'][0]['span_id']]}
                                              for e in payload['evidence']]}
        elif schema == 'CoverageDraft':
            finding = payload['papers'][0]['findings'][0]
            ref = finding['evidence_refs'][0]
            evidence = next(e for e in payload['evidence'] if e['ref_id'] == ref)
            result = state.get('coverage_response') or {'questions': [
                {'question_id': q['question_id'], 'status': 'partial', 'answer': '所给材料支持部分方法描述。',
                 'reason': '现有材料尚不足以完成全部维度对照。', 'gaps': ['尚缺少一致口径的实验对照。'],
                 'finding_ids': [finding['finding_id']],
                 'citations': [{'ref_id': ref, 'span_ids': [evidence['spans'][0]['span_id']]}],
                 'suggestions': [{'action': 'read_existing', 'paper_ids': [payload['papers'][0]['paper_id']],
                    'target': '实验条件相关正文（待定位）', 'rationale': '补齐实验条件再比较。'}]}
                for q in payload['questions']], 'limitations': ['仅评估本轮所提供的分析和原文片段，模型判断待人工核对。']}
        else:
            refs = [finding['evidence'][0]['ref'] for paper in payload['papers']
                    for findings in paper['findings'].values() for finding in findings]
            if schema == 'QuestionSuggestions':
                result = {'questions': [{'question': '方法怎样比较？', 'rationale': '材料可比较。', 'evidence_refs': refs}]}
            else:
                assert schema == 'ReviewDraft'
                result = {'title': '比较研究', 'background': [], 'common_methods': [],
                          'comparisons': [{'claim': '两篇论文提供研究发现。', 'evidence_refs': refs}],
                          'limitations': [], 'tentative_directions': []}
        return http.Response(200, json={'id': 'controlled', 'object': 'chat.completion', 'created': 0,
            'model': body['model'], 'choices': [{'index': 0, 'message': {'role': 'assistant',
                'content': json.dumps(result)}, 'finish_reason': 'stop'}]})
    monkeypatch.setattr('agent.llm_runtime.DefaultHttpxClient', lambda **kwargs:
                        DefaultHttpxClient(transport=http.MockTransport(handler), **kwargs))
    for module in (tasks, analyses, claim_reviews, research_plans, research_search,
                   research_execution, plan_reports, reviews):
        database_client.app.include_router(module.router)
    return database_client, state, Session


def post(client, url, body=None):
    response = client.post(url, json=body)
    assert response.status_code == 202, response.text
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    return response.json(), task


def defaults_b(monkeypatch):
    for key, value in {'LLM_MODEL': 'model-B', 'LLM_BASE_URL': 'https://b.example/v1',
                       'LLM_READ_TIMEOUT': '29', 'LLM_MAX_OUTPUT_TOKENS': '999',
                       'RESEARCH_PLAN_MAX_OUTPUT_TOKENS': '7000', 'REVIEW_MAX_OUTPUT_TOKENS': '8000',
                       'CLAIM_REVIEW_MAX_OUTPUT_TOKENS': '3000', 'LLM_ENABLE_THINKING': 'true'}.items():
        monkeypatch.setenv(key, value)


def begin(client):
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    assert planning['status'] == 'succeeded', planning
    plan_id = planning['result_id']
    response, search = post(client, f'/api/v1/agent/plans/{plan_id}/search', {'expected_revision': 1, 'per_query': 10})
    assert search['status'] == 'succeeded', search
    run = client.get(f'/api/v1/agent/plans/{plan_id}/search').json()['run']
    return planning, run


def test_plan_search_analysis_claim_review_and_report_keep_root_config_and_trace(workspace, monkeypatch):
    client, state, _ = workspace
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    assert planning['status'] == 'succeeded', planning
    plan_id = planning['result_id']
    defaults_b(monkeypatch)
    _, search = post(client, f'/api/v1/agent/plans/{plan_id}/search', {'expected_revision': 1, 'per_query': 10})
    assert search['status'] == 'succeeded', search
    run = client.get(f'/api/v1/agent/plans/{plan_id}/search').json()['run']
    response, execution = post(client, f'/api/v1/agent/plans/{plan_id}/executions', {
        'expected_revision': 1, 'search_run_id': run['id'], 'candidate_ids': [c['candidate_id'] for c in run['candidates']]})
    assert execution['status'] == 'succeeded', execution
    saved = client.get(f'/api/v1/agent/plans/{plan_id}/executions').json()['items'][0]
    aid = saved['papers'][0]['analysis']['analysis_id']
    _, claim = post(client, f'/api/v1/analyses/{aid}/claim-reviews')
    assert claim['status'] == 'succeeded', claim
    _, report = post(client, f'/api/v1/agent/plans/{plan_id}/executions/{response["execution_id"]}/reports', {
        'research_question': '方法怎样比较？', 'comparison_dimensions': ['方法', '证据']})
    assert report['status'] == 'succeeded', report
    output = client.get('/api/v1/reviews/' + report['result_id']).json()
    assert output['model_name'] == 'model-A'
    assert output['interaction_id'] == planning['interaction_id']
    markdown = client.get('/api/v1/reviews/' + report['result_id'] + '/files/md')
    assert markdown.status_code == 200
    assert planning['interaction_id'] in markdown.text and output['runtime_snapshot_id'] in markdown.text
    trace = client.get('/api/v1/tasks/' + planning['id'] + '/trace')
    assert trace.status_code == 200, trace.text
    trace = trace.json()
    assert trace['interaction']['evaluation_allowed'] is False
    records = {task['id']: task for task in trace['tasks']}
    assert records[search['id']]['parent_task_id'] == planning['id']
    assert records[execution['id']]['parent_task_id'] == search['id']
    for paper in saved['papers']:
        child = records[paper['analysis']['task_id']]
        assert child['parent_task_id'] == execution['id']
        assert child['plan_step'] == 4
    assert records[claim['id']]['parent_task_id'] == saved['papers'][0]['analysis']['task_id']
    assert records[report['id']]['parent_task_id'] == execution['id']
    review_child = next(task for task in records.values() if task['type'] == 'review_generation')
    assert review_child['parent_task_id'] == report['id']
    assert all(task['root_task_id'] == planning['id'] for task in records.values())
    assert trace['plan_revisions'][0]['revision'] == 1
    assert any(artifact['id'] == output['id'] for artifact in trace['artifacts'])
    expected_tokens = {'ResearchPlan': 6000, 'ScreeningBatch': 6000, 'PaperAnalysis': 4321,
                       'ReferenceClaimReview': 2000, 'ReviewDraft': 10000}
    for request, schema, _ in state['requests']:
        body = json.loads(request.content)
        assert body['model'] == 'model-A' and request.url.host == 'a.example'
        assert body['max_completion_tokens'] == expected_tokens[schema]
        assert body['enable_thinking'] is False and request.extensions['timeout']['read'] == 17
    # 新根任务使用更新后的默认值；旧根任务从未被改写。
    _, other = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    assert other['status'] == 'succeeded'
    assert other['interaction_id'] != planning['interaction_id']
    request = state['requests'][-1][0]
    assert json.loads(request.content)['model'] == 'model-B' and request.url.host == 'b.example'


def test_plan_revisions_remain_immutable_and_search_records_exact_revision(workspace):
    client, _, _ = workspace
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    plan_id = planning['result_id']
    original = client.get('/api/v1/agent/plans/' + plan_id).json()
    changed = deepcopy(original['plan'])
    changed['research_questions'] = ['新的比较问题是什么？']
    response = client.put('/api/v1/agent/plans/' + plan_id, json={'expected_revision': 1, 'plan': changed})
    assert response.status_code == 200, response.text
    old = client.get(f'/api/v1/agent/plans/{plan_id}/revisions/1')
    new = client.get(f'/api/v1/agent/plans/{plan_id}/revisions/2')
    assert old.status_code == new.status_code == 200
    assert old.json()['plan']['research_questions'] == original['plan']['research_questions']
    assert new.json()['plan']['research_questions'] == changed['research_questions']
    _, search = post(client, f'/api/v1/agent/plans/{plan_id}/search', {'expected_revision': 2})
    assert search['plan_revision'] == 2 and search['plan_snapshot_id'].startswith(f'{plan_id}:2:')


def test_claim_resume_uses_registry_and_cache_after_defaults_switch(workspace, monkeypatch):
    client, state, _ = workspace
    _, analysis = post(client, '/api/v1/analyses', {'paper_id': 1, 'batch_size': 1})
    assert analysis['status'] == 'succeeded', analysis
    state['fail_schema'] = 'ReferenceClaimReview'
    response, failed = post(client, f'/api/v1/analyses/{analysis["result_id"]}/claim-reviews')
    assert failed['status'] == 'failed'
    defaults_b(monkeypatch)
    monkeypatch.setenv('RESEARCH_RUNTIME_PINNING', 'false')
    state['fail_schema'] = None
    _, resumed = post(client, f'/api/v1/analyses/{analysis["result_id"]}/claim-reviews?resume_review_id={response["review_id"]}')
    assert resumed['status'] == 'succeeded', resumed
    assert resumed['resumed_from_task_id'] == failed['id']
    assert resumed['runtime_snapshot_id'] == failed['runtime_snapshot_id']
    assert json.loads(state['requests'][-1][0].content)['model'] == 'model-A'
    # 显式新建允许采用当前配置，同时保留旧评审与原根任务。
    _, fresh = post(client, f'/api/v1/analyses/{analysis["result_id"]}/claim-reviews?new_run=true')
    assert fresh['status'] == 'succeeded', fresh
    assert fresh['runtime_snapshot_id'] != resumed['runtime_snapshot_id']
    assert fresh['interaction_id'] != resumed['interaction_id']
    assert json.loads(state['requests'][-1][0].content)['model'] == 'model-B'


def test_changed_credentials_reject_child_before_queue_and_preserve_plan(workspace, monkeypatch):
    client, state, _ = workspace
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    before = len(state['requests'])
    monkeypatch.setenv('LLM_API_KEY', 'unavailable-original-credential')
    response = client.post(f'/api/v1/agent/plans/{planning["result_id"]}/search', json={'expected_revision': 1})
    assert response.status_code == 409
    assert response.json()['detail']['code'] == 'RUNTIME_SNAPSHOT_UNAVAILABLE'
    assert len(state['requests']) == before
    assert client.get('/api/v1/agent/plans/' + planning['result_id']).status_code == 200


def test_search_resume_reuses_queries_and_original_model_after_restart(workspace, monkeypatch):
    client, state, _ = workspace
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    url = f'/api/v1/agent/plans/{planning["result_id"]}/search'
    state['fail_schema'] = 'ScreeningBatch'
    first, failed = post(client, url, {'expected_revision': 1})
    assert failed['status'] == 'failed' and len(state['fetches']) == 2
    defaults_b(monkeypatch)
    monkeypatch.setenv('RESEARCH_RUNTIME_PINNING', 'false')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', 'invalid-new-default')
    state['fail_schema'] = None
    second, resumed = post(client, url, {'expected_revision': 1})
    assert resumed['status'] == 'succeeded', resumed
    assert first['run_id'] == second['run_id']
    assert resumed['resumed_from_task_id'] == failed['id']
    assert len(state['fetches']) == 2
    assert json.loads(state['requests'][-1][0].content)['model'] == 'model-A'


def test_question_suggestion_to_edited_review_keeps_source_task_config(workspace, monkeypatch):
    client, state, _ = workspace
    ids = []
    for paper_id in (1, 2):
        _, task = post(client, '/api/v1/analyses', {'paper_id': paper_id})
        assert task['status'] == 'succeeded', task
        ids.append(task['result_id'])
    _, questions = post(client, '/api/v1/reviews/questions', {'paper_ids': [1, 2], 'analysis_ids': ids})
    assert questions['status'] == 'succeeded', questions
    defaults_b(monkeypatch)
    _, review = post(client, '/api/v1/reviews', {'paper_ids': [1, 2], 'analysis_ids': ids,
        'topic': '用户修改后的研究问题', 'source_question_task_id': questions['id']})
    assert review['status'] == 'succeeded', review
    result = client.get('/api/v1/reviews/' + review['result_id']).json()
    assert result['source_question_task_id'] == questions['id'] and result['question_edited'] is True
    assert result['model_name'] == 'model-A'
    trace = client.get('/api/v1/tasks/' + review['id'] + '/trace').json()
    assert next(t for t in trace['tasks'] if t['id'] == review['id'])['parent_task_id'] == questions['id']


def test_tool_source_mapping_and_pdf_budget_use_captured_configuration(workspace, monkeypatch):
    from tools.execution_config import scope
    from tools.venue_search import cvpr
    from tools.open_access_pdf import pdf_download_settings
    client, _, _ = workspace
    monkeypatch.setenv('OPENALEX_CVPR_SOURCE_IDS', 'S1')
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    binding = runtime_bindings.restore(planning['runtime_bindings'], 'search')
    monkeypatch.setenv('OPENALEX_CVPR_SOURCE_IDS', 'S999')
    monkeypatch.setenv('PDF_DOWNLOAD_TIMEOUT', '3')
    requests = []
    def fetch(path, params):
        requests.append((path, params))
        return {'id': 'https://openalex.org/S1', 'display_name': 'IEEE/CVF Conference on Computer Vision and Pattern Recognition', 'type': 'conference'}
    monkeypatch.setattr(cvpr, 'get_json', fetch)
    with scope(binding.tools['search']):
        assert cvpr.resolve_sources()[0]['id'] == 'S1'
        assert requests[0][0] == 'sources/S1'
        assert pdf_download_settings()['total_timeout'] == binding.tools['search']['pdf_download']['total_timeout']
    assert pdf_download_settings()['total_timeout'] == 3


def test_plan_revision_link_rollback_does_not_block_later_edits(workspace, monkeypatch):
    import sqlite3
    client, _, _ = workspace
    _, task = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    url = '/api/v1/agent/plans/' + task['result_id']
    current = client.get(url).json()
    changed = deepcopy(current['plan'])
    changed['research_questions'] = ['一次未提交的编辑']
    original = research_plans.link_revision
    def fail(db, plan):
        if plan['revision'] == 2:
            raise sqlite3.OperationalError('controlled rollback')
        original(db, plan)
    with monkeypatch.context() as temporary:
        temporary.setattr(research_plans, 'link_revision', fail)
        assert client.put(url, json={'expected_revision': 1, 'plan': changed}).status_code == 503
    assert client.get(url).json()['revision'] == 1
    assert client.get(url + '/revisions/2').status_code == 409
    changed['research_questions'] = ['用户随后提交的新编辑']
    assert client.put(url, json={'expected_revision': 1, 'plan': changed}).status_code == 200
    assert client.get(url + '/revisions/2').json()['plan']['research_questions'] == changed['research_questions']


def test_two_role_clients_remain_isolated_in_parallel(workspace, monkeypatch):
    from agent.research_planner import ResearchPlan
    _, state, _ = workspace
    bundle_a = runtime_bindings.capture_bundle()
    defaults_b(monkeypatch)
    bundle_b = runtime_bindings.capture_bundle()
    a = runtime_bindings.restore(bundle_a, 'planning')
    b = runtime_bindings.restore(bundle_b, 'planning')
    def invoke(binding):
        with binding.llm() as llm:
            llm.with_structured_output(ResearchPlan, method='json_schema', strict=True).invoke([
                ('system', 'Controlled test'), ('human', json.dumps({'goal': GOAL}))])
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(invoke, [a, b]))
    actual = {(json.loads(request.content)['model'], request.url.host,
               request.extensions['timeout']['read']) for request, _, _ in state['requests']}
    assert actual == {('model-A', 'a.example', 17), ('model-B', 'b.example', 29)}


def test_reused_analysis_keeps_its_original_model_provenance(workspace, monkeypatch):
    client, _, _ = workspace
    _, planning = post(client, '/api/v1/agent/plans', {'goal': GOAL})
    defaults_b(monkeypatch)
    _, standalone = post(client, '/api/v1/analyses', {'paper_id': 1})
    assert standalone['status'] == 'succeeded', standalone
    plan_id = planning['result_id']
    _, search = post(client, f'/api/v1/agent/plans/{plan_id}/search', {'expected_revision': 1})
    run = client.get(f'/api/v1/agent/plans/{plan_id}/search').json()['run']
    _, execution = post(client, f'/api/v1/agent/plans/{plan_id}/executions', {
        'expected_revision': 1, 'search_run_id': run['id'], 'candidate_ids': [c['candidate_id'] for c in run['candidates']]})
    assert execution['status'] == 'succeeded', execution
    trace = client.get('/api/v1/tasks/' + execution['id'] + '/trace').json()
    artifact = next(a for a in trace['artifacts'] if a['id'] == standalone['result_id'])
    assert artifact['relation'] == 'reused'
    assert artifact['source_task_id'] == standalone['id']
    assert artifact['source_runtime_snapshot_id'] == standalone['runtime_snapshot_id']


def auto_execute(client, planning, search):
    return post(client, f'/api/v1/agent/plans/{planning["result_id"]}/executions', {
        'expected_revision': 1, 'search_run_id': search['id'], 'review_after_analysis': True,
        'candidate_ids': [p['candidate_id'] for p in search['candidates']]})


def test_execution_automatically_reviews_fixed_analysis_and_exports_links(workspace, monkeypatch):
    client, state, _ = workspace
    planning, search = begin(client)
    state['verdict'] = 'contradicted'
    defaults_b(monkeypatch)
    response, task = auto_execute(client, planning, search)
    assert task['status'] == 'succeeded', task
    base = f'/api/v1/agent/plans/{planning["result_id"]}/executions'
    run = client.get(base).json()['items'][0]
    assert run['review_after_analysis'] is True
    for paper in run['papers']:
        assert paper['analysis']['status'] == paper['review']['status'] == 'succeeded'
        review = paper['review']
        assert review['analysis_id'] == paper['analysis']['analysis_id']
        assert review['task']['parent_task_id'] == task['id']
        assert review['task']['execution_phase'] == 'review'
        assert review['task']['analysis_task_id'] == paper['analysis']['task_id']
        assert review['completed_claims'] == review['total_claims'] == 1
        assert review['verdict_counts']['contradicted'] == 1  # 完成表示运行完成，不能把模型判矛盾当作执行失败。
    review_requests = [(request, schema) for request, schema, _ in state['requests'] if schema == 'ReferenceClaimReview']
    assert len(review_requests) == 2
    assert all(json.loads(request.content)['model'] == 'model-A' and request.url.host == 'a.example'
               for request, _ in review_requests)
    _, report = post(client, base + '/' + response['execution_id'] + '/reports', {
        'research_question': '方法怎样比较？', 'comparison_dimensions': ['方法']})
    assert report['status'] == 'succeeded', report
    result = client.get('/api/v1/reviews/' + report['result_id']).json()
    assert len(result['research_context']['auxiliary_reviews']) == 2
    md = client.get('/api/v1/reviews/' + report['result_id'] + '/files/md').text
    assert run['papers'][0]['review']['review_id'] in md
    before = len(state['requests'])
    repeated, _ = auto_execute(client, planning, search)
    assert repeated['reused'] is True and len(state['requests']) == before


def test_auto_review_failure_resume_keeps_analysis_and_skips_completed_reviews(workspace, monkeypatch):
    client, state, _ = workspace
    planning, search = begin(client)
    count = 0
    def hook(schema):
        nonlocal count
        if schema == 'ReferenceClaimReview':
            count += 1
            if count == 2:
                state['fail_schema'] = schema
    state['hook'] = hook
    response, first = auto_execute(client, planning, search)
    assert first['status'] == 'failed'
    base = f'/api/v1/agent/plans/{planning["result_id"]}/executions'
    run = client.get(base).json()['items'][0]
    assert all(p['analysis']['status'] == 'succeeded' for p in run['papers'])
    assert [p['review']['status'] for p in run['papers']] == ['succeeded', 'failed']
    before = len(state['requests'])
    ids = [p['analysis']['analysis_id'] for p in run['papers']]
    review_ids = [p['review']['review_id'] for p in run['papers']]
    defaults_b(monkeypatch)
    monkeypatch.setenv('RESEARCH_RUNTIME_PINNING', 'false')
    state.update(hook=None, fail_schema=None)
    _, resumed = post(client, base + '/' + response['execution_id'] + '/resume')
    assert resumed['status'] == 'succeeded', resumed
    run = client.get(base).json()['items'][0]
    assert [p['analysis']['analysis_id'] for p in run['papers']] == ids
    assert [p['review']['review_id'] for p in run['papers']] == review_ids
    assert len(state['requests']) == before + 1
    request, schema, _ = state['requests'][-1]
    assert schema == 'ReferenceClaimReview' and json.loads(request.content)['model'] == 'model-A'
    assert len(run['papers'][1]['review']['task_history']) == 2


def test_auto_review_of_reused_analysis_uses_execution_role(workspace, monkeypatch):
    client, state, _ = workspace
    planning, search = begin(client)
    defaults_b(monkeypatch)
    _, prior = post(client, '/api/v1/analyses', {'paper_id': 1})
    assert prior['status'] == 'succeeded'
    _, task = auto_execute(client, planning, search)
    assert task['status'] == 'succeeded', task
    run = client.get(f'/api/v1/agent/plans/{planning["result_id"]}/executions').json()['items'][0]
    paper = run['papers'][0]
    assert paper['analysis']['analysis_id'] == prior['result_id'] and paper['analysis']['reused'] is True
    assert paper['review']['task']['model_name'] == 'model-A'
    assert paper['review']['task']['interaction_id'] == planning['interaction_id']
    assert paper['review']['task']['analysis_task_id'] == prior['id']


def test_auto_review_refuses_changed_fixed_analysis_on_resume(workspace, monkeypatch):
    client, state, Session = workspace
    planning, search = begin(client)
    state['fail_schema'] = 'ReferenceClaimReview'
    response, first = auto_execute(client, planning, search)
    assert first['status'] == 'failed'
    base = f'/api/v1/agent/plans/{planning["result_id"]}/executions'
    run = client.get(base).json()['items'][0]
    original = run['papers'][0]['analysis']['analysis_id']
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).content_sha256 = 'changed-source'
    state['fail_schema'] = None
    _, resumed = post(client, base + '/' + response['execution_id'] + '/resume')
    assert resumed['status'] == 'failed'
    run = client.get(base).json()['items'][0]
    assert run['papers'][0]['analysis']['analysis_id'] == original
    assert run['papers'][0]['fulltext']['error']['code'] == 'RUNTIME_SNAPSHOT_UNAVAILABLE'
