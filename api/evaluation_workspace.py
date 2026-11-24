"""P3/P4 操作入口；模型调用必须逐次明确确认，P5生产控制保留显式CLI。"""
from pathlib import Path
from fastapi import APIRouter,BackgroundTasks,HTTPException,Depends
from pydantic import BaseModel,ConfigDict,Field
from agent.runtime_snapshot import RuntimeUnavailable
from api import tasks,runtime_bindings
from storage.research_experiments import put,get
from api.local_access import require_local_evaluation_access

router=APIRouter(prefix='/api/v1/evaluation',tags=['离线研究评测'],
                 dependencies=[Depends(require_local_evaluation_access)])
CHECKPOINTS=Path(__file__).resolve().parents[1]/'outputs/experiments/workspace.sqlite3'


class Content(BaseModel):
    model_config=ConfigDict(extra='forbid')
    content:dict


class Execution(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    job_id:str=Field(min_length=1,max_length=100)
    confirm_model_calls:bool=False


def invalid(exc):
    if isinstance(exc,RuntimeUnavailable):return runtime_bindings.unavailable(exc)
    return HTTPException(422,detail={'code':'EVALUATION_INPUT_INVALID','message':'输入契约或材料不符合要求，请检查版本、授权与集合。'})


@router.get('/capabilities')
def capabilities():
    return {'live_model_replay':True,'modes':['frozen'],'candidate_type':'bounded_prompt_and_skill_strategy',
        'autonomous_prompt_evolution_cli':True,'arbitrary_code_execution':False,
        'default_execution':False,'scheduler_started_by_api':False,'production_business_routing':False,
        'access_mode':'loopback_only',
        'notice':'本工作区用于离线任务回放和实验；不替换现有科研配置，未完成人工标注时不计算语义准确率。'}


@router.post('/datasets',status_code=201)
def dataset(body:Content):
    try:
        from evaluation.task_quality import Dataset
        value=Dataset.model_validate(body.content)
        if any(not c.permission.allowed for c in value.cases):raise ValueError('unapproved')
        return {'artifact_id':put('task_dataset',value.model_dump())}
    except (ValueError,RuntimeUnavailable) as exc:raise invalid(exc) from None


@router.post('/datasets/export',status_code=201)
def export(body:Content):
    try:
        from evaluation.task_export import ExportCase,export_dataset
        value=body.content
        return export_dataset(value['id'],value['revision'],value['annotation_version'],[ExportCase.model_validate(v) for v in value['cases']])
    except (ValueError,KeyError,RuntimeUnavailable) as exc:raise invalid(exc) from None


@router.post('/bindings',status_code=201)
def binding(body:Content):
    try:
        from evaluation.task_replay import Strategy,register
        if set(body.content)-{'strategy','timeout'}:raise ValueError('unknown fields')
        return {'artifact_id':put('task_replay_binding',register(Strategy.model_validate(body.content.get('strategy',{})),body.content.get('timeout',120.0)))}
    except (ValueError,RuntimeUnavailable) as exc:raise invalid(exc) from None


@router.post('/jobs',status_code=201)
def job(body:Content):
    try:
        from evaluation.replay_jobs import ReplayJob,prepare
        return {'job_id':prepare(ReplayJob.model_validate(body.content))[0]}
    except (ValueError,RuntimeUnavailable) as exc:raise invalid(exc) from None


@router.get('/artifacts/{identifier}')
def artifact(identifier:str):
    try:return get(identifier)
    except RuntimeUnavailable as exc:raise invalid(exc) from None


@router.post('/execute',status_code=202)
def execute(body:Execution,background_tasks:BackgroundTasks):
    if not body.confirm_model_calls:raise HTTPException(422,detail='需明确确认本次模型调用及预算。')
    try:
        from evaluation.replay_jobs import ReplayJob,prepare
        spec=ReplayJob.model_validate(get(body.job_id,'task_replay_job'))
        if spec.parent_experiment_id:raise RuntimeUnavailable('实验子任务须通过原父实验执行，不能单独入队。')
        prepare(spec)
        binding=get(spec.binding_artifact_id,'task_replay_binding')
        task=tasks.create_task(task_type='evaluation_replay',stage='等待离线回放，预算及版本已固定',deduplicate=True,dedup_key=body.job_id,
            metadata={'evaluation_job_id':body.job_id,'agent_version_id':binding['agent_version_id'],
                'runtime_snapshot_id':binding['runtime_snapshot_id'],'model_name':binding['runtime']['model']})
        if not task.get('_reused'):background_tasks.add_task(worker,task['id'],body.job_id)
        return {'task_id':task['id'],'job_id':body.job_id,'reused':bool(task.get('_reused'))}
    except (ValueError,RuntimeUnavailable) as exc:raise invalid(exc) from None


def worker(task_id,job_id):
    try:
        from evaluation.replay_jobs import run_job
        tasks.update_task(task_id,status='running',stage='正在固定材料回放；人工要求不发送给模型')
        result=run_job(job_id,CHECKPOINTS)
        payload=get(result,'task_replay_result');state=payload['checkpoint']['state']
        from evaluation.task_quality import Dataset
        spec=get(job_id,'task_replay_job')
        dataset=Dataset.model_validate(get(spec['dataset_artifact_id'],'task_dataset'))
        expected=sum(c.split==spec['split'] for c in dataset.cases)*spec['repetitions']
        all_success=state=='completed' and len(payload['run']['outcomes'])==expected and all(o['status']=='succeeded' for o in payload['run']['outcomes'])
        tasks.update_task(task_id,status='succeeded' if all_success else 'failed',progress=100,result_id=result,
            stage='回放结束，结果已保存；语义判定待人工核对',
            error=None if all_success else {'code':'EVALUATION_INCOMPLETE','message':'包含失败/未完成项，预算和结果保留。','retryable':False})
    except Exception as exc:
        tasks.update_task(task_id,status='failed',stage='离线回放中断，已有检查点保留',error={'code':getattr(exc,'code','EVALUATION_FAILED'),'message':'请检查固定版本、预算与授权；不会静默替换模型。','retryable':False})


@router.post('/compare')
def compare_records(body:Content):
    try:
        from evaluation.evolution import run_artifact
        from evaluation.task_quality import compare
        left,dataset,_=run_artifact(body.content['baseline_id']);right,other,_=run_artifact(body.content['candidate_id'])
        if dataset.sha256!=other.sha256:raise ValueError('dataset mismatch')
        result=compare(dataset,left,right,allow_holdout=body.content.get('allow_holdout') is True)
        return {'artifact_id':put('task_comparison',result),'report':result}
    except (KeyError,ValueError,RuntimeUnavailable) as exc:raise invalid(exc) from None
