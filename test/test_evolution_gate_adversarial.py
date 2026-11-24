"""以fake版本结果验证晋级规则；不运行模型、不进行真实发布。"""
from copy import deepcopy
from pathlib import Path
import pytest
from agent.runtime_snapshot import fingerprint
from evaluation import evolution
from evaluation.task_quality import Dataset,Run,Outcome,Finding,Citation,Judgment,load

ROOT=Path(__file__).resolve().parents[1]/'evaluation/examples/task_quality_v1'


@pytest.fixture
def gate(monkeypatch):
    original=load(ROOT/'dataset.json',Dataset)
    held=deepcopy(original.model_dump());held['cases']=held['cases'][:2]
    for c in held['cases']:c['split']='holdout';c['kind']='natural'
    dataset=Dataset.model_validate(held)
    development=Dataset.model_validate(original.model_dump()|{'cases':[original.cases[2].model_dump()|{'split':'development'}]})
    def outcome(case,success=True):
        m=case.materials[0]
        return Outcome(case_id=case.id,repetition=1,input_hash=case.input_hash,status='succeeded' if success else 'failed',
            answer='有条件的回答' if success else '',findings=[Finding(statement='材料中的限定发现',citations=[Citation(material_id=m.id,quote=m.text,start_char=0,end_char=len(m.text))])] if success else [],
            human_judgments=[Judgment(requirement_id='R1',verdict='met',assessor='fake-human',reason='用于验证程序边界的人工声明替身',context='independent')] if success else [],error_code=None if success else 'MODEL_TIMEOUT')
    fields={'kind':'recorded_run','dataset_sha256':dataset.sha256,'split':'holdout','mode':'frozen','repetitions':1,'code_fingerprint':'a'*64,'runtime_snapshot_id':'fake-runtime',
        'prompt_schema_fingerprint':'b'*64,'tool_config_fingerprint':'c'*64}
    left=Run(id='left',outcomes=[outcome(dataset.cases[0]),outcome(dataset.cases[1],False)],**fields)
    right=Run(id='right',outcomes=[outcome(c) for c in dataset.cases],**fields)
    policy=evolution.GatePolicy(revision='1',author='operator',frozen_before_experiment=True).model_dump()
    artifacts={'policy':policy,'experiment':{'policy_id':'policy','candidate_binding_ids':['candidate-binding'],'baseline_binding_id':'baseline-binding','dataset_artifact_id':'development'},'development':development.model_dump()}
    monkeypatch.setattr(evolution,'get',lambda identifier,kind=None:deepcopy(artifacts[identifier]))
    monkeypatch.setattr(evolution,'put',lambda kind,content:content)
    monkeypatch.setattr(evolution,'run_artifact',lambda identifier:(left if identifier=='left' else right,dataset,{'binding_artifact_id':'baseline-binding' if identifier=='left' else 'candidate-binding'}))
    return left,right,artifacts


def evaluate():return evolution.evaluate_pair('left','right','policy','experiment')


def test_actual_gain_with_independent_material_can_only_recommend_manual_release(gate):
    result=evaluate()
    assert result['decision']=='eligible_for_manual_release'
    assert result['production_changed'] is False and result['human_identities_authenticated'] is False


def test_invalid_source_is_hard_rejected_even_when_runtime_improves(gate):
    _,right,_=gate
    right.outcomes[0].findings[0].citations[0].quote='invented source'
    result=evaluate()
    assert result['decision']=='retain_baseline' and any('引用来源错误' in r for r in result['reasons'])


def test_independent_claim_cannot_be_replaced_by_model_influenced_labels(gate):
    _,right,_=gate
    right.outcomes[0].human_judgments[0].context='after_model_inspection'
    assert evaluate()['decision']=='retain_baseline'


def test_development_identity_leak_blocks_holdout_claim(gate):
    _,_,artifacts=gate
    dev=artifacts['development']['cases'][0]
    dev['group_id']='group-latency'
    result=evaluate()
    assert result['decision']=='retain_baseline' and any('同源' in r for r in result['reasons'])


def test_candidate_cannot_choose_looser_policy_after_experiment(gate):
    _,_,artifacts=gate
    artifacts['experiment']['policy_id']='another-policy'
    from agent.runtime_snapshot import RuntimeUnavailable
    with pytest.raises(RuntimeUnavailable):evaluate()
