"""P5 独立调度、显式发布、确定性灰度与回滚。仅作用于新回放任务。"""
from copy import deepcopy
import time
from uuid import uuid4
from sqlalchemy import select
from agent.runtime_snapshot import fingerprint,RuntimeUnavailable
from api import provenance
from storage.research_experiments import put,get
from storage.research_operations import EvaluationSchedule,ReplayReleaseChannel
from evaluation.task_quality import Strict
from pydantic import Field


class Schedule(Strict):
    id:str=Field(pattern='^[a-zA-Z0-9_-]{1,80}$')
    job_id:str
    interval_seconds:int=Field(ge=60,le=2678400)
    max_runs:int=Field(ge=1,le=10000)
    max_total_calls:int=Field(ge=1,le=100000)
    expires_at:float=Field(gt=0,allow_inf_nan=False)
    actor:str=Field(min_length=1)
    enabled:bool=False


def save_schedule(spec):
    job=get(spec.job_id,'task_replay_job')
    if job['split']=='holdout':raise RuntimeUnavailable('定时调度不读取留出任务，留出须单次独立授权。')
    if job.get('parent_experiment_id'):raise RuntimeUnavailable('实验子任务不能通过独立排程绕过父预算。')
    def save(session):
        row=session.get(EvaluationSchedule,spec.id,with_for_update=True)
        if row:raise RuntimeUnavailable('排程ID已存在；修改须新建版本并停用旧版本。')
        session.add(EvaluationSchedule(id=spec.id,payload={'spec':spec.model_dump(),'runs_reserved':0,'calls_reserved':0,'history':[]},next_at=time.time(),lease_until=0))
    provenance.transaction(save)
    return spec.id


def set_enabled(identifier,enabled,actor):
    def update(session):
        row=session.get(EvaluationSchedule,identifier,with_for_update=True)
        if not row:raise RuntimeUnavailable('排程不存在。')
        value=deepcopy(row.payload);value['spec']['enabled']=enabled
        value['history'].append({'action':'enable' if enabled else 'disable','actor':actor,'at':time.time()});row.payload=value
    provenance.transaction(update)


def claim_due(worker_id):
    now=time.time()
    def claim(session):
        candidates=session.scalars(select(EvaluationSchedule).where(EvaluationSchedule.next_at<=now,EvaluationSchedule.lease_until<=now).order_by(EvaluationSchedule.next_at).with_for_update(skip_locked=True)).all()
        for row in candidates:
            value=deepcopy(row.payload);spec=value['spec']
            if not spec['enabled'] or spec['expires_at']<=now or value['runs_reserved']>=spec['max_runs']:continue
            job=get(spec['job_id'],'task_replay_job')
            if value['calls_reserved']+job['max_calls']>spec['max_total_calls']:continue
            # 按单轮最大调用保守预扣；中断不退回未知用量，续排不重置预算。
            value['runs_reserved']+=1;value['calls_reserved']+=job['max_calls']
            token=uuid4().hex
            value['history'].append({'action':'dispatch_reserved','worker':worker_id,'token':token,'at':now,'calls':job['max_calls']})
            row.payload=value;row.lease_token=token;row.lease_until=now+job['total_seconds']+120
            row.next_at=now+spec['interval_seconds']
            return {'id':row.id,'token':token,'spec':spec,'dispatch':value['runs_reserved']}
        return None
    return provenance.transaction(claim)


def dispatch_once(worker_id,checkpoints):
    claim=claim_due(worker_id)
    if not claim:return {'dispatched':False}
    result=None;error=None
    try:
        from evaluation.replay_jobs import ReplayJob,prepare,run_job
        original=get(claim['spec']['job_id'],'task_replay_job')
        # 新调度轮次有独立作业标识，原配置固定；历史作业仍可查询。
        job_artifact=put('scheduled_replay_dispatch',{'schedule_id':claim['id'],'dispatch':claim['dispatch'],'job':original})
        prepare(ReplayJob.model_validate(original))
        # 每轮重复使用同一固定作业会只命中缓存；将轮次加入实际作业身份。
        job_id=put('task_replay_job',original|{'dispatch_identity':job_artifact})
        result=run_job(job_id,checkpoints)
    except Exception as exc:error={'code':getattr(exc,'code','SCHEDULE_EXECUTION_FAILED'),'error_type':type(exc).__name__}
    def finish(session):
        row=session.get(EvaluationSchedule,claim['id'],with_for_update=True)
        if not row or row.lease_token!=claim['token'] or row.lease_until<time.time():raise RuntimeUnavailable('排程租约已过期，旧执行不能覆盖新状态。')
        value=deepcopy(row.payload);value['history'].append({'action':'finished','dispatch':claim['dispatch'],'result_id':result,'error':error,'at':time.time()})
        row.payload=value;row.lease_token=None;row.lease_until=0
    provenance.transaction(finish)
    return {'dispatched':True,'schedule_id':claim['id'],'result_id':result,'error':error}


def publish(channel_id,decision_id,expected_revision,actor,canary_percent=0):
    if not 0<=canary_percent<=100:raise RuntimeUnavailable('灰度比例无效。')
    decision=get(decision_id,'evolution_gate_decision')
    if decision['decision']!='eligible_for_manual_release':raise RuntimeUnavailable('验证未通过，不允许发布候选。')
    from evaluation.task_replay import restore
    from evaluation.evolution import evaluate_pair
    refreshed=get(evaluate_pair(decision['baseline_result_id'],decision['candidate_result_id'],decision['policy_id'],decision['experiment_id']),'evolution_gate_decision')
    if refreshed['decision']!='eligible_for_manual_release':raise RuntimeUnavailable('发布前复核未通过。')
    binding=get(decision['candidate_binding_id'],'task_replay_binding');restore(binding)
    def update(session):
        row=session.get(ReplayReleaseChannel,channel_id,with_for_update=True)
        if not row:raise RuntimeUnavailable('先显式初始化兼容基线渠道。')
        value=deepcopy(row.payload)
        if value['revision']!=expected_revision:raise RuntimeUnavailable('渠道修订已变化，请刷新后发布。')
        experiment=get(decision['experiment_id'],'evolution_experiment')
        if experiment['baseline_binding_id']!=value['baseline']:
            raise RuntimeUnavailable('晋级依据的基线不属于此渠道，拒绝跨基线发布。')
        if refreshed['candidate_binding_id']!=decision['candidate_binding_id']:
            raise RuntimeUnavailable('发布前复核的候选与原决策不一致。')
        value['history'].append({'action':'release','previous':{k:value[k] for k in ('baseline','candidate','canary_percent')},'actor':actor,'decision_id':decision_id,'at':time.time()})
        value.update(revision=expected_revision+1,candidate=decision['candidate_binding_id'],canary_percent=canary_percent)
        row.payload=value;return value
    return provenance.transaction(update)


def initialize_channel(channel_id,binding_id,actor):
    from evaluation.task_replay import restore
    restore(get(binding_id,'task_replay_binding'))
    def create(session):
        if session.get(ReplayReleaseChannel,channel_id):raise RuntimeUnavailable('渠道已存在。')
        value={'revision':1,'baseline':binding_id,'candidate':None,'canary_percent':0,'history':[{'action':'initialize','actor':actor,'at':time.time()}]}
        session.add(ReplayReleaseChannel(id=channel_id,payload=value));return value
    return provenance.transaction(create)


def choose_binding(channel_id,new_task_id):
    def read(session):
        row=session.get(ReplayReleaseChannel,channel_id)
        if not row:raise RuntimeUnavailable('发布渠道不存在。')
        return deepcopy(row.payload)
    value=provenance.transaction(read)
    bucket=int(fingerprint([channel_id,new_task_id])[:8],16)%100
    chosen=value['candidate'] if value['candidate'] and bucket<value['canary_percent'] else value['baseline']
    return {'binding_artifact_id':chosen,'channel_id':channel_id,'channel_revision':value['revision'],'routing_bucket':bucket}


def rollback(channel_id,expected_revision,actor):
    from evaluation.task_replay import restore
    def update(session):
        row=session.get(ReplayReleaseChannel,channel_id,with_for_update=True)
        if not row or row.payload['revision']!=expected_revision:raise RuntimeUnavailable('渠道不存在或修订已变化。')
        value=deepcopy(row.payload)
        # 回滚前确认旧artifact仍可恢复；不假装旧代码在当前进程必然兼容。
        restore(get(value['baseline'],'task_replay_binding'))
        value['history'].append({'action':'rollback','actor':actor,'candidate':value['candidate'],'at':time.time()})
        value.update(revision=expected_revision+1,candidate=None,canary_percent=0);row.payload=value;return value
    return provenance.transaction(update)
