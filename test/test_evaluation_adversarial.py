"""模拟模型/数据库/时间下的对抗与崩溃恢复；禁止真实联网和生产写入。"""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import pytest
from agent.runtime_snapshot import fingerprint,RuntimeUnavailable
from evaluation import task_replay as replay,replay_jobs as jobs,evolution,operations
from evaluation.task_quality import Dataset,load
from storage.research_experiments import put,get,Checkpoints
from storage.research_operations import ReplayReleaseChannel
from storage.coverage_reports import CoverageArtifact
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace

ROOT=Path(__file__).resolve().parents[1]/'evaluation/examples/task_quality_v1'


@pytest.fixture
def replay_environment(workspace,monkeypatch,tmp_path):
    client,state,Session=workspace
    calls=[];behavior={'mode':'success'}
    class Model:
        def with_structured_output(self,*args,**kwargs):return self
        def invoke(self,messages):
            payload=json.loads(messages[1][1]);calls.append(payload)
            if behavior['mode']=='crash':raise SystemExit('controlled process crash')
            if behavior['mode']=='fail':raise RuntimeError('controlled model failure')
            m=payload['materials'][0]
            return {'answer':'只依据固定材料的回答。','findings':[{'statement':'仅在给定范围观察结果。','references':[{'material_id':m['material_id'],'span_ids':[m['spans'][0]['span_id']]}]}]}
    @contextmanager
    def llm(**kwargs):
        behavior['actual_runtime']=kwargs['runtime_snapshot'];yield Model()
    monkeypatch.setattr(replay,'analysis_llm',llm)
    dataset=load(ROOT/'dataset.json',Dataset)
    # 一个冻结案例便于精确核对尝试数和预算。
    dataset=Dataset.model_validate(dataset.model_dump()|{'cases':[dataset.cases[0].model_dump()]})
    dataset_id=put('task_dataset',dataset.model_dump())
    binding_id=put('task_replay_binding',replay.register(replay.Strategy()))
    return {'dataset_id':dataset_id,'binding_id':binding_id,'dataset':dataset,'calls':calls,'behavior':behavior,
            'path':tmp_path/'replay.sqlite','Session':Session}


def prepare(env,**kwargs):
    return jobs.prepare(jobs.ReplayJob(dataset_artifact_id=env['dataset_id'],binding_artifact_id=env['binding_id'],repetitions=1,max_calls=3,total_seconds=30,**kwargs))[0]


def test_crash_after_dispatch_does_not_allow_more_than_max_attempts(replay_environment):
    env=replay_environment;job=prepare(env,max_attempts=1)
    env['behavior']['mode']='crash'
    with pytest.raises(SystemExit):jobs.run_job(job,env['path'])
    assert len(env['calls'])==1
    journal=next(iter(Checkpoints(env['path']).items(job).values()))
    assert journal['invoke_started'] is True
    assert journal['runtime_snapshot_id']==env['behavior']['actual_runtime'].id
    env['behavior']['mode']='success'
    result=get(jobs.run_job(job,env['path']),'task_replay_result')
    assert len(env['calls'])==1, '崩溃后的未知尝试仍须计入每例max_attempts，不能再次调用模型。'
    assert result['run']['outcomes'][0]['status']=='interrupted'


def test_actual_fixed_runtime_budget_and_success_cache(replay_environment,monkeypatch):
    env=replay_environment;job=prepare(env)
    monkeypatch.setenv('LLM_MODEL','later-default')
    result=get(jobs.run_job(job,env['path']),'task_replay_result')
    assert result['run']['outcomes'][0]['status']=='succeeded'
    assert env['behavior']['actual_runtime'].model=='model-A'
    assert env['behavior']['actual_runtime'].retries==0
    assert 0<env['behavior']['actual_runtime'].read_timeout<=30
    assert result['checkpoint']['calls_reserved']==1
    again=get(jobs.run_job(job,env['path']),'task_replay_result')
    assert len(env['calls'])==1 and again['checkpoint']['calls_reserved']==1
    assert 'requirements' not in env['calls'][0] and 'human_judgments' not in env['calls'][0]


def test_annotation_cannot_replace_recorded_answer(replay_environment,monkeypatch):
    env=replay_environment;job=prepare(env)
    result_id=jobs.run_job(job,env['path']);raw=get(result_id,'task_replay_result')
    changed=deepcopy(raw['run']);changed['outcomes'][0]['answer']='伪造更好的回答'
    forged=put('task_run_annotations',{'source_result_id':result_id,'source_run_fingerprint':fingerprint(raw['run']),'run':changed})
    with pytest.raises(RuntimeUnavailable):evolution.run_artifact(forged)


def test_result_cannot_claim_another_runtime_version(replay_environment):
    env=replay_environment;job=prepare(env)
    result=get(jobs.run_job(job,env['path']),'task_replay_result')
    result['run']['runtime_snapshot_id']='unrelated-runtime'
    forged=put('task_replay_result',result)
    with pytest.raises(RuntimeUnavailable):evolution.run_artifact(forged)


def test_publish_rejects_decision_from_different_channel_baseline(replay_environment,monkeypatch):
    env=replay_environment
    baseline=env['binding_id'];other=put('task_replay_binding',replay.derive_binding(get(baseline),replay.Strategy(profile='conditions_first')))
    candidate=put('task_replay_binding',replay.derive_binding(get(baseline),replay.Strategy(profile='contradictions_first')))
    operations.initialize_channel('different-channel',other,'operator')
    experiment_id=put('evolution_experiment',{'baseline_binding_id':baseline,'candidate_binding_ids':[candidate]})
    decision={'decision':'eligible_for_manual_release','candidate_binding_id':candidate,'baseline_result_id':'left','candidate_result_id':'right','policy_id':'policy','experiment_id':experiment_id}
    decision_id=put('evolution_gate_decision',decision)
    monkeypatch.setattr(evolution,'evaluate_pair',lambda *args:decision_id)
    with pytest.raises(RuntimeUnavailable):operations.publish('different-channel',decision_id,1,'operator',100)
    with env['Session']() as session:
        assert session.get(ReplayReleaseChannel,'different-channel').payload['revision']==1


def test_child_experiment_cannot_bypass_shared_budget_via_direct_entry(replay_environment):
    env=replay_environment
    experiment_id=put('evolution_experiment',{'dataset_artifact_id':env['dataset_id'],'baseline_binding_id':env['binding_id'],'candidate_binding_ids':[],
        'max_rounds':1,'max_calls':1,'total_seconds':30})
    job=jobs.ReplayJob(dataset_artifact_id=env['dataset_id'],binding_artifact_id=env['binding_id'],parent_experiment_id=experiment_id)
    with pytest.raises(RuntimeUnavailable):jobs.run_job(jobs.prepare(job)[0],env['path'])
    assert env['calls']==[]


def test_unknown_material_pointer_is_never_published(replay_environment,monkeypatch):
    env=replay_environment
    @contextmanager
    def llm(**kwargs):
        class Model:
            def with_structured_output(self,*args,**kwargs):return self
            def invoke(self,*args):return {'answer':'伪造来源','findings':[{'statement':'错误发现','references':[{'material_id':'foreign','span_ids':['foreign:S1']}]}]}
        yield Model()
    monkeypatch.setattr(replay,'analysis_llm',llm)
    result=get(jobs.run_job(prepare(env),env['path']),'task_replay_result')
    assert result['run']['outcomes'][0]['status']=='failed'
    assert result['run']['outcomes'][0]['findings']==[]


def test_withdrawal_stops_future_dispatch_and_resume(replay_environment):
    env=replay_environment;job=prepare(env)
    put('evaluation_withdrawal',{'dataset_artifact_id':env['dataset_id'],'reason':'撤回测试','actor':'fake-operator'})
    with pytest.raises(RuntimeUnavailable,match='撤回'):jobs.run_job(job,env['path'])
    assert env['calls']==[]


def test_child_parent_budget_exhaustion_does_not_dispatch_model(replay_environment):
    env=replay_environment
    parent=put('evolution_experiment',{'scope':'fake parent'})
    spec=jobs.ReplayJob(dataset_artifact_id=env['dataset_id'],binding_artifact_id=env['binding_id'],parent_experiment_id=parent,repetitions=1,max_calls=1,total_seconds=30)
    job=jobs.prepare(spec)[0]
    def exhausted():raise RuntimeUnavailable('PARENT_BUDGET_EXHAUSTED')
    result=get(jobs.run_job(job,env['path'],reserve_parent=exhausted),'task_replay_result')
    assert env['calls']==[] and result['checkpoint']['state']=='stopped'
    assert result['run']['outcomes'][0]['status']=='interrupted'
    assert result['run']['outcomes'][0]['calls'] is None


def test_saved_attempt_result_cannot_be_overwritten_after_completion(replay_environment):
    env=replay_environment;job=prepare(env)
    jobs.run_job(job,env['path'])
    store=Checkpoints(env['path']);items=store.items(job);key=next(iter(items));value=deepcopy(items[key])
    token=store.acquire(job,60)
    value['outcome']['answer']='changed successful result'
    try:
        with pytest.raises(RuntimeUnavailable,match='不可覆盖'):store.finish_item(job,token,key,value)
    finally:store.release(job,token)


@pytest.fixture
def evaluation_api(replay_environment,workspace,monkeypatch):
    from api import evaluation_workspace as api
    client,_,_=workspace
    client.app.include_router(api.router)
    monkeypatch.setattr(api,'CHECKPOINTS',replay_environment['path'])
    from fastapi.testclient import TestClient
    return TestClient(client.app, base_url='http://127.0.0.1', client=('127.0.0.1',50000)),replay_environment


def test_api_does_not_execute_without_explicit_boolean_confirmation(evaluation_api):
    client,env=evaluation_api;job=prepare(env)
    for confirmation in (False,'true',1):
        response=client.post('/api/v1/evaluation/execute',json={'job_id':job,'confirm_model_calls':confirmation})
        assert response.status_code==422,response.text
    assert env['calls']==[]


def test_api_rejects_invalid_dataset_or_strategy_without_model_calls(evaluation_api):
    client,env=evaluation_api
    raw=env['dataset'].model_dump();raw['cases'][0]['permission']['allowed']=False
    assert client.post('/api/v1/evaluation/datasets',json={'content':raw}).status_code==422
    response=client.post('/api/v1/evaluation/bindings',json={'content':{'strategy':{'profile':'run_arbitrary_code'}}})
    assert response.status_code==422
    assert env['calls']==[]


def test_api_fake_execution_publishes_exact_job_and_result(evaluation_api):
    from api import evaluation_workspace as api
    client,env=evaluation_api;job=prepare(env)
    response=client.post('/api/v1/evaluation/execute',json={'job_id':job,'confirm_model_calls':True})
    assert response.status_code==202,response.text
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='succeeded',task
    saved=client.get('/api/v1/evaluation/artifacts/'+task['result_id'])
    assert saved.status_code==200 and saved.json()['job_id']==job
    assert saved.json()['semantic_review']=='not_collected' and len(env['calls'])==1


def test_api_parent_job_must_not_enqueue_as_standalone(evaluation_api):
    client,env=evaluation_api
    parent=put('evolution_experiment',{'scope':'fake-parent'})
    spec=jobs.ReplayJob(dataset_artifact_id=env['dataset_id'],binding_artifact_id=env['binding_id'],parent_experiment_id=parent)
    job=jobs.prepare(spec)[0]
    response=client.post('/api/v1/evaluation/execute',json={'job_id':job,'confirm_model_calls':True})
    assert response.status_code==409,response.text
    assert env['calls']==[]


def test_expired_response_does_not_publish_success(replay_environment,monkeypatch):
    env=replay_environment;job=prepare(env)
    original=replay.execute_case
    def late(case,binding,**kwargs):
        answer=original(case,binding,**kwargs)
        store=Checkpoints(env['path'])
        with store.db() as db:
            row=db.execute('SELECT payload FROM experiment_jobs WHERE id=?',(job,)).fetchone()
            payload=json.loads(row[0]);payload['deadline']=0
            db.execute('UPDATE experiment_jobs SET payload=? WHERE id=?',(json.dumps(payload),job))
        return answer
    # 模拟模型刚返回时期限到达，不能依赖调用前读取的旧deadline。
    monkeypatch.setattr(jobs,'execute_case',late)
    result=get(jobs.run_job(job,env['path']),'task_replay_result')
    assert result['run']['outcomes'][0]['status']!='succeeded'


def test_budget_stop_reports_specific_stable_code(replay_environment):
    env=replay_environment
    spec=jobs.ReplayJob(dataset_artifact_id=env['dataset_id'],binding_artifact_id=env['binding_id'],repetitions=2,max_calls=1,total_seconds=30)
    result=get(jobs.run_job(jobs.prepare(spec)[0],env['path']),'task_replay_result')
    assert len(env['calls'])==1
    assert result['checkpoint']['stop_reason']=='EXPERIMENT_BUDGET_EXHAUSTED'


def test_deadline_expiring_while_initializing_client_prevents_model_dispatch(replay_environment,monkeypatch):
    env=replay_environment;job=prepare(env)
    original=replay.analysis_llm
    @contextmanager
    def slow_client(**kwargs):
        store=Checkpoints(env['path'])
        with store.db() as db:
            row=db.execute('SELECT payload FROM experiment_jobs WHERE id=?',(job,)).fetchone()
            payload=json.loads(row[0]);payload['deadline']=0
            db.execute('UPDATE experiment_jobs SET payload=? WHERE id=?',(json.dumps(payload),job))
        with original(**kwargs) as model:yield model
    monkeypatch.setattr(replay,'analysis_llm',slow_client)
    result=get(jobs.run_job(job,env['path']),'task_replay_result')
    assert len(env['calls'])==0
    assert result['run']['outcomes'][0]['status']=='failed'
    assert result['run']['outcomes'][0]['error_code']=='EXPERIMENT_DEADLINE_EXCEEDED'
