"""真实业务函数+隔离SQLite/P0仓库，模型替身接收实际角色和参数。"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from evaluation import evolution_agent as agent
from evaluation.harness_contracts import EVOLUTION_PROMPT, AUXILIARY_REVIEW_PROMPT, SKILLS
from evaluation.evolution import GatePolicy
from evaluation.task_quality import Dataset
from evaluation.task_replay import Strategy
from storage.research_experiments import get, put, Checkpoints
from test.test_evaluation_adversarial import replay_environment
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace


@pytest.fixture
def auto(replay_environment, monkeypatch, tmp_path):
    env = replay_environment
    parent = get(env['binding_id'], 'task_replay_binding')
    control = agent.register_role(parent, 'controller')
    review = agent.register_role(parent, 'reviewer')
    policy = put('gate_policy', GatePolicy(revision='auto-test-1', author='fake-operator',
        frozen_before_experiment=True).model_dump())
    spec = agent.AutonomousSpec(dataset_artifact_id=env['dataset_id'], baseline_binding_id=env['binding_id'],
        controller_binding_id=control, reviewer_binding_id=review, policy_id=policy,
        max_calls=12, max_steps=6, max_candidates=1, total_seconds=120)
    state = {'calls':[], 'actions':['read_feedback','read_skill','propose_candidate','run_probe','compare_probe','finish'],
             'crash_once':False, 'hook':None, 'foreign_pointer':False, 'review_failure':False,
             'invalid_controller_once':False, 'review_error':None, 'baseline_supported':False}

    def candidate_id(body):
        for entry in body['history']:
            if entry['action']['tool'] == 'propose_candidate' and entry['observation']['ok']:
                return entry['observation']['result']['candidate_id']

    class Model:
        def __init__(self, runtime):
            self.runtime = runtime

        def with_structured_output(self, schema, **kwargs):
            self.schema = schema
            return self

        def invoke(self, messages):
            system, body = messages[0][1], json.loads(messages[1][1])
            state['calls'].append({'system':system,'body':body,'runtime':self.runtime,
                                   'schema':self.schema.__name__})
            if state['hook']:
                state['hook']()
            if state['crash_once']:
                state['crash_once'] = False
                raise SystemExit('controlled interrupted model')
            if system == EVOLUTION_PROMPT:
                if state['invalid_controller_once']:
                    state['invalid_controller_once']=False
                    return {'tool':'run_shell','reason':'无效动作必须计入预算而不能执行。'}
                step = len(body['history'])
                tool = state['actions'][min(step, len(state['actions'])-1)]
                row = {'tool':tool,'reason':'先根据实际反馈检查再进行有预算的验证。'}
                if tool == 'read_skill':
                    row['skill_id'] = 'experimental_conditions'
                elif tool == 'propose_candidate':
                    row['proposal'] = {'hypothesis':'先保留实验条件可能减少不当泛化错误。',
                        'rationale':'基于开发反馈提出通用条件核对策略，用同样材料进行受控比较。',
                        'strategy':{'instructions':'先保留研究条件，再说明所给材料可以支持的结果；没有依据时明确不足。',
                                    'skills':['experimental_conditions'],
                                    'max_input_chars':body['baseline_strategy']['max_input_chars'],
                                    'max_output_tokens':body['baseline_strategy']['max_output_tokens']}}
                elif tool in {'run_probe','compare_probe'} or tool == 'finish' and candidate_id(body):
                    row['candidate_id'] = candidate_id(body)
                return row
            material = body['materials'][0]
            references = [{'material_id':material['material_id'],
                           'span_ids':[material['spans'][0]['span_id']]}]
            findings = [{'statement':'材料给出了限定条件下的发现。','references':references}]
            if system == AUXILIARY_REVIEW_PROMPT:
                if state['review_error']:raise state['review_error']
                if state['review_failure']:raise RuntimeError('controlled auxiliary failure')
                return {'source_support':'supported' if state['baseline_supported'] or '候选' in body['answer'] else 'insufficient',
                    'answer_completeness':'answered','reason':'仅是用于验证调度和统计的模型判断替身。','findings':findings}
            if state['foreign_pointer']:
                references[0]['material_id'] = 'foreign-material'
            return {'answer':'候选回答，保留适用条件。' if '候选通用指令' in system else '基线回答。',
                    'findings':findings}

    @contextmanager
    def model_factory(**kwargs):
        yield Model(kwargs['runtime_snapshot'])

    monkeypatch.setattr(agent, 'analysis_llm', model_factory)
    return {'env':env,'spec':spec,'state':state,'path':tmp_path/'autonomous.sqlite3'}


def launch(auto, **overrides):
    spec = agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | overrides)
    job = agent.prepare(spec)
    result = get(agent.execute(job, auto['path']), 'autonomous_evolution_result')
    return job, result


def test_llm_tool_loop_executes_prompt_skill_candidate_and_independent_review(auto, monkeypatch):
    # 默认模型切换不应改变已经登记的任何角色请求。
    expected = get(auto['env']['binding_id'], 'task_replay_binding')['runtime']
    monkeypatch.setenv('LLM_MODEL','new-default-model')
    monkeypatch.setenv('LLM_BASE_URL','https://new-default.example/v1')
    job, result = launch(auto)
    assert result['checkpoint']['state'] == 'completed'
    assert result['checkpoint']['calls_reserved'] == len(auto['state']['calls']) == 10
    assert [e['action']['tool'] for e in result['trajectory']] == auto['state']['actions']
    assert len(result['comparisons']) == 1
    assert result['comparisons'][0]['recommend_for_independent_validation'] is True
    assert result['production_decision'] == 'retain_baseline' and result['production_changed'] is False
    assert result['human_gold'] == 'not_collected'
    assert sum(v['reserved_requests'] for v in result['usage_by_role'].values())==10
    assert result['usage_by_role']['reviewer']['reserved_requests']==2
    assert all(v['tokens'] is None and v['cost'] is None for v in result['usage_by_role'].values())
    requests = auto['state']['calls']
    assert all(r['runtime'].model == expected['model'] and r['runtime'].retries == 0
        and 0 < r['runtime'].read_timeout <= expected['read_timeout'] for r in requests)
    assert any(SKILLS['experimental_conditions']['instructions'] in r['system'] for r in requests)
    reviews = [r for r in requests if r['system'] == AUXILIARY_REVIEW_PROMPT]
    assert len(reviews) == 2 and all('候选通用指令' not in r['system'] for r in reviews)
    for r in requests:
        def check(value):
            if isinstance(value, dict):
                assert not set(value) & {'requirements','human_judgments','permission','gold_label'}
                for v in value.values():check(v)
            elif isinstance(value, list):
                for v in value:check(v)
        check(r['body'])
    # 同作业恢复成功结果不再次请求或重置预算。
    repeated = get(agent.execute(job, auto['path']), 'autonomous_evolution_result')
    assert repeated['checkpoint']['calls_reserved'] == 10 and len(auto['state']['calls']) == 10
    assert result['workflow_status']=='candidate_comparison_available' and result['comparison_count']==1


def test_shared_budget_includes_controller_executor_reviewer_and_resume(auto):
    job, result = launch(auto, max_calls=3)
    assert len(auto['state']['calls']) == result['checkpoint']['calls_reserved'] == 3
    assert result['checkpoint']['stop_reason'] == 'EXPERIMENT_BUDGET_EXHAUSTED'
    assert result['checkpoint']['state'] == 'partial'
    repeated = get(agent.execute(job, auto['path']), 'autonomous_evolution_result')
    assert repeated['checkpoint']['calls_reserved'] == 3 and len(auto['state']['calls']) == 3


def test_crash_attempt_is_not_dispatched_again_and_unknown_usage_stays_unknown(auto):
    auto['state']['crash_once'] = True
    job = agent.prepare(auto['spec'])
    with pytest.raises(SystemExit):agent.execute(job, auto['path'])
    assert len(auto['state']['calls']) == 1
    auto['state']['actions'] = ['read_feedback','finish']
    result = get(agent.execute(job, auto['path']), 'autonomous_evolution_result')
    assert len(auto['state']['calls']) == 3  # 两个新的controller请求，没有第二次executor请求。
    probe = get(result['probe_artifact_ids'][0], 'autonomous_probe')
    outcome = probe['run']['outcomes'][0]
    assert outcome['status'] == 'interrupted' and outcome['calls'] is None
    assert result['checkpoint']['calls_reserved'] == 3


def test_withdrawal_during_call_stops_before_more_dispatch(auto):
    auto['state']['hook'] = lambda: put('evaluation_withdrawal', {
        'dataset_artifact_id':auto['env']['dataset_id'],'actor':'fake-operator','reason':'撤回测试'})
    _, result = launch(auto)
    assert result['checkpoint']['state'] == 'partial'
    assert len(auto['state']['calls']) == 1
    assert not result['probe_artifact_ids'] and result['production_changed'] is False


def test_no_human_labels_even_when_model_auxiliary_score_is_perfect(auto):
    _, result = launch(auto)
    for identifier in result['probe_artifact_ids']:
        probe = get(identifier, 'autonomous_probe')
        assert all(not o['human_judgments'] for o in probe['run']['outcomes'])
        assert probe['metrics']['independent_human'] == {}
        assert probe['metrics']['unreviewed_requirements'] == 1
    assert result['independent_validation'] == 'not_performed'


@pytest.mark.parametrize('split',['holdout','regression'])
def test_foreign_split_is_rejected_before_model(auto,split):
    d=deepcopy(auto['env']['dataset'].model_dump())
    d['cases'][0]['split']=split
    spec=agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | {
        'dataset_artifact_id':put('task_dataset',Dataset.model_validate(d).model_dump())})
    with pytest.raises(RuntimeUnavailable,match='开发'):agent.prepare(spec)
    assert auto['state']['calls'] == []


def test_unauthorized_dataset_and_unfrozen_gate_cannot_start(auto):
    d=deepcopy(auto['env']['dataset'].model_dump());d['cases'][0]['permission']['allowed']=False
    spec=agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | {'dataset_artifact_id':put('task_dataset',d)})
    with pytest.raises(RuntimeUnavailable):agent.prepare(spec)
    policy=GatePolicy(revision='invalid',author='fake',frozen_before_experiment=False)
    spec=agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | {'policy_id':put('gate_policy',policy.model_dump())})
    with pytest.raises(RuntimeUnavailable,match='冻结'):agent.prepare(spec)
    assert auto['state']['calls'] == []


def test_tool_scope_and_candidate_limits_are_program_controlled(auto):
    job=agent.prepare(auto['spec']);session=agent.EvolutionSession(job,auto['path'])
    try:
        with pytest.raises(ValueError,match='案例'):
            session.dispatch(agent.Action(tool='read_material',reason='测试越界',case_id='../private',material_id='/etc/passwd'))
        with pytest.raises(ValueError,match='参数'):
            session.dispatch(agent.Action(tool='capabilities',reason='测试混入参数',candidate_id='foreign'))
        p=agent.Proposal(hypothesis='先核对条件可能减少不当概括。',rationale='基于开发反馈提出通用条件核对规则。',
            strategy=Strategy(instructions='保留研究条件和结果范围，再概括结论。',skills=['experimental_conditions']))
        with pytest.raises(ValueError,match='先读取'):
            session.dispatch(agent.Action(tool='propose_candidate',reason='测试先读技能',proposal=p))
        p.strategy=Strategy(instructions='保留研究条件和结果范围，再概括结论。')
        first=session.dispatch(agent.Action(tool='propose_candidate',reason='先验证通用候选',proposal=p))
        assert first==session.dispatch(agent.Action(tool='propose_candidate',reason='幂等登记同一候选',proposal=p))
        p=p.model_copy(deep=True);p.hypothesis='核对相反证据可能减少冲突遗漏。'
        with pytest.raises(ValueError,match='数量'):
            session.dispatch(agent.Action(tool='propose_candidate',reason='测试数量上限',proposal=p))
    finally:session.store.release(job,session.token)


@pytest.mark.parametrize('tool',['shell','write_file','publish','read_holdout','change_policy'])
def test_unavailable_tools_cannot_be_invented(tool):
    with pytest.raises(ValidationError):agent.Action(tool=tool,reason='不可用的工具不应可执行')


@pytest.mark.parametrize('instructions',['固定报告数值为0.777，所有问题都输出这一答案。',
    '```python\nprint(1)\n```','请求https://example.org读取私有评分。'])
def test_candidate_cannot_smuggle_numeric_answers_urls_or_code(instructions):
    with pytest.raises(ValidationError):Strategy(instructions=instructions)


def test_candidate_cannot_embed_known_case_or_material_identity(auto):
    case=auto['env']['dataset'].cases[0]
    p=agent.Proposal(hypothesis='先检查固定材料可减少错误。',rationale='依据开发样本提出通用假设并验证。',
        strategy=Strategy(instructions='遇到'+case.id+'时直接复用已知结论，不再核对。'))
    with pytest.raises(ValueError,match='编号'):agent.validate_patch(p,auto['env']['dataset'])


def test_invalid_source_output_remains_failed_and_in_denominator(auto):
    auto['state']['foreign_pointer']=True;auto['state']['actions']=['read_feedback','finish']
    _,result=launch(auto)
    probe=get(result['probe_artifact_ids'][0],'autonomous_probe')
    assert probe['metrics']['task_denominator']==1 and probe['metrics']['statuses']=={'failed':1}
    assert probe['auxiliary_failures']==1 and probe['auxiliary_answered_and_supported']==0
    assert probe['run']['outcomes'][0]['error_code']=='AUTONOMOUS_SOURCE_INVALID'


def test_source_violation_cannot_be_hidden_by_other_auxiliary_improvement(auto,monkeypatch):
    job=agent.prepare(auto['spec']);session=agent.EvolutionSession(job,auto['path'])
    metrics={'execution_success_fraction':{'numerator':0},'source_pass_fraction':{'numerator':0}}
    baseline={'metrics':metrics,'auxiliary_answered_and_supported':0,'auxiliary_failures':0,'run':{'outcomes':[]}}
    candidate={'metrics':metrics,'auxiliary_answered_and_supported':1,'auxiliary_failures':0,
        'run':{'outcomes':[{'case_id':'invalid-reference','error_code':'AUTONOMOUS_SOURCE_INVALID'}]}}
    monkeypatch.setattr(session,'allowed_probe',lambda key:baseline if key=='baseline' else candidate)
    try:
        result=session.comparison('candidate')
        assert result['critical_deterministic_error_cases']==['invalid-reference']
        assert result['recommend_for_independent_validation'] is False
    finally:session.store.release(job,session.token)


def test_role_configuration_must_match_fixed_executor(auto):
    parent=get(auto['spec'].controller_binding_id,'autonomous_role_binding')
    parent['runtime']['model']='forged-model'
    parent['runtime_snapshot_id']='runtime_'+fingerprint(parent['runtime'])
    bad=agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | {'controller_binding_id':put('autonomous_role_binding',parent)})
    with pytest.raises(RuntimeUnavailable):agent.prepare(bad)


def test_proposal_cannot_increase_its_resource_budget(auto):
    job=agent.prepare(auto['spec']);session=agent.EvolutionSession(job,auto['path'])
    try:
        base=Strategy.model_validate(json.loads(session.parent['version']['tools_json'])['strategy'])
        p=agent.Proposal(hypothesis='调整通用指令可能改善条件保留。',rationale='用于验证候选不能自行扩大调用资源预算。',
            strategy=base.model_copy(update={'max_output_tokens':base.max_output_tokens+1}))
        with pytest.raises(ValueError,match='资源预算'):
            session.dispatch(agent.Action(tool='propose_candidate',reason='测试资源边界',proposal=p))
        assert not any(k.startswith('candidate:') for k in session.entries())
    finally:session.store.release(job,session.token)


def test_failed_auxiliary_review_is_not_counted_as_correct(auto):
    auto['state']['review_failure']=True;auto['state']['actions']=['read_feedback','finish']
    _,result=launch(auto)
    probe=get(result['probe_artifact_ids'][0],'autonomous_probe')
    assert probe['metrics']['statuses']=={'succeeded':1}
    assert probe['auxiliary_failures']==1 and probe['auxiliary_answered_and_supported']==0
    assert probe['auxiliary_denominator']==1 and probe['semantic_human_gold']=='not_collected'
    assert result['checkpoint']['state']=='partial'
    assert result['checkpoint']['stop_reason']=='baseline_auxiliary_review_incomplete'
    assert result['workflow_status']=='baseline_only' and result['comparison_count']==0


def test_auxiliary_provider_failure_keeps_sanitized_diagnostic(auto):
    from agent.codex_model import execution_error
    auto['state']['review_error']=execution_error('unexpected private-upstream-secret failure')
    auto['state']['actions']=['read_feedback','finish']
    _,result=launch(auto)
    call=next(v for v in result['attempts'].values() if v['role']=='reviewer')
    assert call['error_code']=='CODEX_EXEC_FAILED'
    assert call['safe_error']['diagnostic']['reason']=='execution'
    assert 'private-upstream-secret' not in json.dumps(result,ensure_ascii=False)
    assert result['checkpoint']['state']=='partial'


def test_step_limit_stops_tool_loop_honestly(auto):
    _,result=launch(auto,max_steps=1)
    assert result['checkpoint']['state']=='partial' and result['checkpoint']['stop_reason']=='step_limit'
    assert len(auto['state']['calls'])==3 and len(result['trajectory'])==1


def test_controller_repairs_invalid_action_from_observation_with_shared_budget(auto):
    auto['state']['invalid_controller_once']=True
    auto['state']['actions']=['unused','read_feedback','read_skill','propose_candidate','run_probe','finish']
    _,result=launch(auto)
    assert result['checkpoint']['state']=='completed'
    assert result['trajectory'][0]['action']['tool']=='invalid_model_response'
    assert result['trajectory'][0]['observation']['code']=='AUTONOMOUS_RESPONSE_INVALID'
    assert result['checkpoint']['calls_reserved']==len(auto['state']['calls'])==10
    assert len(result['comparisons'])==1 and result['production_changed'] is False


def test_finish_without_reading_baseline_is_rejected_then_diagnosis_may_end_without_candidate(auto):
    auto['state']['actions']=['finish','read_feedback','finish']
    _,result=launch(auto,max_steps=3)
    assert result['trajectory'][0]['observation']['ok'] is False
    assert 'read_feedback' in result['trajectory'][0]['observation']['message']
    assert result['trajectory'][1]['action']['tool']=='read_feedback'
    assert result['checkpoint']['state']=='completed'
    assert result['checkpoint']['stop_reason']=='model_no_change_after_diagnosis'
    assert result['workflow_status']=='baseline_only'
    assert result['candidate_count']==result['comparison_count']==0


def test_proposed_candidate_must_be_evaluated_before_finish(auto):
    auto['state']['actions']=['read_feedback','read_skill','propose_candidate','finish']
    _,result=launch(auto,max_steps=4)
    assert result['trajectory'][-1]['observation']['ok'] is False
    assert 'run_probe' in result['trajectory'][-1]['observation']['message']
    assert result['checkpoint']['state']=='partial' and result['candidate_count']==1
    assert result['workflow_status']=='candidate_not_evaluated'


def test_exercise_mode_cannot_finish_with_only_baseline(auto):
    auto['state']['actions']=['read_feedback','finish']
    _,result=launch(auto,max_steps=2,exercise_candidate=True)
    assert result['checkpoint']['state']=='partial'
    assert result['experiment_mode']=='candidate_path_validation'
    assert result['trajectory'][-1]['observation']['ok'] is False
    assert '候选链路验收' in result['trajectory'][-1]['observation']['message']
    assert result['candidate_count']==result['comparison_count']==0


def test_exercise_mode_completes_real_candidate_path_without_authorizing_release(auto):
    _,result=launch(auto,exercise_candidate=True)
    assert result['checkpoint']['state']=='completed'
    assert result['experiment_mode']=='candidate_path_validation'
    assert result['candidate_count']==result['comparison_count']==1
    assert result['production_decision']=='retain_baseline' and result['production_changed'] is False
    assert all(r['body'].get('exercise_candidate') is True for r in auto['state']['calls'] if r['system']==EVOLUTION_PROMPT)


def test_exercise_mode_can_finish_comparison_without_any_gain(auto):
    auto['state']['baseline_supported']=True
    _,result=launch(auto,exercise_candidate=True)
    assert result['checkpoint']['state']=='completed'
    assert result['candidate_count']==result['comparison_count']==1
    assert result['comparisons'][0]['auxiliary_supported_and_answered_delta']==0
    assert result['candidates_for_independent_validation']==[]
    assert result['production_decision']=='retain_baseline' and result['production_changed'] is False


def test_identical_strategy_cannot_be_used_as_fake_candidate(auto):
    job=agent.prepare(auto['spec']);session=agent.EvolutionSession(job,auto['path'])
    try:
        base=Strategy.model_validate(json.loads(session.parent['version']['tools_json'])['strategy'])
        proposal=agent.Proposal(hypothesis='这是尚未验证的通用指令假设。',rationale='本测试检查候选必须实际改变运行策略。',strategy=base)
        with pytest.raises(ValueError,match='原样重复'):
            session.dispatch(agent.Action(tool='propose_candidate',reason='不能重复基线',proposal=proposal))
        assert not any(k.startswith('candidate:') for k in session.entries())
    finally:session.store.release(job,session.token)


def test_feedback_detail_filter_keeps_full_denominator_and_status_index(auto):
    data=auto['env']['dataset'].model_dump()
    second=deepcopy(data['cases'][0]);second['id']='second-development-case'
    data['cases'].append(second)
    spec=agent.AutonomousSpec.model_validate(auto['spec'].model_dump() | {'dataset_artifact_id':put('task_dataset',data)})
    job=agent.prepare(spec);session=agent.EvolutionSession(job,auto['path'])
    try:
        session.probe('baseline')
        case_id=data['cases'][0]['id']
        feedback=session.dispatch(agent.Action(tool='read_feedback',reason='只展开一例但保留全部统计',case_id=case_id))
        assert len(feedback['cases'])==1 and feedback['cases'][0]['case_id']==case_id
        assert len(feedback['case_status_index'])==2
        assert feedback['metrics']['task_denominator']==feedback['auxiliary_summary']['denominator']==2
        assert feedback['detail_scope']['full_task_denominator']==2
        summary=session.dispatch(agent.Action(tool='read_feedback',reason='总览不展开引文'))
        assert summary['cases']==[] and len(summary['case_status_index'])==2
        assert summary['detail_scope']['mode']=='summary'
        assert all(r['auxiliary_source_support']=='insufficient' and
            r['auxiliary_answer_completeness']=='answered' for r in summary['case_status_index'])
        assert all('human_judgments' not in r for r in feedback['cases'])
        with pytest.raises(ValueError,match='案例'):
            session.dispatch(agent.Action(tool='read_feedback',reason='拒绝其他集合',case_id='foreign-holdout'))
    finally:session.store.release(job,session.token)


def test_large_feedback_summary_and_detail_are_structured_without_cut_json(auto,monkeypatch):
    job=agent.prepare(auto['spec']);session=agent.EvolutionSession(job,auto['path'])
    try:
        probe=deepcopy(session.probe('baseline'))
        probe['run']['outcomes'][0]['answer']='长回答与原文。'*5000
        monkeypatch.setattr(session,'allowed_probe',lambda key:probe)
        summary=session.dispatch(agent.Action(tool='read_feedback',reason='先看完整总览'))
        assert len(json.dumps(summary,ensure_ascii=False))<12000
        case_id=session.dataset.cases[0].id
        action=agent.Action(tool='read_feedback',reason='按题展开',case_id=case_id)
        detail=session.dispatch(action)
        original={'step':0,'action':action.model_dump(),'observation':{'ok':True,'result':detail}}
        request=session.controller_input([deepcopy(original)])
        observation=request['history'][0]['observation']
        assert observation['details_omitted'] is True and 'preview' not in observation
        assert observation['summary']['metrics']['task_denominator']==1
        assert observation['cases'][0]['answer_chars']==len(probe['run']['outcomes'][0]['answer'])
        assert len(observation['cases'][0]['answer_preview'])==4000
        assert original['observation']['result']['cases'][0]['answer']==probe['run']['outcomes'][0]['answer']
    finally:session.store.release(job,session.token)


@pytest.mark.parametrize('limits',[{'max_steps':4},{'max_calls':8}])
def test_probe_cannot_spend_finish_step_or_call_and_resume_does_not_reset(auto,limits):
    job,result=launch(auto,**limits)
    assert result['checkpoint']['state']=='partial'
    assert result['checkpoint']['stop_reason']=='AUTONOMOUS_FINISH_BUDGET_INSUFFICIENT'
    assert result['candidate_count']==1 and result['comparison_count']==0
    assert result['usage_by_role']['executor']['reserved_requests']==1
    assert result['usage_by_role']['reviewer']['reserved_requests']==1
    calls=len(auto['state']['calls'])
    repeated=get(agent.execute(job,auto['path']),'autonomous_evolution_result')
    assert repeated['checkpoint']['stop_reason']=='AUTONOMOUS_FINISH_BUDGET_INSUFFICIENT'
    assert len(auto['state']['calls'])==calls==result['checkpoint']['calls_reserved']


def test_four_case_candidate_completes_with_original_budget_and_durable_progress(auto):
    data=auto['env']['dataset'].model_dump()
    data['cases']=[dict(deepcopy(data['cases'][0]),id=f'development-case-{i}') for i in range(4)]
    auto['state']['baseline_supported']=True
    job,result=launch(auto,dataset_artifact_id=put('task_dataset',data),
        max_steps=8,max_calls=24,exercise_candidate=True)
    assert result['checkpoint']['state']=='completed'
    assert result['checkpoint']['stop_reason']=='model_finished'
    assert len(auto['state']['calls'])==result['checkpoint']['calls_reserved']==22
    assert result['comparisons'][0]['task_denominator']==4
    assert result['comparisons'][0]['auxiliary_supported_and_answered_delta']==0
    assert result['candidates_for_independent_validation']==[]
    assert result['production_decision']=='retain_baseline'
    controller=[r['body'] for r in auto['state']['calls'] if r['system']==EVOLUTION_PROMPT]
    assert controller[1]['progress']['baseline_feedback_read'] is True
    assert controller[-1]['progress']['probed_candidate_ids']
    assert controller[-1]['budget']['finish_calls_reserved_required']==1
    # 即使旧历史从控制器上下文移除，持久动作仍可查询，不靠重新读取找回进度。
    session=agent.EvolutionSession(job,auto['path'])
    try:
        body=session.controller_input([])
        assert body['progress']['baseline_feedback_read'] is True
        assert body['progress']['candidate_ids']==body['progress']['probed_candidate_ids']
    finally:session.store.release(job,session.token)


def test_late_response_is_not_published_as_success(auto,monkeypatch):
    import storage.research_experiments as ledger
    def expire_actual_deadline():
        # 以实际预扣后持久化的deadline为准，避免把版本登记耗时误算进测试时间。
        with Checkpoints(auto['path']).db() as db:
            row=db.execute('SELECT payload FROM experiment_jobs').fetchone()
            deadline=json.loads(row[0])['deadline']
        monkeypatch.setattr(ledger.time,'time',lambda:deadline+0.001)
    auto['state']['hook']=expire_actual_deadline
    _,result=launch(auto)
    assert result['checkpoint']['stop_reason']=='EXPERIMENT_DEADLINE_EXCEEDED'
    assert len(auto['state']['calls'])==1 and not result['probe_artifact_ids']
    assert all(a['response'] is None for a in result['attempts'].values())


def test_prepare_only_requires_no_model_permission_or_execution(monkeypatch,tmp_path):
    import run_evolution_agent as cli
    monkeypatch.setenv('TEST_DATABASE_URL','postgresql://fake:secret@invalid.example/production')
    from evaluation.task_quality import load
    d=load(Path(__file__).resolve().parents[1]/'evaluation/examples/task_quality_v1/dataset.json',Dataset)
    with pytest.raises(RuntimeUnavailable,match='researchpilot_test'):
        cli.test_database(tmp_path,d)


def test_cli_requires_explicit_mode_and_protects_output_root():
    import run_evolution_agent as cli
    with pytest.raises(SystemExit):cli.main(['--dataset','missing.json','--output-dir','/private/tmp/wrong'])
    with pytest.raises(SystemExit):cli.main(['--dataset','missing.json','--output-dir','/private/tmp/wrong','--prepare-only'])
