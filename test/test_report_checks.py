"""实际HTTP组装、隔离业务库、原综述协议和固定分母；模型传输受控。"""
from copy import deepcopy
import json

import pytest
from fastapi import HTTPException

from api import coverage, execution_acceptance, report_checks, tasks
from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from evaluation.report_export import handoff
from evaluation.task_quality import Dataset
from storage.research_experiments import get, put
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, begin, post, defaults_b


@pytest.fixture
def report(workspace, tmp_path, monkeypatch):
    client, state, Session = workspace
    monkeypatch.setattr(report_checks, 'CHECKPOINTS', tmp_path/'checks.sqlite3')
    client.app.include_router(report_checks.router)
    client.app.include_router(execution_acceptance.router)
    client.app.include_router(coverage.router)
    planning, search = begin(client)
    response, execution = post(client, f'/api/v1/agent/plans/{planning["result_id"]}/executions', {
        'expected_revision':1, 'search_run_id':search['id'], 'candidate_ids':[c['candidate_id'] for c in search['candidates']],
        'review_after_analysis':True})
    assert execution['status']=='succeeded', execution
    base=f'/api/v1/agent/plans/{planning["result_id"]}/executions/{response["execution_id"]}'
    _, generated = post(client, base+'/reports', {'research_question':'方法怎样比较？', 'comparison_dimensions':['方法','证据']})
    assert generated['status']=='succeeded', generated
    value = report_checks.source(generated['result_id'])
    return client, state, Session, planning, base, value


def check(client, report_id, kind, **kwargs):
    return post(client, f'/api/v1/reviews/{report_id}/checks', dict(kind=kind, **kwargs))


def test_plan_to_report_independent_review_original_replay_and_acceptance(report, monkeypatch):
    client, state, _, planning, base, value = report
    original = deepcopy(value)
    generation = next(r for r in state['requests'] if r[1]=='ReviewDraft')
    defaults_b(monkeypatch)
    _, covered = post(client, base+'/coverage', {})
    assert covered['status']=='succeeded', covered
    _, audited = check(client, value['id'], 'independent_review')
    assert audited['status']=='succeeded', audited
    audit = report_checks.detail(value['id'], audited['result_id'])
    assert audit['complete'] and audit['denominator']==len(audit['items'])==1
    assert not audit['human_review_completed']
    assert all(c['text']==value['evidence_index'][c['ref_id']]['text'][c['start_char']:c['end_char']]
               for c in audit['items'][0]['result']['citations'])
    _, replayed = check(client, value['id'], 'original_replay')
    assert replayed['status']=='succeeded', replayed
    replay = report_checks.detail(value['id'], replayed['result_id'])
    assert replay['items'][0]['result']['mode']=='frozen_original_protocol'
    request = state['requests'][-1]
    assert json.loads(request[0].content)==json.loads(generation[0].content)
    assert request[0].url==generation[0].url
    assert request[0].extensions['timeout']==generation[0].extensions['timeout']
    assert report_checks.source(value['id'])==original
    selection={'report_id':value['id'],'coverage_id':covered['result_id'],
        'report_review_id':audit['id'],'report_replay_id':replay['id']}
    inspected = client.post(base+'/acceptance/inspect',json=selection).json()
    checks={v['key']:v['status'] for v in inspected['checks']}
    assert checks['report_independent_review']==checks['report_original_replay']=='passed'
    assert checks['plan_revision']=='passed'
    assert all(v['status']=='passed' for v in inspected['checks'])
    assert {v['kind'] for v in inspected['artifacts']} >= {'plan_revision','analysis','claim_review','coverage','report','independent_review','original_replay'}
    saved=client.post(base+'/acceptance',json={**selection,'expected_fingerprint':inspected['fingerprint'],
        'reviewer':'controlled-test','manual':[{'key':key,'verdict':'not_checked','reason':''} for key in sorted(execution_acceptance.MANUAL_KEYS)]})
    assert saved.status_code==201,saved.text
    saved=saved.json()
    assert not saved['evaluation_allowed'] and all(v['verdict']=='not_checked' for v in saved['manual'])
    assert client.get('/api/v1/acceptance/'+saved['id']).json()['inputs_unchanged']
    for fmt in ('md','json','csv'):
        assert client.get(f'/api/v1/reviews/{value["id"]}/files/{fmt}').status_code==200
    trace=client.get('/api/v1/tasks/'+planning['id']+'/trace').json()
    children={v['id']:v for v in trace['tasks']}
    assert children[audited['id']]['root_task_id']==planning['id']
    assert children[replayed['id']]['parent_task_id']==value['source_task_id']
    assert {a['id'] for a in trace['artifacts']} >= {audit['id'],replay['id'],value['id']}


def test_review_failure_resume_keeps_original_runtime_budget_and_history(report, monkeypatch):
    client,state,_,_,_,value=report
    state['fail_schema']='ReferenceClaimReview'
    response,failed=check(client,value['id'],'independent_review',max_calls=2)
    assert failed['status']=='failed' and failed['result_id'],failed
    first=coverage.read(failed['result_id'])
    assert first['failed']==first['denominator']==1
    state['fail_schema']=None
    defaults_b(monkeypatch)
    _,succeeded=check(client,value['id'],'independent_review',resume_request_id=response['request_id'],max_calls=100)
    assert succeeded['status']=='succeeded',succeeded
    second=coverage.read(succeeded['result_id'])
    assert second['usage']['calls_reserved']==2 and len(second['attempts'])==2
    assert coverage.read(first['id'])==first
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'
    count=len(state['requests'])
    _,cached=check(client,value['id'],'independent_review',resume_request_id=response['request_id'])
    assert cached['status']=='succeeded' and len(state['requests'])==count


def test_replay_rejects_legacy_and_source_tampering_before_call(report):
    client,state,_,_,_,value=report
    before=len(state['requests'])
    with tasks.connection() as db:
        changed=deepcopy(value);changed.pop('synthesis_request')
        db.execute('UPDATE reviews SET payload=? WHERE id=?',(json.dumps(changed),value['id']))
    response=client.post(f'/api/v1/reviews/{value["id"]}/checks',json={'kind':'original_replay'})
    assert response.status_code==409 and len(state['requests'])==before


def test_cross_source_span_rejected_and_conflict_preserved(report):
    from review.report_review import units, input_for, materialize
    _,_,_,_,_,value=report
    provided=input_for(value,units(value)[0],60000)
    result={'item_id':provided['sample_id'],'verdict':'supported','reason':'受控冲突供复核。',
        'evidence_assessments':[{'evidence_id':e['evidence_id'],'relation':'contradicts','reason':'受控理由。',
            'span_ids':[e['spans'][0]['span_id']]} for e in provided['evidence']]}
    saved=materialize(result,provided,value)
    assert saved['diagnostics']==['overall_supported_conflicts_with_evidence_assessment']
    assert saved['review']['verdict']=='supported'
    result['evidence_assessments'][0]['span_ids']=[provided['evidence'][1]['spans'][0]['span_id']]
    with pytest.raises(ValueError):materialize(result,provided,value)
    detailed=deepcopy(value)
    detailed['draft']['comparisons'][0]['paragraphs']=[
        {'text':'解释第一篇的实验条件。','evidence_refs':[provided['evidence'][0]['evidence_id']]},
        {'text':'解释第二篇的实验条件。','evidence_refs':[provided['evidence'][1]['evidence_id']]}]
    assert [u['unit_id'] for u in units(detailed)]==[
        'comparisons:0:claim','comparisons:0:paragraph:0','comparisons:0:paragraph:1']


def permission(allowed=True):
    return dict(allowed=allowed,basis='仅本测试合成材料，明确授权离线开发。',granted_by='controlled-test',granted_at='2026-11-24')


def requirements():
    return [dict(id='r1',description='保持实验条件和原文归属。',author='controlled-test',rationale='测试用户要求，不是人工答案。')]


def test_actual_problem_handoff_preserves_protocol_and_not_human_gold(report):
    from evaluation.task_replay import execute_case
    client,state,_,_,_,value=report
    state['verdict']='insufficient'
    _,task=check(client,value['id'],'independent_review')
    with pytest.raises(RuntimeUnavailable):handoff(value['id'],[task['result_id']],permission(False),requirements())
    result=handoff(value['id'],[task['result_id']],permission(),requirements())
    assert result['issues'] and not result['experiment_started'] and result['production_decision']=='retain_baseline'
    dataset=Dataset.model_validate(get(result['dataset_artifact_id'],'task_dataset'))
    case=dataset.cases[0]
    assert case.review_request==value['synthesis_request']
    assert case.source_feedback['issues'][0]['is_human_gold'] is False
    binding=get(result['baseline_binding_id'],'task_replay_binding')
    captured=[]
    answer,findings=execute_case(case,binding,invoke=lambda body,runtime:(captured.append(body) or value['draft']))
    assert captured==[value['synthesis_request']['payload']] and json.loads(answer)==value['draft']
    assert findings and all(c.quote in next(m.text for m in case.materials if m.id==c.material_id) for f in findings for c in f.citations)
    changed=case.model_dump();changed['review_request']=None
    assert case.input_hash!=type(case).model_validate(changed).input_hash


def test_no_issue_does_not_create_fake_evolution_problem(report):
    client,_,_,_,_,value=report
    _,task=check(client,value['id'],'independent_review')
    with pytest.raises(ValueError,match='没有实际'):
        handoff(value['id'],[task['result_id']],permission(),requirements())


def test_unknown_interrupted_request_not_silently_reissued(report, monkeypatch):
    from review import report_review
    client,state,_,_,_,value=report
    original_worker=report_checks.worker
    monkeypatch.setattr(report_checks,'worker',lambda task_id:None)
    response,queued=check(client,value['id'],'independent_review')
    monkeypatch.setattr(report_checks,'worker',original_worker)
    original_assess=report_review.assess
    def crash(*args,**kwargs):raise SystemExit('controlled unknown interruption')
    monkeypatch.setattr(report_review,'assess',crash)
    with pytest.raises(SystemExit):original_worker(queued['id'])
    tasks.recover_interrupted_tasks()
    monkeypatch.setattr(report_review,'assess',original_assess)
    before=len(state['requests'])
    _,resumed=check(client,value['id'],'independent_review',resume_request_id=response['request_id'])
    assert resumed['status']=='failed' and '中断' in resumed['error']['message']
    assert len(state['requests'])==before


@pytest.mark.parametrize('kind', ['independent_review', 'original_replay'])
def test_success_cache_is_revalidated_before_reuse(report, kind):
    from storage.research_experiments import Checkpoints
    client,state,_,_,_,value=report
    response,task=check(client,value['id'],kind)
    assert task['status']=='succeeded',task
    original=coverage.read(task['result_id'])
    store=Checkpoints(report_checks.CHECKPOINTS)
    with store.db() as db:
        key,payload=db.execute('SELECT key,payload FROM experiment_items WHERE job_id=?',(response['request_id'],)).fetchone()
        corrupted=json.loads(payload)
        if kind=='independent_review':
            corrupted['result']['citations'][0]['text']='tampered original'
        else:
            corrupted['result']['draft']['comparisons'][0]['evidence_refs']=['P999:E999']
        db.execute('UPDATE experiment_items SET payload=? WHERE job_id=? AND key=?',
                   (json.dumps(corrupted),response['request_id'],key))
    before=len(state['requests'])
    _,resumed=check(client,value['id'],kind,resume_request_id=response['request_id'])
    assert resumed['status']=='failed' and len(state['requests'])==before
    result=coverage.read(resumed['result_id'])
    assert not result['complete'] and result['failed']==result['denominator']==1
    assert '复验' in result['stop_error']['message']
    assert coverage.read(original['id'])==original


def test_budget_stop_keeps_partial_results_full_denominator_and_retry_slots(report):
    client,state,_,_,_,value=report
    # 同一冻结报告有三个独立待审单元，预算只容许第一项。
    changed=deepcopy(value)
    point=changed['draft']['comparisons'][0]
    point['paragraphs']=[{'text':'第一段解释。','evidence_refs':point['evidence_refs'][:1]},
                         {'text':'第二段解释。','evidence_refs':point['evidence_refs'][-1:]}]
    with tasks.connection() as db:
        db.execute('UPDATE reviews SET payload=? WHERE id=?',(json.dumps(changed),value['id']))
    response,task=check(client,value['id'],'independent_review',max_calls=1)
    assert task['status']=='failed' and task['result_id'],task
    result=coverage.read(task['result_id'])
    assert result['denominator']==3 and result['failed']==2 and not result['complete']
    assert [v['status'] for v in result['items']]==['succeeded','not_attempted','not_attempted']
    assert result['stop_error']['code']=='EXPERIMENT_BUDGET_EXHAUSTED'
    assert result['usage']['calls_reserved']==1 and result['usage']['actual_transport_calls'] is None
    before=len(state['requests'])
    _,again=check(client,value['id'],'independent_review',resume_request_id=response['request_id'],max_calls=100)
    retried=coverage.read(again['result_id'])
    assert len(state['requests'])==before and retried['denominator']==3 and retried['failed']==2


def test_native_report_protocol_enters_evolution_candidate_loop(report, monkeypatch, tmp_path):
    from contextlib import contextmanager
    from evaluation import evolution_agent as agent
    from evaluation.evolution import GatePolicy
    from evaluation.harness_contracts import EVOLUTION_PROMPT, AUXILIARY_REVIEW_PROMPT, SKILLS
    from review.synthesis import SYSTEM
    client,state,_,_,_,value=report
    state['verdict']='insufficient'
    _,checked=check(client,value['id'],'independent_review')
    exported=handoff(value['id'],[checked['result_id']],permission(),requirements())
    binding=get(exported['baseline_binding_id'],'task_replay_binding')
    controller=agent.register_role(binding,'controller');reviewer=agent.register_role(binding,'reviewer')
    policy=put('gate_policy',GatePolicy(revision='report-test-v1',author='controlled-test',frozen_before_experiment=True).model_dump())
    spec=agent.AutonomousSpec(dataset_artifact_id=exported['dataset_artifact_id'],baseline_binding_id=exported['baseline_binding_id'],
        controller_binding_id=controller,reviewer_binding_id=reviewer,policy_id=policy,max_calls=12,max_steps=6,total_seconds=300,exercise_candidate=True)
    recorded=[];actions=iter(['read_feedback','read_skill','propose_candidate','run_probe','finish'])
    class Model:
        def __init__(self,runtime):self.runtime=runtime
        def with_structured_output(self,schema,**kwargs):self.schema=schema;return self
        def invoke(self,messages):
            system,body=messages[0][1],json.loads(messages[1][1])
            recorded.append((system,body,self.schema.__name__,self.runtime))
            if system==EVOLUTION_PROMPT:
                tool=next(actions)
                result={'tool':tool,'reason':'依据已保存的综述问题验证通用候选。'}
                if tool=='read_feedback':result['case_id']=body['cases'][0]['case_id']
                if tool=='read_skill':result['skill_id']='experimental_conditions'
                if tool=='propose_candidate':result['proposal']={'hypothesis':'先核对各篇条件可能减少超范围归纳。',
                    'rationale':'根据独立综述审查的实际待复核项比较通用条件规则。',
                    'strategy':{**body['baseline_strategy'],'skills':['experimental_conditions']}}
                if tool in {'run_probe','finish'}:
                    result['candidate_id']=next(e['observation']['result']['candidate_id'] for e in body['history'] if e['action']['tool']=='propose_candidate')
                return result
            if system==AUXILIARY_REVIEW_PROMPT:
                material=body['materials'][0]
                return {'source_support':'insufficient','answer_completeness':'partial','reason':'受控辅助判断，未作人工裁定。',
                    'findings':[{'statement':'材料有范围限制。','references':[{'material_id':material['material_id'],
                        'span_ids':[material['spans'][0]['span_id']]}]}]}
            assert self.schema.__name__=='ReviewDraft' and body==value['synthesis_request']['payload']
            return value['draft']
    @contextmanager
    def factory(**kwargs):yield Model(kwargs['runtime_snapshot'])
    monkeypatch.setattr(agent,'analysis_llm',factory)
    identifier=agent.prepare(spec)
    result=get(agent.execute(identifier,tmp_path/'report_evolution.sqlite3'),'autonomous_evolution_result')
    assert result['checkpoint']['state']=='completed' and result['workflow_status']=='candidate_comparison_available',result
    assert result['candidate_count']==1 and result['comparison_count']==1
    assert result['production_decision']=='retain_baseline'
    executors=[v for v in recorded if v[2]=='ReviewDraft']
    assert len(executors)==2 and executors[0][0]==SYSTEM
    assert SKILLS['experimental_conditions']['instructions'] in executors[1][0]
    assert all(v[3].model=='model-A' and v[3].retries==0 for v in recorded)
    feedback=next(e for e in result['trajectory'] if e['action']['tool']=='read_feedback')['observation']['result']
    assert feedback['cases'][0]['recorded_source_problem']['issues']
    assert feedback['cases'][0]['recorded_source_problem']['semantic_human_gold']=='not_collected'
