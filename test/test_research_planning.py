"""规划必须是真实模型调用、仅生成草稿，并保持约束来源、依赖与版本一致。"""
from contextlib import contextmanager
from copy import deepcopy
from datetime import date
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
import pytest

from agent.research_planner import ResearchPlan, ResearchPlanningError, build_planning_graph, validate_research_plan
from api import research_plans, tasks
from tools.venue_search.catalog import VENUES
from test.test_codex_model import cli

GOAL = '整理2020–2025年机器人视觉研究，分析3篇论文，比较精度和能耗并生成证据报告。'


def proposal():
    actions = ['search_papers', 'screen_papers', 'acquire_fulltext', 'analyze_papers', 'compare_papers', 'generate_review']
    return {'topic': '机器人视觉评测', 'objective': '比较机器人视觉任务的精度和能耗，形成带证据的报告。',
            'research_questions': ['视觉方法应如何同时比较精度和能耗？'],
            'scope': {'start_year': 2020, 'end_year': 2025, 'year_basis': 'explicit', 'year_evidence': '2020–2025年',
                      'paper_limit': 3, 'requested_paper_count': 3, 'count_evidence': '3篇论文',
                      'languages': [], 'requested_sources': [], 'inclusion_criteria': ['机器人视觉任务及其评价方法'], 'exclusion_criteria': []},
            'search_queries': ['robot vision accuracy energy benchmark', '机器人视觉 精度 能耗 评测'],
            'comparison_dimensions': ['精度', '能耗', '实验条件'],
            'deliverables': ['paper_list', 'paper_analysis', 'comparison_matrix', 'evidence_report'],
            'assumptions': [], 'clarification_questions': [],
            'steps': [{'number': i, 'action': action, 'title': f'计划步骤{i}',
                       'purpose': '按研究目标执行本步骤；已有有效分析可复用。', 'inputs': ['前序材料或研究目标'],
                       'outputs': ['待生成的研究材料'], 'depends_on': [i-1] if i > 1 else []}
                      for i, action in enumerate(actions, 1)]}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr('api.analyses.make_db', lambda *a: pytest.fail('规划不能访问论文数据库'))
    monkeypatch.setattr('agent.research_tools.make_research_tools', lambda *a: pytest.fail('规划不能执行文献工具'))
    for key, value in {'LLM_MODEL': 'test-model', 'LLM_API_KEY': 'private-plan-key', 'LLM_READ_TIMEOUT': '120',
                       'LLM_MAX_RETRIES': '0', 'STRUCTURED_OUTPUT_METHOD': 'json_schema'}.items():
        monkeypatch.setenv(key, value)
    app = FastAPI();app.include_router(research_plans.router);app.include_router(tasks.router)
    with TestClient(app) as result:
        yield result


@pytest.fixture
def model(monkeypatch):
    state = {'value': proposal(), 'calls': [], 'mode': 'valid'}
    class Model:
        def with_structured_output(self, schema, **kwargs):
            assert schema is ResearchPlan
            state['method'] = kwargs['method']
            return self
        def invoke(self, messages):
            state['calls'].append(messages)
            if state.get('exception'):
                raise state['exception']
            if state['mode'] == 'truncated':
                return {'raw': AIMessage(content='', response_metadata={'finish_reason': 'length'}), 'parsed': state['value']}
            if state['mode'] == 'json':
                return {'raw': AIMessage(content='```json\n' + json.dumps(state['value']) + '\n```'), 'parsed': None}
            return deepcopy(state['value'])
    @contextmanager
    def factory(**kwargs):
        state['settings'] = kwargs
        yield Model()
    monkeypatch.setattr(research_plans, 'analysis_llm', factory)
    return state


def start(client, goal=GOAL):
    response = client.post('/api/v1/agent/plans', json={'goal': goal})
    assert response.status_code == 202, response.text
    return client.get('/api/v1/tasks/' + response.json()['task_id']).json()


@pytest.mark.parametrize('method', ['json_schema', 'function_calling'])
def test_goal_to_plan_persists_without_executing_tools(client, model, monkeypatch, method):
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', method)
    task = start(client)
    assert task['type'] == 'research_planning' and task['status'] == 'succeeded'
    result = client.get('/api/v1/agent/plans/' + task['result_id']).json()
    assert result['status'] == 'draft' and result['revision'] == 1
    assert result['goal'] == GOAL and result['plan']['scope']['start_year'] == 2020
    assert result['capabilities']['execution_available'] is False
    assert result['capabilities']['search_sources'] == ['arxiv', *('openalex_'+s for s in VENUES)]
    assert len(model['calls']) == 1 and model['method'] == method
    request = json.loads(model['calls'][0][1][1])
    assert request['goal'] == GOAL and date.fromisoformat(request['today'])
    assert model['settings']['max_output_tokens'] == 6000
    assert 'private-plan-key' not in json.dumps(result) + json.dumps(task)
    assert client.post('/api/v1/agent/plans/' + result['id'] + '/execute').status_code == 404


def test_relative_years_are_explicitly_suggested_and_large_request_is_preserved(client, model):
    value = model['value'];current = date.today().year
    value['scope'].update(start_year=current-4, end_year=current, year_basis='assumed', year_evidence='',
                          requested_paper_count=20, count_evidence='20篇', paper_limit=5, requested_sources=['OpenAlex'])
    task = start(client, '梳理近几年机器人视觉研究，在OpenAlex寻找20篇并生成报告。')
    result = client.get('/api/v1/agent/plans/' + task['result_id']).json()
    assert result['plan']['scope']['requested_paper_count'] == 20
    notes = ' '.join(result['plan']['assumptions'])
    assert f'{current-4}–{current}' in notes and '原始要求为 20 篇' in notes and '尚未接通' in notes


@pytest.mark.parametrize('mutation,code', [
    ('future_dependency', 'PLAN_VALIDATION_FAILED'), ('wrong_year_evidence', 'PLAN_VALIDATION_FAILED'),
    ('edited_by_model', 'PLAN_VALIDATION_FAILED'), ('backwards_years', 'PLAN_VALIDATION_FAILED'),
    ('missing_dependency', 'PLAN_VALIDATION_FAILED'), ('unsupported_action', 'PLAN_STRUCTURE_FAILED'),
    ('completed_step', 'PLAN_STRUCTURE_FAILED'), ('missing_fields', 'PLAN_STRUCTURE_FAILED'),
])
def test_invalid_plans_are_never_published(client, model, mutation, code):
    value = model['value']
    if mutation == 'future_dependency':value['steps'][0]['depends_on'] = [6]
    if mutation == 'wrong_year_evidence':value['scope']['year_evidence'] = '2018–2022年'
    if mutation == 'edited_by_model':value['scope']['year_basis'] = 'edited'
    if mutation == 'backwards_years':value['scope'].update(start_year=2025, end_year=2020)
    if mutation == 'missing_dependency':value['steps'][-1]['depends_on'] = [1]
    if mutation == 'unsupported_action':value['steps'][0]['action'] = 'run_shell'
    if mutation == 'completed_step':value['steps'][0]['status'] = 'completed'
    if mutation == 'missing_fields':value.pop('scope')
    task = start(client)
    assert task['status'] == 'failed' and task['result_id'] is None and task['error']['code'] == code
    with tasks.connection() as db:
        if db.execute("select name from sqlite_master where name='research_plans'").fetchone():
            assert db.execute('select count(*) from research_plans').fetchone()[0] == 0


def test_edit_and_save_revalidates_and_checks_revision(client, model):
    task = start(client)
    url = '/api/v1/agent/plans/' + task['result_id']
    saved = client.get(url).json()
    value = saved['plan'];value['topic'] = '用户修改后的研究主题'
    value['scope'].update(start_year=2021, end_year=2026, year_basis='edited', year_evidence='', paper_limit=4)
    response = client.put(url, json={'plan': value, 'expected_revision': 1})
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2 and response.json()['edited_by_user'] is True
    assert response.json()['capabilities']['executable_actions'] == ['search_papers', 'screen_papers', 'acquire_fulltext', 'analyze_papers', 'compare_papers', 'generate_review']
    assert response.json()['plan']['scope']['start_year'] == 2021
    assert client.put(url, json={'plan': value, 'expected_revision': 1}).status_code == 409
    value['scope'].update(start_year=2026, end_year=2020)
    assert client.put(url, json={'plan': value, 'expected_revision': 2}).status_code == 422
    assert client.get(url).json()['revision'] == 2
    assert len(model['calls']) == 1


def test_minimal_search_only_plan_can_omit_report_steps(client, model):
    model['value']['deliverables'] = ['paper_list']
    model['value']['steps'] = model['value']['steps'][:2]
    assert start(client)['status'] == 'succeeded'


@pytest.mark.parametrize('mode', ['truncated', 'json'])
def test_truncation_and_raw_json(client, model, mode):
    model['mode'] = mode
    task = start(client)
    assert task['status'] == ('failed' if mode == 'truncated' else 'succeeded')


def test_same_goal_deduplicates_only_active_requests_and_handles_restart(client, model, monkeypatch):
    monkeypatch.setattr(research_plans, 'run_planning', lambda *a, **kw: None)
    first = client.post('/api/v1/agent/plans', json={'goal': GOAL}).json()
    assert client.post('/api/v1/agent/plans', json={'goal': GOAL}).json() == {'task_id': first['task_id'], 'reused': True}
    tasks.recover_interrupted_tasks()
    assert client.get('/api/v1/tasks/' + first['task_id']).json()['error']['code'] == 'SERVICE_RESTARTED'
    assert model['calls'] == []


@pytest.mark.parametrize('body', [{'goal': '  '}, {'goal': GOAL, 'api_key': 'browser-secret'}, {'goal': GOAL, 'execute': True}])
def test_bad_or_execution_override_request_rejected(client, model, body):
    assert client.post('/api/v1/agent/plans', json=body).status_code == 422
    assert model['calls'] == []


def test_assumption_notes_are_updated_instead_of_accumulating():
    value = proposal();value['scope'].update(year_basis='assumed', year_evidence='')
    first = validate_research_plan(ResearchPlan.model_validate(value), GOAL, today=date(2026,11,10))
    first.scope.paper_limit = 4
    second = validate_research_plan(first, GOAL, today=date(2026,11,10))
    third = validate_research_plan(second, GOAL, today=date(2026,11,10))
    assert second == third
    assert sum(note.startswith('原始要求为') for note in third.assumptions) == 1


def test_codex_cli_uses_same_planning_schema(client, cli, monkeypatch, tmp_path):
    path = tmp_path / 'plan.json';path.write_text(json.dumps(proposal()))
    monkeypatch.setenv('FAKE_CODEX_PLAN_PATH', str(path))
    monkeypatch.delenv('LLM_API_KEY', raising=False);monkeypatch.delenv('LLM_MODEL', raising=False)
    task = start(client)
    assert task['status'] == 'succeeded' and task['http_backend'] == 'codex'
