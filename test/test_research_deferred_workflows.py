"""P3–P5 受控边界回归。最初延后测试，现已按用户要求执行fake检查。"""
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from agent.runtime_snapshot import RuntimeUnavailable,fingerprint
from evaluation.task_quality import Dataset,load
from evaluation.task_replay import Strategy,payload,materialize
from evaluation.evolution import Candidate,GatePolicy
from evaluation.operations import Schedule
from evaluation.replay_jobs import ReplayJob
from storage.research_experiments import Checkpoints

ROOT=Path(__file__).resolve().parents[1]/'evaluation/examples/task_quality_v1'


def test_model_payload_never_contains_human_requirements_or_permission():
    dataset=load(ROOT/'dataset.json',Dataset);case=dataset.cases[0]
    value=payload(case,Strategy(profile='conditions_first'))
    assert set(value)=={'question','scope','materials','reading_profile','profile_guidance'}
    assert 'requirements' not in str(value) and 'synthetic-fixture-author' not in str(value)
    assert ''.join(s['text'] for s in value['materials'][0]['spans'])==case.materials[0].text


def test_pointer_materializer_restores_original_and_rejects_foreign_source():
    case=load(ROOT/'dataset.json',Dataset).cases[0];provided=payload(case,Strategy())
    material=provided['materials'][0];span=material['spans'][0]
    answer={'answer':'限定范围回答','findings':[{'statement':'示例结论','references':[{'material_id':material['material_id'],'span_ids':[span['span_id']]}]}]}
    _,findings=materialize(answer,provided)
    assert findings[0].citations[0].quote==case.materials[0].text
    bad=deepcopy(answer);bad['findings'][0]['references'][0]['material_id']='foreign'
    with pytest.raises(ValueError):materialize(bad,provided)
    bad=deepcopy(answer);bad['findings'][0]['references'][0]['span_ids']=['foreign-span']
    with pytest.raises(ValueError):materialize(bad,provided)


def test_data_strategy_cannot_modify_code_permission_or_gate():
    base={'hypothesis':'先核对条件可能减少外推错误。','rationale':'根据开发材料的错误类型提出待验证假设。','parent_binding_id':'binding','author':'operator','strategy':{'profile':'conditions_first'}}
    Candidate.model_validate(base)
    for field,value in [('code_patch','rm -rf'),('gate_policy',{}),('holdout_answers',{})]:
        with pytest.raises(ValidationError):Candidate.model_validate(base|{field:value})
    with pytest.raises(ValidationError):Strategy(profile='execute_python')


def test_checkpoint_budget_and_lease_do_not_reset_on_resume(tmp_path,monkeypatch):
    import storage.research_experiments as module
    now=[100.0];monkeypatch.setattr(module.time,'time',lambda:now[0])
    store=Checkpoints(tmp_path/'jobs.sqlite3');config={'max_calls':2,'total_seconds':30}
    store.create('job',config);token=store.acquire('job',60)
    assert token and store.acquire('job',60) is None
    assert store.reserve('job',token)['calls_reserved']==1
    store.release('job',token);second=store.acquire('job',60)
    assert store.reserve('job',second)['calls_reserved']==2
    with pytest.raises(RuntimeUnavailable,match='BUDGET'):store.reserve('job',second)
    now[0]=131
    with pytest.raises(RuntimeUnavailable,match='DEADLINE'):store.reserve('job',second)
    with pytest.raises(RuntimeUnavailable):store.save_item('job',token,'case',{})
    with pytest.raises(RuntimeUnavailable):store.create('job',config|{'max_calls':10})


def test_scheduler_configuration_is_disabled_by_default_and_bounded():
    value=Schedule(id='s1',job_id='job',interval_seconds=60,max_runs=2,max_total_calls=10,expires_at=9999999999.0,actor='operator')
    assert value.enabled is False
    with pytest.raises(ValidationError):Schedule.model_validate(value.model_dump()|{'interval_seconds':0})
    with pytest.raises(ValidationError):GatePolicy(revision='1',author='operator',frozen_before_experiment=True,max_success_rate_drop=-0.1)
    with pytest.raises(ValidationError):ReplayJob(dataset_artifact_id='d',binding_artifact_id='b',repetitions=0)


def test_response_schema_uses_actual_allowed_material_and_span_ids():
    from evaluation.task_replay import response_schema
    dataset=load(ROOT/'dataset.json',Dataset)
    provided=payload(dataset.cases[0],Strategy())
    schema=response_schema(provided).model_json_schema()
    import json
    rendered=json.dumps(schema)
    assert provided['materials'][0]['material_id'] in rendered
    assert provided['materials'][0]['spans'][0]['span_id'] in rendered


def test_material_hashes_never_change_during_pointer_selection():
    case=load(ROOT/'dataset.json',Dataset).cases[0]
    before=case.input_hash
    provided=payload(case,Strategy(profile='contradictions_first'))
    span=provided['materials'][0]['spans'][0]
    materialize({'answer':'有条件的描述','findings':[{'statement':'描述适用边界','references':[{'material_id':'M1','span_ids':[span['span_id']]}]}]},provided)
    assert case.input_hash==before


def test_lease_expiry_boundary_rejects_old_owner(tmp_path,monkeypatch):
    import storage.research_experiments as module
    now=[100.0];monkeypatch.setattr(module.time,'time',lambda:now[0])
    store=Checkpoints(tmp_path/'lease.sqlite');store.create('job',{'max_calls':1,'total_seconds':30})
    token=store.acquire('job',10)
    now[0]=110.0
    with pytest.raises(RuntimeUnavailable,match='租约'):store.reserve('job',token)
