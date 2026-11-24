"""P4 仅数据策略候选、固定规则晋级建议。禁止任意代码补丁和留出输入。"""
from pydantic import Field
from agent.runtime_snapshot import fingerprint, RuntimeUnavailable
from evaluation.task_quality import Strict, Dataset, Run, compare
from evaluation.task_replay import Strategy
from storage.research_experiments import put,get


class Candidate(Strict):
    hypothesis:str=Field(min_length=10,max_length=3000)
    rationale:str=Field(min_length=10,max_length=6000)
    parent_binding_id:str
    strategy:Strategy
    author:str=Field(min_length=1)


class GatePolicy(Strict):
    revision:str=Field(min_length=1)
    author:str=Field(min_length=1)
    frozen_before_experiment:bool
    max_success_rate_drop:float=Field(default=0.0,ge=0,le=0.2,allow_inf_nan=False)
    max_independent_met_rate_drop:float=Field(default=0.0,ge=0,le=0.2,allow_inf_nan=False)
    min_success_rate_gain:float=Field(default=0.05,gt=0,le=1,allow_inf_nan=False)
    min_met_rate_gain:float=Field(default=0.05,gt=0,le=1,allow_inf_nan=False)
    minimum_independent_groups:int=Field(default=2,ge=2)


def register_candidate(candidate):
    get(candidate.parent_binding_id,'task_replay_binding')
    return put('evolution_candidate',candidate.model_dump())


def failure_hypotheses(result_id):
    record=get(result_id,'task_replay_result')
    spec=get(record['job_id'],'task_replay_job')
    if spec['split']!='development':raise RuntimeUnavailable('失败归因只读取开发集，不读取留出/回归答案。')
    run=Run.model_validate(record['run'])
    # 只给数据策略候选建议，不输出实际原文或人工答案。
    failed=[{'case_id':o.case_id,'repetition':o.repetition,'error_code':o.error_code} for o in run.outcomes if o.status!='succeeded']
    suggestions=[{'profile':'conditions_first','hypothesis':'优先说明实验条件可能减少超范围概括。','status':'unverified'},
                 {'profile':'contradictions_first','hypothesis':'先检查相反证据可能减少遗漏冲突。','status':'unverified'}]
    return put('evolution_failure_hypotheses',{'source_result_id':result_id,'failures':failed,'suggestions':suggestions,
        'missing_cases_included_in_run_comparison':True,'automatically_applied':False})


def run_artifact(identifier):
    value=get(identifier)
    if 'source_result_id' in value:
        raw=get(value['source_result_id'],'task_replay_result')
        if fingerprint(raw['run'])!=value['source_run_fingerprint']:raise RuntimeUnavailable('人工判定来源指纹不符。')
        run=Run.model_validate(value['run'])
        source_run=Run.model_validate(raw['run'])
        def without_annotations(record):
            copy=record.model_dump()
            for item in copy['outcomes']:item['human_judgments']=[]
            return copy
        if without_annotations(run)!=without_annotations(source_run):
            raise RuntimeUnavailable('标注归档只能改变人工判定，不能改写原回答、状态、引用或版本。')
    else:raw=value;run=Run.model_validate(value['run'])
    spec=get(raw['job_id'],'task_replay_job')
    dataset=Dataset.model_validate(get(spec['dataset_artifact_id'],'task_dataset'))
    from evaluation.replay_jobs import permission_check
    permission_check(spec['dataset_artifact_id'],dataset)
    if not raw.get('actual_registry_checked'):raise RuntimeUnavailable('没有实际运行注册表校验记录。')
    from evaluation.task_replay import restore
    binding=get(spec['binding_artifact_id'],'task_replay_binding')
    restore(binding)
    from evaluation.task_replay import build_run
    source_run=Run.model_validate(raw['run'])
    from evaluation.task_quality import check_run
    check_run(dataset,source_run,allow_holdout=True)
    expected=build_run(dataset,binding,source_run.outcomes,spec['split'],spec['repetitions'],raw['job_id'])
    if expected.model_dump()!=source_run.model_dump():raise RuntimeUnavailable('结果声明的版本/输入不属于实际登记作业。')
    return run,dataset,spec


def evaluate_pair(baseline_id,candidate_id,policy_id,experiment_id):
    policy=GatePolicy.model_validate(get(policy_id,'gate_policy'))
    experiment=get(experiment_id,'evolution_experiment')
    if experiment['policy_id']!=policy_id or not policy.frozen_before_experiment:raise RuntimeUnavailable('晋级规则未在实验前固定。')
    left,dataset,left_spec=run_artifact(baseline_id);right,other,right_spec=run_artifact(candidate_id)
    if dataset.sha256!=other.sha256:raise RuntimeUnavailable('实验比较的数据集版本不一致。')
    if right_spec['binding_artifact_id'] not in experiment['candidate_binding_ids'] or left_spec['binding_artifact_id']!=experiment['baseline_binding_id']:
        raise RuntimeUnavailable('结果不属于预先登记的基线/候选。')
    comparison=compare(dataset,left,right,allow_holdout=True)
    reasons=[]
    if right.split!='holdout':reasons.append('重要改进尚缺独立留出材料验证。')
    selected=[c for c in dataset.cases if c.split==right.split]
    development=Dataset.model_validate(get(experiment['dataset_artifact_id'],'task_dataset'))
    dev_groups={c.group_id for c in development.cases}
    dev_papers={m.paper_key for c in development.cases for m in c.materials}
    dev_docs={m.document_sha256 for c in development.cases for m in c.materials}
    dev_texts={m.text_sha256 for c in development.cases for m in c.materials}
    if any(c.group_id in dev_groups or any(m.paper_key in dev_papers or m.document_sha256 in dev_docs or m.text_sha256 in dev_texts for m in c.materials) for c in selected):
        reasons.append('独立验证材料与开发实验同源，不得作为留出验证。')
    if any(c.kind!='natural' for c in selected):reasons.append('当前包含合成材料，不能作为真实质量晋级依据。')
    if len({c.group_id for c in selected})<policy.minimum_independent_groups:reasons.append('独立材料组数量不足。')
    metrics_a=comparison['left']['metrics'];metrics_b=comparison['right']['metrics']
    if metrics_b['source_checks'].get('failed',0):reasons.append('存在确定性引用来源错误，直接拒绝。')
    if any(r['error_code'] in {'PERMISSION_DENIED','SOURCE_OWNERSHIP_ERROR','FABRICATED_SOURCE'} for r in comparison['right']['rows']):reasons.append('存在关键权限/来源错误，直接拒绝。')
    for label,report in [('baseline',comparison['left']),('candidate',comparison['right'])]:
        if any(j['context']!='independent' for r in report['rows'] if r['status']=='succeeded' for j in r['human_judgments']):
            reasons.append(label+'成功项缺少完整独立人工判定。')
    a_success=metrics_a['statuses'].get('succeeded',0)/metrics_a['task_denominator'];b_success=metrics_b['statuses'].get('succeeded',0)/metrics_b['task_denominator']
    a_met=metrics_a['independent_human'].get('met',0)/metrics_a['requirement_denominator'];b_met=metrics_b['independent_human'].get('met',0)/metrics_b['requirement_denominator']
    if b_success-a_success < -policy.max_success_rate_drop:reasons.append('运行可靠性低于冻结的非劣阈值。')
    if b_met-a_met < -policy.max_independent_met_rate_drop:reasons.append('人工回答程度低于冻结的非劣阈值。')
    if b_success-a_success<policy.min_success_rate_gain and b_met-a_met<policy.min_met_rate_gain:reasons.append('未达到预先规定的实际收益。')
    return put('evolution_gate_decision',{'experiment_id':experiment_id,'policy_id':policy_id,'baseline_result_id':baseline_id,
        'candidate_result_id':candidate_id,'candidate_binding_id':right_spec['binding_artifact_id'],
        'decision':'eligible_for_manual_release' if not reasons else 'retain_baseline','reasons':reasons,
        'comparison':comparison,'production_changed':False,'human_identities_authenticated':False})


def execute_experiment(identifier,path):
    """数据策略在固定实现内运行；共享预算，不运行候选代码或接触留出答案。"""
    from storage.research_experiments import Checkpoints
    from evaluation.replay_jobs import ReplayJob,prepare,run_job
    config=get(identifier,'evolution_experiment')
    dataset=Dataset.model_validate(get(config['dataset_artifact_id'],'task_dataset'))
    if any(c.split!='development' for c in dataset.cases):raise RuntimeUnavailable('开发实验不接受留出/回归数据。')
    store=Checkpoints(path);store.create(identifier,config)
    token=store.acquire(identifier,config['total_seconds']+120)
    if not token:raise RuntimeUnavailable('实验已有执行者。')
    try:
        store.update(identifier,token,state='running')
        results={}
        for binding_id in [config['baseline_binding_id'],*config['candidate_binding_ids']]:
            cached=store.items(identifier).get(binding_id)
            if cached:results[binding_id]=cached;continue
            spec=ReplayJob(dataset_artifact_id=config['dataset_artifact_id'],binding_artifact_id=binding_id,
                split='development',repetitions=config['max_rounds'],max_calls=config['max_calls'],total_seconds=config['total_seconds'],parent_experiment_id=identifier)
            job_id,_,_=prepare(spec)
            result_id=run_job(job_id,path,reserve_parent=lambda:store.reserve(identifier,token))
            store.save_item(identifier,token,binding_id,{'result_id':result_id});results[binding_id]={'result_id':result_id}
            child=get(result_id,'task_replay_result')
            if child['checkpoint']['state']!='completed':
                store.update(identifier,token,state='stopped',stop_reason=child['checkpoint'].get('stop_reason') or 'child_incomplete')
                return put('evolution_experiment_results',{'experiment_id':identifier,'results':results,'checkpoint':store.create(identifier,config),'production_changed':False})
        store.update(identifier,token,state='completed',stop_reason='confirmed_experiment_rounds_attempted')
        return put('evolution_experiment_results',{'experiment_id':identifier,'results':results,'checkpoint':store.create(identifier,config),'production_changed':False})
    except Exception:
        store.update(identifier,token,state='stopped',stop_reason='experiment_interrupted')
        raise
    finally:store.release(identifier,token)
