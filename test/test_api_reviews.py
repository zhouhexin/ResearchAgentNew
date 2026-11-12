"""多篇分析快照 → 比较矩阵 → 受控模型综述 → 原文核验 → 下载。"""
from contextlib import contextmanager
from copy import deepcopy
import csv
import io
import json

import pytest
from sqlalchemy import delete

from api import analyses, documents, reviews, tasks
from api.analysis_results import complete_analysis, load_result
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_api_analyses import client as analysis_client, model as analysis_model
from test.test_codex_model import cli


@pytest.fixture
def client(analysis_client, analysis_model, monkeypatch, tmp_path):
    monkeypatch.setattr(reviews, 'make_db', documents.make_db)
    monkeypatch.setattr(reviews, 'ERROR_DIR', tmp_path / 'review_errors')
    analysis_client.app.include_router(reviews.router)
    for paper_id in (1, 3):
        response = analysis_client.post('/api/v1/analyses', json={'paper_id': paper_id, 'max_chunks': 6})
        task = analysis_client.get('/api/v1/tasks/' + response.json()['task_id']).json()
        assert task['status'] == 'succeeded'
    return analysis_client


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
            payload = json.loads(messages[1][1])
            state['payload'] = payload
            refs = [finding['evidence'][0]['ref'] for p in payload['papers'] for values in p['findings'].values() for finding in values]
            if state['mode'] == 'bad_reference':
                refs[-1] = 'P3:E99999'
            if state['mode'] == 'wrong_paper':
                refs[-1] = 'P1:' + refs[-1].split(':')[1]
            draft = {'title': '多论文方法比较', 'background': [], 'common_methods': [],
                     'comparisons': [{'claim': '两篇论文呈现可比较的研究发现', 'evidence_refs': refs}],
                     'limitations': [], 'tentative_directions': []}
            if state['mode'] == 'single_paper':
                draft['comparisons'] = []
                draft['background'] = [{'claim': '单篇发现', 'evidence_refs': refs[:1]}]
            if state['mode'] in {'detailed', 'bad_paragraph'}:
                draft['comparisons'][0].update(heading='结合实验口径理解结果', paragraphs=[
                    {'text': '先解释研究中提供的方法和输入，再说明比较所依赖的条件。', 'evidence_refs': refs[:1]},
                    {'text': '另一篇论文提供了不同的研究发现，结论应保留各自的适用范围。',
                     'evidence_refs': ['P3:E99999'] if state['mode'] == 'bad_paragraph' else refs[-1:]}])
                draft['synthesis'] = [{'claim': '应结合研究问题及各篇证据条件作综合判断。', 'evidence_refs': refs}]
            if state['mode'] == 'raw_json':
                from langchain_core.messages import AIMessage
                return {'raw': AIMessage(content='```json\n' + json.dumps(draft) + '\n```'), 'parsed': None, 'parsing_error': None}
            return draft
    @contextmanager
    def factory(**kwargs):
        state['settings'] = kwargs
        yield FakeModel()
    monkeypatch.setattr(reviews, 'analysis_llm', factory)
    return state


def compare(client):
    response = client.post('/api/v1/reviews/compare', json={'paper_ids': [1, 3]})
    assert response.status_code == 200
    return response.json()


def generate(client, selection=None, topic='方法比较'):
    body = {'paper_ids': [1, 3], 'topic': topic}
    if selection:
        body['analysis_ids'] = selection['analysis_ids']
    response = client.post('/api/v1/reviews', json=body)
    assert response.status_code == 202
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


def test_compare_pins_versions_without_model_call(client, model):
    value = compare(client)
    assert value['paper_ids'] == [1, 3]
    assert len(value['analysis_ids']) == 2
    assert [row['paper_id'] for row in value['matrix']] == [1, 3]
    assert all(row['analysis_id'] in value['analysis_ids'] for row in value['matrix'])
    assert model['calls'] == 0


def test_codex_cli_review_validates_two_papers_and_exports(client, cli, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY', raising=False)
    monkeypatch.delenv('LLM_MODEL', raising=False)
    task = generate(client)
    assert task['status'] == 'succeeded' and task['http_backend'] == 'codex'
    report = client.get('/api/v1/reviews/' + task['result_id']).json()
    assert report['model_name'] == 'Codex（CLI 默认模型）'
    assert {ref.split(':')[0] for point in report['draft']['comparisons'] for ref in point['evidence_refs']} == {'P1', 'P3'}
    for format in ('json', 'md', 'csv'):
        response = client.get(f'/api/v1/reviews/{task["result_id"]}/files/{format}')
        assert response.status_code == 200 and response.content


def test_review_result_downloads_and_citation_ownership(client, model):
    selection = compare(client)
    task = generate(client, selection)
    assert task['type'] == 'review_generation' and task['status'] == 'succeeded'
    assert task['progress'] == 100
    result_response = client.get('/api/v1/reviews/' + task['result_id'])
    value = result_response.json()
    assert value['analysis_ids'] == selection['analysis_ids']
    assert value['paper_ids'] == [1, 3] and value['is_current'] is True
    assert '/private/' not in result_response.text and 'private-test-key' not in result_response.text
    for ref in value['draft']['comparisons'][0]['evidence_refs']:
        paper, evidence = ref.split(':')
        record = client.get('/api/v1/evidence/' + evidence).json()
        assert record['paper_id'] == int(paper[1:])
    prefix = '/api/v1/reviews/' + value['id'] + '/files/'
    json_file = client.get(prefix + 'json')
    assert json_file.json()['id'] == value['id']
    assert 'attachment' in json_file.headers['content-disposition']
    markdown = client.get(prefix + 'md')
    assert markdown.status_code == 200
    assert '多论文方法比较' in markdown.text and '/private/' not in markdown.text
    assert selection['analysis_ids'][0] in markdown.text
    matrix = client.get(prefix + 'csv')
    assert matrix.content.startswith(b'\xef\xbb\xbf')
    rows = list(csv.DictReader(io.StringIO(matrix.content.decode('utf-8-sig'))))
    assert {row['论文ID'] for row in rows} == {'1', '3'}
    assert '[P1:' in rows[0]['研究问题']
    assert client.get(prefix + 'exe').status_code == 422
    assert client.get('/api/v1/reviews/missing').status_code == 404


def test_existing_report_gets_friendly_citations_and_new_markdown_without_regeneration(client, model):
    task = generate(client)
    key = task['result_id']
    calls = model['calls']
    # 模拟功能上线前已保存的报告：只保留原有冻结快照，旧 MD 为普通编号。
    with tasks.connection() as db:
        row = db.execute('SELECT payload FROM reviews WHERE id=?', (key,)).fetchone()
        payload = json.loads(row[0])
        payload.pop('citation_index', None)
        db.execute('UPDATE reviews SET payload=?,markdown=? WHERE id=?',
                   (json.dumps(payload), 'legacy plain citations', key))
    value = client.get('/api/v1/reviews/' + key).json()
    refs = value['draft']['comparisons'][0]['evidence_refs']
    assert set(value['citation_index']) == set(refs)
    for ref, citation in value['citation_index'].items():
        assert citation['paper_number'] == value['paper_ids'].index(citation['paper_id']) + 1
        assert citation['quote_kind'] == 'analysis_quotes'
        assert citation['quotes'] and all(q in value['evidence_index'][ref]['text'] for q in citation['quotes'])
    url = '/api/v1/reviews/' + key + '/files/'
    markdown = client.get(url + 'md').text
    assert '[^evidence-1]:' in markdown and 'legacy plain citations' not in markdown
    assert '完整原文引句' in markdown and '内部定位：`' in markdown
    assert value['analysis_ids'][0] in markdown
    assert client.get(url + 'json').json()['citation_index'] == value['citation_index']
    # 原文更新后仍沿用报告自身快照；只标记历史状态，不重新生成或替换引句。
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).content_sha256 = 'new-document'
    historical = client.get('/api/v1/reviews/' + key).json()
    assert historical['is_current'] is False
    assert historical['citation_index'] == value['citation_index']
    assert model['calls'] == calls


@pytest.mark.parametrize('mode,code', [('bad_reference', 'REVIEW_VALIDATION_FAILED'), ('wrong_paper', 'REVIEW_VALIDATION_FAILED'), ('single_paper', 'REVIEW_VALIDATION_FAILED'), ('empty', 'REVIEW_STRUCTURE_FAILED')])
def test_invalid_review_never_published(client, model, mode, code):
    model['mode'] = mode
    task = generate(client)
    assert task['status'] == 'failed' and task['result_id'] is None
    assert task['error']['code'] == code
    assert (reviews.ERROR_DIR / f'{task["id"]}.json').exists()


def test_raw_json_model_response_supported(client, model):
    model['mode'] = 'raw_json'
    assert generate(client)['status'] == 'succeeded'


def test_detailed_paragraphs_and_synthesis_persist_and_export(client, model, monkeypatch):
    model['mode'] = 'detailed'
    monkeypatch.setenv('REVIEW_MAX_OUTPUT_TOKENS', '9000')
    task = generate(client)
    assert task['status'] == 'succeeded'
    assert model['settings']['max_output_tokens'] == 9000
    result = client.get('/api/v1/reviews/' + task['result_id']).json()
    assert result['writing_profile'] == 'detailed-v1'
    assert len(result['draft']['comparisons'][0]['paragraphs']) == 2
    assert result['draft']['synthesis']
    markdown = client.get(f'/api/v1/reviews/{task["result_id"]}/files/md').text
    assert '### 结合实验口径理解结果' in markdown and '先解释研究中提供的方法' in markdown
    assert '围绕研究问题的综合回答' in markdown
    assert model['payload']['papers'][0]['material_coverage']['research_questions']['provided'] >= 1


def test_invalid_paragraph_cannot_be_published(client, model):
    model['mode'] = 'bad_paragraph'
    task = generate(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'REVIEW_VALIDATION_FAILED'


@pytest.mark.parametrize('paper_ids,status', [([1], 422), ([1, 1], 422), ([1, 2, 3, 4, 5, 6], 422), ([1, 999], 404), ([1, 2], 422), ([0, 3], 422)])
def test_selection_validation_happens_before_model(client, model, paper_ids, status):
    response = client.post('/api/v1/reviews', json={'paper_ids': paper_ids, 'topic': '比较'})
    assert response.status_code == status
    assert model['calls'] == 0


def test_wrong_analysis_version_and_blank_topic_rejected(client, model):
    selection = compare(client)
    assert client.post('/api/v1/reviews', json={'paper_ids': [1, 3], 'analysis_ids': selection['analysis_ids'][::-1], 'topic': '比较'}).status_code == 422
    assert client.post('/api/v1/reviews', json={'paper_ids': [1, 3], 'topic': '  '}).status_code == 422
    assert model['calls'] == 0


@pytest.mark.parametrize('change', ['hash', 'chunk_ids'])
def test_original_changes_during_generation_prevent_publish(client, model, change):
    _, Session = documents.make_db('sqlite://')
    def hook():
        with Session.begin() as session:
            if change == 'hash':
                session.get(PaperDocumentRecord, 10).content_sha256 = 'changed'
            else:
                session.execute(delete(DocumentChunkRecord).where(DocumentChunkRecord.document_id == 10))
    model['hook'] = hook
    task = generate(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'REVIEW_INPUTS_CHANGED'
    assert task['result_id'] is None


def test_explicit_preview_versions_used_even_if_new_analysis_appears(client, model):
    selection = compare(client)
    snapshot = deepcopy(load_result(selection['analysis_ids'][0]))
    snapshot['analysis']['research_questions'][0]['statement'] = '新版本分析，不应替换已预览矩阵'
    pending = tasks.create_task(1, task_type='paper_analysis')
    newer = complete_analysis(pending['id'], snapshot)
    task = generate(client, selection)
    assert task['status'] == 'succeeded'
    result = client.get('/api/v1/reviews/' + task['result_id']).json()
    assert result['analysis_ids'] == selection['analysis_ids']
    assert newer['id'] not in result['analysis_ids']


def test_historical_review_retained_after_failure_and_source_update(client, model):
    first = generate(client)
    model['mode'] = 'single_paper'
    assert generate(client)['status'] == 'failed'
    assert client.get('/api/v1/reviews/' + first['result_id']).json()['is_current'] is True
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).content_sha256 = 'updated'
    assert client.get('/api/v1/reviews/' + first['result_id']).json()['is_current'] is False
    assert client.post('/api/v1/reviews/compare', json={'paper_ids': [1, 3]}).status_code == 422


def test_duplicate_active_task_reused_only_for_same_inputs(client, model, monkeypatch):
    monkeypatch.setattr(reviews, 'run_review', lambda *args: None)
    body = {'paper_ids': [1, 3], 'topic': '方法比较'}
    first = client.post('/api/v1/reviews', json=body).json()
    repeated = client.post('/api/v1/reviews', json=body).json()
    assert repeated['reused'] is True and repeated['task_id'] == first['task_id']
    other = client.post('/api/v1/reviews', json=dict(body, topic='局限比较')).json()
    assert other['task_id'] != first['task_id']
    assert model['calls'] == 0


def test_model_timeout_is_failed_task(client, model):
    import httpx
    from openai import APITimeoutError
    model['exception'] = APITimeoutError(request=httpx.Request('POST', 'https://model.example.org'))
    task = generate(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'MODEL_TIMEOUT'


def test_model_connection_failure_exposes_safe_cause(client, model):
    import socket
    import httpx
    from openai import APIConnectionError

    exc = APIConnectionError(request=httpx.Request('POST', 'https://model.example.org/private?key=private-test-key'))
    exc.__cause__ = socket.gaierror('private-test-key')
    model['exception'] = exc
    task = generate(client)
    assert task['error']['code'] == 'MODEL_CONNECTION_ERROR'
    assert '域名解析失败' in task['error']['message']
    diagnostic = (reviews.ERROR_DIR / f'{task["id"]}.json').read_text()
    assert json.loads(diagnostic)['reason'] == 'dns'
    assert 'private-test-key' not in diagnostic + json.dumps(task)


def test_comparison_does_not_require_model_key(client, model, monkeypatch):
    monkeypatch.delenv('LLM_API_KEY')
    assert compare(client)['paper_ids'] == [1, 3]
    response = client.post('/api/v1/reviews', json={'paper_ids': [1, 3], 'topic': '比较'})
    assert response.status_code == 422
    assert model['calls'] == 0


def test_analysis_without_supported_findings_cannot_enter_review(client, model):
    selection = compare(client)
    snapshot = deepcopy(load_result(selection['analysis_ids'][1]))
    snapshot['analysis'] = {key: [] for key in snapshot['analysis']}
    snapshot['evidence_index'] = {}
    pending = tasks.create_task(3, task_type='paper_analysis')
    complete_analysis(pending['id'], snapshot)
    response = client.post('/api/v1/reviews/compare', json={'paper_ids': [1, 3]})
    assert response.status_code == 422
    assert '没有可用于综述' in response.json()['detail']
    assert model['calls'] == 0
