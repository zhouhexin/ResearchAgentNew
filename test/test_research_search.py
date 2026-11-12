"""计划快照 → 实际检索工具适配 → 去重/初筛核验 → 持久化/恢复。"""
from contextlib import contextmanager
from copy import deepcopy
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest
import requests

from agent.research_search import ScreeningBatch, criteria_for, merge_candidates
from api import research_plans, research_search, tasks
from test.test_research_planning import GOAL, proposal
from test.test_codex_model import cli


def paper(number, **kwargs):
    identifier = f'2401.{number:05d}'
    return {'source': 'arxiv', 'arxiv_id': identifier, 'arxiv_version': identifier+'v1',
            'doi': '10.48550/arxiv.'+identifier, 'published_doi': None, 'openalex_id': None,
            'url': 'https://arxiv.org/abs/'+identifier, 'pdf_url': 'https://arxiv.org/pdf/'+identifier,
            'title': f'Robot vision benchmark {number}', 'authors': ['A. Researcher'],
            'abstract': f'We benchmark robot vision and energy efficiency in study {number}.',
            'year': 2024, 'citation_count': None, 'identity_verified': True, 'saved_paper_id': None, **kwargs}


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path/'tasks.sqlite3')
    monkeypatch.setenv('LLM_PROVIDER', 'openai')
    monkeypatch.setenv('LLM_API_KEY', 'private-search-key')
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')
    monkeypatch.setenv('LLM_MAX_RETRIES', '0')
    with tasks.connection() as db:
        research_plans.initialize(db)
        saved = {'id': 'plan_test', 'goal': GOAL, 'plan': proposal(), 'revision': 1, 'status': 'draft',
                 'created_at': tasks.now(), 'updated_at': tasks.now(), 'planning_date': '2026-11-10',
                 'model_name': 'test-model', 'source_task_id': 'planning_task', 'capabilities': {}}
        db.execute('INSERT INTO research_plans VALUES (?,?)', ('plan_test', json.dumps(saved)))
    state = {'fetches': [], 'calls': [], 'pages': [[paper(1), paper(2)], [paper(2), paper(3)]],
             'mode': 'valid', 'fail_search': None, 'fail_model_at': None}
    def fetch(query, **kwargs):
        index = proposal()['search_queries'].index(query)
        state['fetches'].append((index, kwargs))
        if state['fail_search'] == index:
            raise requests.Timeout('secret-network-detail')
        return {'items': deepcopy(state['pages'][index]), 'total': 100}
    monkeypatch.setattr(research_search, 'search_arxiv_page', fetch)
    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert schema is ScreeningBatch
            state['method'] = kwargs['method']
            return self
        def invoke(self, messages):
            payload = json.loads(messages[1][1]);state['calls'].append(payload)
            if state['fail_model_at'] == len(state['calls']):
                raise requests.Timeout('private-upstream-detail')
            result = {'papers': [{'candidate_id': p['candidate_id'], 'checks': [
                {'criterion_id': c['id'], 'verdict': ('meets' if state.get('matches_exclusion') else 'fails') if c['kind'] == 'exclude' else 'meets',
                 'quote': p['title'], 'explanation': '标题和摘要支持此项判断。'} for c in payload['criteria']
            ]} for p in payload['papers']]}
            mode = state['mode']
            if mode == 'unknown_paper':result['papers'][0]['candidate_id'] = 'arxiv:9999.00001'
            if mode == 'missing_paper':result['papers'].pop()
            if mode == 'duplicate_paper':result['papers'][-1] = deepcopy(result['papers'][0])
            if mode == 'missing_criterion':result['papers'][0]['checks'].pop()
            if mode == 'duplicate_criterion':result['papers'][0]['checks'][-1] = deepcopy(result['papers'][0]['checks'][0])
            if mode == 'quote_mismatch':result['papers'][0]['checks'][0]['quote'] = 'fabricated quote'
            if mode == 'cross_paper_quote':result['papers'][0]['checks'][0]['quote'] = payload['papers'][1]['title']
            if mode == 'empty_quote':result['papers'][0]['checks'][0]['quote'] = ''
            if mode == 'unknown_with_quote':result['papers'][0]['checks'][0]['verdict'] = 'unknown'
            if mode == 'empty_explanation':result['papers'][0]['checks'][0]['explanation'] = ' '
            if mode == 'decisions':
                result['papers'][1]['checks'][0].update(verdict='unknown', quote='')
                result['papers'][2]['checks'][0]['verdict'] = 'fails'
            if mode in {'truncated', 'refused', 'json', 'bad_json'}:
                raw = AIMessage(content='```json\n'+json.dumps(result)+'\n```' if mode == 'json' else '{',
                    response_metadata={'finish_reason': 'length'} if mode == 'truncated' else {},
                    additional_kwargs={'refusal': 'no'} if mode == 'refused' else {})
                return {'parsed': None if mode in {'json', 'bad_json'} else result, 'raw': raw}
            return result
    @contextmanager
    def factory(**kwargs):
        state['settings'] = kwargs
        yield Model()
    monkeypatch.setattr(research_search, 'analysis_llm', factory)
    app = FastAPI()
    app.include_router(research_plans.router);app.include_router(research_search.router);app.include_router(tasks.router)
    with TestClient(app) as client:
        yield client, state


URL = '/api/v1/agent/plans/plan_test/search'


def start(client, revision=1, per_query=10):
    response = client.post(URL, json={'expected_revision': revision, 'per_query': per_query})
    assert response.status_code == 202, response.text
    task = client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    return response.json(), task, client.get(URL, params={'run_id': response.json()['run_id']}).json()['run']


def edit(client, change):
    record = client.get('/api/v1/agent/plans/plan_test').json()
    change(record['plan'])
    response = client.put('/api/v1/agent/plans/plan_test', json={'expected_revision': record['revision'], 'plan': record['plan']})
    assert response.status_code == 200, response.text
    return response.json()


def test_search_screen_and_save_versioned_results(setup):
    client, state = setup;state['mode'] = 'decisions'
    response, task, run = start(client)
    assert task['type'] == 'research_search' and task['status'] == run['status'] == 'succeeded'
    assert task['result_id'] == run['id'] and task['plan_id'] == 'plan_test'
    assert run['revision'] == 1 and run['plan'] == proposal() and run['goal'] == GOAL
    assert len(run['candidates']) == 3 and run['duplicate_count'] == 1
    assert run['candidates'][1]['query_numbers'] == [1, 2]
    assert [s['decision'] for s in run['screenings'].values()] == ['included', 'uncertain', 'excluded']
    assert run['screenings']['arxiv:2401.00001']['checks'][-1]['criterion_id'] == 'Y'
    assert [item[1] for item in state['fetches']] == [{'page_size': 10, 'start_year': 2020, 'end_year': 2025}]*2
    assert len(state['calls']) == 1 and state['settings']['max_output_tokens'] == 6000
    assert 'private-search-key' not in json.dumps(run)+json.dumps(task)
    again, _, _ = start(client)
    assert again['reused'] and again['run_id'] == response['run_id'] and len(state['fetches']) == 2
    assert len(state['calls']) == 1


def test_search_failure_continues_only_unfinished_query(setup):
    client, state = setup;state['fail_search'] = 1
    first, task, partial = start(client)
    assert task['error']['code'] == 'SEARCH_TIMEOUT' and partial['status'] == 'failed'
    assert len(partial['candidates']) == 2 and partial['queries'][0]['status'] == 'succeeded'
    assert state['calls'] == [] and 'secret-network-detail' not in json.dumps(task)
    state['fail_search'] = None
    second, task, result = start(client)
    assert first['run_id'] == second['run_id'] and first['task_id'] != second['task_id']
    assert task['status'] == 'succeeded' and result['attempts'] == 2
    assert [item[0] for item in state['fetches']] == [0, 1, 1]


def test_screen_failure_keeps_verified_batches_and_resumes(setup):
    client, state = setup
    state['pages'] = [[paper(i) for i in range(1, 9)], []];state['fail_model_at'] = 2
    first, task, partial = start(client)
    assert task['status'] == 'failed' and len(partial['screenings']) == 5
    state['fail_model_at'] = None
    second, task, result = start(client)
    assert task['status'] == 'succeeded' and result['screenings'].keys() == {f'arxiv:2401.{i:05d}' for i in range(1, 9)}
    assert second['run_id'] == first['run_id'] and len(state['fetches']) == 2
    assert len(state['calls']) == 3 and len(state['calls'][-1]['papers']) == 3


@pytest.mark.parametrize('mode', ['unknown_paper', 'missing_paper', 'duplicate_paper', 'missing_criterion',
    'duplicate_criterion', 'quote_mismatch', 'cross_paper_quote', 'empty_quote', 'unknown_with_quote',
    'empty_explanation', 'truncated', 'refused', 'bad_json'])
def test_invalid_screening_batch_is_not_published(setup, mode):
    client, state = setup;state['mode'] = mode
    _, task, run = start(client)
    assert task['status'] == 'failed' and task['error']['code'] == 'SCREENING_VALIDATION_FAILED'
    assert run['screenings'] == {} and len(run['candidates']) == 3


def test_raw_json_screening_supported(setup):
    client, state = setup;state['mode'] = 'json'
    assert start(client)[1]['status'] == 'succeeded'


def test_empty_search_success_without_model_call(setup):
    client, state = setup;state['pages'] = [[], []]
    _, task, run = start(client)
    assert task['status'] == 'succeeded' and run['candidates'] == [] and state['calls'] == []


def test_years_missing_abstract_and_exclusion_are_conservative(setup):
    client, state = setup
    state['pages'] = [[paper(1, year=2019), paper(2, year=None), paper(3, abstract='')], []]
    _, _, run = start(client)
    assert [s['decision'] for s in run['screenings'].values()] == ['excluded', 'uncertain', 'uncertain']
    edit(client, lambda p: p['scope'].update(exclusion_criteria=['排除不相关领域'], languages=['英语']))
    state['matches_exclusion'] = True
    _, _, run = start(client, revision=2)
    assert {'T', 'I1', 'X1', 'L'} == {c['id'] for c in state['calls'][-1]['criteria']}
    assert run['screenings']['arxiv:2401.00002']['decision'] == 'excluded'  # 命中排除条件


def test_revision_changes_do_not_mix_snapshots_or_checkpoints(setup):
    client, state = setup
    first, _, old = start(client)
    edit(client, lambda p: p['scope'].update(start_year=2021, year_basis='edited', year_evidence=''))
    assert client.get(URL).json()['run'] is None
    assert client.post(URL, json={'expected_revision': 1}).status_code == 409
    second, _, new = start(client, revision=2)
    assert first['run_id'] != second['run_id'] and len(state['fetches']) == 4 and len(state['calls']) == 2
    assert old['plan']['scope']['start_year'] == 2020 and new['plan']['scope']['start_year'] == 2021
    assert client.get(URL, params={'run_id': first['run_id']}).json()['run']['revision'] == 1
    assert len(client.get(URL).json()['history']) == 2


def test_model_changes_create_separate_screening_runs(setup, monkeypatch):
    client, state = setup;first, _, _ = start(client)
    monkeypatch.setenv('LLM_MODEL', 'different-model')
    second, _, _ = start(client)
    assert first['run_id'] != second['run_id'] and len(state['calls']) == 2


def test_active_dedup_restart_and_stale_worker_guard(setup, monkeypatch):
    client, state = setup;runner = research_search.run_search
    monkeypatch.setattr(research_search, 'run_search', lambda *a: None)
    first, _, old = start(client)
    assert start(client)[0] == {**first, 'reused': True}
    assert client.post(URL, json={'expected_revision': 1, 'per_query': 8}).status_code == 409
    tasks.recover_interrupted_tasks()
    assert client.get(URL).json()['run']['status'] == 'failed'
    monkeypatch.setattr(research_search, 'run_search', runner)
    second, task, new = start(client)
    assert second['run_id'] == first['run_id'] and task['status'] == 'succeeded'
    with pytest.raises(RuntimeError):
        research_search.write_checkpoint(old, first['task_id'], complete=True)
    assert client.get(URL).json()['run']['task_id'] == second['task_id']


@pytest.mark.parametrize('mutation', ['source', 'library_only', 'unsupported_prerequisite', 'screen_before_search'])
def test_unsupported_plan_is_not_silently_substituted(setup, mutation):
    client, state = setup
    with tasks.connection() as db:
        value = json.loads(db.execute('SELECT payload FROM research_plans').fetchone()[0])
        p = value['plan']
        if mutation == 'source':p['scope']['requested_sources'] = ['OpenAlex']
        if mutation == 'library_only':p['steps'][0]['action'] = 'select_library_papers'
        if mutation == 'unsupported_prerequisite':p['steps'].insert(1, {'action': 'select_library_papers'})
        if mutation == 'screen_before_search':p['steps'][0], p['steps'][1] = p['steps'][1], p['steps'][0]
        db.execute('UPDATE research_plans SET payload=?', (json.dumps(value),))
    assert client.post(URL, json={'expected_revision': 1}).status_code == 422
    assert state['fetches'] == state['calls'] == []


@pytest.mark.parametrize('body', [{'expected_revision': 0}, {'expected_revision': 1, 'per_query': 21},
                                {'expected_revision': 1, 'api_key': 'browser-secret'}])
def test_request_validation(setup, body):
    client, state = setup
    assert client.post(URL, json=body).status_code == 422 and state['fetches'] == []


def test_missing_or_foreign_record(setup):
    client, _ = setup
    assert client.get(URL, params={'run_id': 'foreign'}).status_code == 404
    assert client.get('/api/v1/agent/plans/missing/search').status_code == 404


def test_dedup_versions_doi_and_invalid_metadata():
    queries = [{'number': 1, 'items': [paper(1), paper(2, published_doi='10.1234/X'),
               paper(99, identity_verified=False)]},
               {'number': 2, 'items': [paper(1, arxiv_version='2401.00001v2'), paper(3, published_doi='https://doi.org/10.1234/x')]}]
    result, counts = merge_candidates(queries)
    assert len(result) == 2 and all(p['query_numbers'] == [1, 2] for p in result)
    assert counts == {'duplicate_count': 2, 'skipped_count': 1}
    # 同名不同编号不进行标题猜测合并。
    result, _ = merge_candidates([{'number': 1, 'items': [paper(1), paper(2, title=paper(1)['title'])]}])
    assert len(result) == 2


def test_real_atom_adapter_and_codex_protocol(setup, cli, monkeypatch):
    from agent.llm_runtime import analysis_llm
    from tools import arxiv_search
    from test.test_arxiv_search import FEED
    client, state = setup
    seen = []
    def query(params):
        seen.append(params)
        return FEED.replace('2017-06-12', '2024-06-12').encode()
    monkeypatch.setattr(arxiv_search, 'query_arxiv', query)
    monkeypatch.setattr(research_search, 'search_arxiv_page', arxiv_search.search_arxiv_page)
    monkeypatch.setattr(research_search, 'analysis_llm', analysis_llm)
    _, task, run = start(client)
    assert task['status'] == 'succeeded' and len(run['candidates']) == 1 and run['duplicate_count'] == 1
    assert seen[0]['max_results'] == 10 and 'submittedDate:[202001010000 TO 202512312359]' in seen[0]['search_query']
    captured = json.loads(cli.read_text())
    assert captured['schema']['properties']['papers']
    assert 'private-search-key' not in cli.read_text()
