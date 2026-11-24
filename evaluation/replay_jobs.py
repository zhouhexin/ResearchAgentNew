"""离线真实回放作业；持久租约、预扣预算、逐次版本记录，不经内部HTTP。"""
import json
import time
from pydantic import Field
from agent.runtime_snapshot import fingerprint, RuntimeUnavailable, RuntimeSnapshot
from evaluation.task_quality import Strict, Dataset, Outcome
from evaluation.task_replay import restore, execute_case, build_run
from storage.research_experiments import Checkpoints, put, get


class ReplayJob(Strict):
    dataset_artifact_id:str
    binding_artifact_id:str
    split:str=Field(default='development',pattern='^(development|regression|holdout)$')
    repetitions:int=Field(default=2,ge=1,le=20)
    max_calls:int=Field(default=20,ge=1,le=10000)
    total_seconds:int=Field(default=1800,ge=10,le=86400)
    max_attempts:int=Field(default=1,ge=1,le=4)
    holdout_authorized:bool=False
    dispatch_identity:str|None=None
    release_route:dict|None=None
    parent_experiment_id:str|None=None


def permission_check(dataset_id,dataset):
    from api import provenance
    from sqlalchemy import select
    from storage.coverage_reports import CoverageArtifact
    def read(session):
        return [r.payload for r in session.scalars(select(CoverageArtifact).where(CoverageArtifact.id.like('experiment_%')))]
    for envelope in provenance.transaction(read):
        value=envelope['content']
        if value.get('artifact_kind')=='evaluation_withdrawal' and value['content']['dataset_artifact_id']==dataset_id:
            raise RuntimeUnavailable('评测材料已撤回，拒绝后续模型调用。')
    if any(not c.permission.allowed for c in dataset.cases):raise RuntimeUnavailable('数据包中存在未授权材料，请单独提供获准材料包。')


def prepare(spec):
    dataset=Dataset.model_validate(get(spec.dataset_artifact_id,'task_dataset'))
    binding=get(spec.binding_artifact_id,'task_replay_binding')
    permission_check(spec.dataset_artifact_id,dataset)
    if spec.split=='holdout' and not spec.holdout_authorized:raise RuntimeUnavailable('留出回放未显式授权。')
    if not any(c.split==spec.split for c in dataset.cases):raise RuntimeUnavailable('集合为空。')
    restore(binding)
    identifier=put('task_replay_job',spec.model_dump())
    return identifier,dataset,binding


def run_job(identifier,path,reserve_parent=None):
    spec=ReplayJob.model_validate(get(identifier,'task_replay_job'))
    _,dataset,binding=prepare(spec)
    if spec.parent_experiment_id and reserve_parent is None:
        raise RuntimeUnavailable('实验子任务必须通过原父实验的共享预算执行。')
    store=Checkpoints(path)
    config=spec.model_dump()
    value=store.create(identifier,config)
    token=store.acquire(identifier,spec.total_seconds+60)
    if not token:raise RuntimeUnavailable('回放已有执行者持有租约。')
    try:
        store.update(identifier,token,state='running')
        cases=[c for c in dataset.cases if c.split==spec.split]
        for case in cases:
            for repetition in range(1,spec.repetitions+1):
                existing=[v for v in store.items(identifier).values() if v['outcome']['case_id']==case.id and v['outcome']['repetition']==repetition]
                if any(v['outcome']['status']=='succeeded' for v in existing):continue
                for attempt in range(len(existing)+1,spec.max_attempts+1):
                    permission_check(spec.dataset_artifact_id,dataset);restore(binding)
                    item_id=fingerprint([case.id,repetition,attempt])
                    pending=Outcome(case_id=case.id,repetition=repetition,input_hash=case.input_hash,status='interrupted',answer='',findings=[],human_judgments=[],error_code='ATTEMPT_INTERRUPTED')
                    reservation={'pending':True,'outcome':pending.model_dump(),'attempt':attempt,'runtime_snapshot_id':binding['runtime_snapshot_id'],'agent_version_id':binding['agent_version_id']}
                    value=store.reserve(identifier,token,reservation=(item_id,reservation))
                    if reserve_parent:
                        parent_budget=reserve_parent()
                        value['deadline']=min(value['deadline'],parent_budget['deadline'])
                    started=time.time()
                    # 剩余期限收紧到实际调用配置，导出并保存实际RuntimeSnapshot。
                    original=RuntimeSnapshot.model_validate(binding['runtime'])
                    runtime=RuntimeSnapshot.model_validate(original.model_dump()|{'read_timeout':min(original.read_timeout,max(0.001,value['deadline']-time.time())),'retries':0})
                    from api import provenance
                    from storage.runtime_versions import save_immutable
                    provenance.transaction(lambda session:save_immutable(session,runtime))
                    actual={**binding,'runtime':runtime.model_dump(),'runtime_snapshot_id':runtime.id}
                    def before_invoke():
                        store.check_deadline(identifier,token,value['deadline'])
                        store.mark_started(identifier,token,item_id,runtime.id)
                    try:
                        answer,findings=execute_case(case,actual,before_invoke=before_invoke)
                        permission_check(spec.dataset_artifact_id,dataset)
                        store.check_deadline(identifier,token,value['deadline'])
                        outcome=Outcome(case_id=case.id,repetition=repetition,input_hash=case.input_hash,status='succeeded',answer=answer,findings=findings,human_judgments=[],seconds=time.time()-started,calls=1)
                    except Exception as exc:
                        outcome=Outcome(case_id=case.id,repetition=repetition,input_hash=case.input_hash,status='failed',answer='',findings=[],human_judgments=[],error_code=getattr(exc,'code','TASK_REPLAY_FAILED'),seconds=time.time()-started,calls=1)
                    call={'pending':False,'outcome':outcome.model_dump(),'attempt':attempt,'runtime_snapshot_id':runtime.id,'agent_version_id':binding['agent_version_id']}
                    store.finish_item(identifier,token,item_id,call)
                    if outcome.status=='succeeded':break
        store.update(identifier,token,state='completed',stop_reason='confirmed_cases_attempted')
    except Exception as exc:
        store.update(identifier,token,state='stopped',stop_reason=getattr(exc,'code',type(exc).__name__))
    finally:store.release(identifier,token)
    attempts=store.items(identifier)
    latest={}
    for item in attempts.values():
        key=(item['outcome']['case_id'],item['outcome']['repetition'])
        if key not in latest or item['attempt']>latest[key]['attempt']:latest[key]=item
    # 所有尝试独立留存；最终使用累计已知调用和耗时，不把重试费用隐去。
    outcomes=[]
    for key,item in latest.items():
        values=[v['outcome'] for v in attempts.values() if (v['outcome']['case_id'],v['outcome']['repetition'])==key]
        outcomes.append(Outcome.model_validate(item['outcome']|{'calls':sum(v['calls'] for v in values) if all(v['calls'] is not None for v in values) else None,'seconds':sum(v['seconds'] for v in values) if all(v['seconds'] is not None for v in values) else None}))
    run=build_run(dataset,binding,outcomes,spec.split,spec.repetitions,identifier)
    result={'job_id':identifier,'run':run.model_dump(),'attempts':attempts,'checkpoint':store.create(identifier,config),
        'completed_at':time.time(),'semantic_review':'not_collected','actual_registry_checked':True}
    return put('task_replay_result',result)
