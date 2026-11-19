"""执行内显式评审与不可变版本关联；复用既有独立评审，不自动批量调用。"""
from copy import deepcopy
import json
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import claim_reviews, coverage, provenance, research_execution, reviews, runtime_bindings, tasks

router=APIRouter(prefix='/api/v1/agent/plans',tags=['执行评审关联'])


class StartReview(BaseModel):
    model_config=ConfigDict(extra='forbid')
    analysis_id:str=Field(min_length=1,max_length=100)
    resume_review_id:str|None=Field(default=None,max_length=100)


class LinkReviews(BaseModel):
    model_config=ConfigDict(extra='forbid')
    review_ids:list[str]=Field(min_length=1,max_length=5)
    research_questions:list[str]|None=Field(default=None,min_length=1,max_length=6)

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.review_ids))!=len(self.review_ids):raise ValueError('不能重复选择评审版本。')
        if self.research_questions is not None:
            self.research_questions=coverage.CoverageRequest(research_questions=self.research_questions).research_questions
        return self


def fixed_run(plan_id,execution_id):
    run=coverage.source_run(plan_id,execution_id)
    if not run.get('runtime_bindings'):
        raise RuntimeUnavailable('本执行没有固定角色配置，请使用新计划；不会套用当前默认模型。')
    selection=reviews.ReviewSelection(paper_ids=[p['paper_id'] for p in run['papers']],
        analysis_ids=[p['analysis']['analysis_id'] for p in run['papers']])
    reviews.prepare_selection(selection)
    return run,selection


@router.get('/{plan_id}/executions/{execution_id}/review-versions')
def versions(plan_id:str,execution_id:str):
    try:
        run,selection=fixed_run(plan_id,execution_id)
        binding=runtime_bindings.restore(run['runtime_bindings'],'claim_review')
        items=[]
        for paper in run['papers']:
            key=paper['analysis']['analysis_id']
            records=claim_reviews.list_reviews(key)['items']
            snapshot=reviews.load_analysis(key)
            items.append({'paper_id':paper['paper_id'],'title':paper['title'],'analysis_id':key,
                'claim_count':len(claim_reviews.build_items(snapshot)),
                'selected_review_id':paper.get('review',{}).get('review_id'),
                'reviews':[{k:r[k] for k in ('id','analysis_id','task','is_current','model_name','total','completed','quote_protocol','legacy_reused')} for r in records]})
        return {'items':items,'model_name':binding.runtime.model,'read_timeout':binding.runtime.read_timeout,
                'sdk_retries':binding.runtime.retries,'automatic_bulk_execution':False}
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.post('/{plan_id}/executions/{execution_id}/review-versions',status_code=202)
def start(plan_id:str,execution_id:str,body:StartReview,background_tasks:BackgroundTasks):
    try:
        run,selection=fixed_run(plan_id,execution_id)
        if body.analysis_id not in selection.analysis_ids:raise HTTPException(422,detail='只能评审本执行固定的分析版本。')
        bundle=run['runtime_bindings']
        runtime_bindings.restore(bundle,'claim_review')
        parent=tasks.get_task(run['task_id'])
        context={**parent,'runtime_bindings':bundle,'execution_review':True,'execution_id':execution_id}
        token=tasks.CURRENT_TASK.set(context)
        try:
            return claim_reviews.start_review(body.analysis_id,background_tasks,resume_review_id=body.resume_review_id,new_run=False)
        finally:tasks.CURRENT_TASK.reset(token)
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


def complete_reviews(ids,analysis_ids):
    records=[]
    for identifier in ids:
        detail=claim_reviews.get_review(identifier)
        if (detail['analysis_id'] not in analysis_ids or not detail['is_current'] or detail['task']['status']!='succeeded'
                or detail['completed']!=detail['total'] or not detail['total']):
            raise HTTPException(422,detail='只可关联本执行分析的当前、完整成功评审；模型标签不决定能否关联。')
        if any(r['analysis_id']==detail['analysis_id'] for r in records):raise HTTPException(422,detail='同一分析只能选择一个评审版本。')
        records.append(detail)
    return records


@router.post('/{plan_id}/executions/{execution_id}/review-links',status_code=202)
def link(plan_id:str,execution_id:str,body:LinkReviews,background_tasks:BackgroundTasks):
    try:
        run,selection=fixed_run(plan_id,execution_id)
        bundle=run['runtime_bindings']
        runtime_bindings.restore(bundle,'coverage')
        material=complete_reviews(body.review_ids,selection.analysis_ids)
        # 排序规范化，用户选择顺序不同仍复用同一输入。
        material.sort(key=lambda r:selection.analysis_ids.index(r['analysis_id']))
        ids=[r['id'] for r in material]
        frozen=coverage.review_material(ids,selection.analysis_ids)
        value={'kind':'execution_review_selection','plan_id':plan_id,'source_execution_id':execution_id,
            'plan_revision':run['revision'],'research_questions':body.research_questions or run['plan']['research_questions'],'selection':selection.model_dump(),'review_ids':ids,
            'review_fingerprints':{r['id']:fingerprint(r) for r in frozen},
            'analysis_fingerprints':{key:fingerprint(reviews.load_analysis(key)) for key in selection.analysis_ids},
            'runtime_bindings':bundle}
        key=fingerprint(value)[:40];value['id']='review_links_'+key
        try:
            saved=coverage.read(value['id'])
            if {k:v for k,v in saved.items() if k!='created_at'}!=value:raise RuntimeUnavailable('原评审选择内容已变化。')
            value=saved
        except HTTPException as exc:
            if exc.status_code!=404:raise
            value['created_at']=tasks.now()
        coverage.store(value)
        identifier='execution_reviews_'+key
        with tasks.connection() as db:
            research_execution.initialize(db);db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM research_executions WHERE id=?',(identifier,)).fetchone()
            if not row:
                derived=research_execution.read_execution(db,plan_id,execution_id)
                derived=deepcopy(derived)
                derived.update(id=identifier,selection_key=identifier,source_execution_id=execution_id,
                    review_selection_id=value['id'],review_after_analysis=False,
                    selection_kind='review_versions',status='succeeded',error=None,
                    created_at=tasks.now(),updated_at=tasks.now())
                by_analysis={r['analysis_id']:r for r in material}
                for paper in derived['papers']:
                    paper.pop('review',None)
                    chosen=by_analysis.get(paper['analysis']['analysis_id'])
                    if chosen:
                        counts={label:sum((v.get('review') or {}).get('verdict')==label for v in chosen['items'])
                                for label in ('supported','contradicted','insufficient','needs_review')}
                        paper['review']={'status':'succeeded','review_id':chosen['id'],'analysis_id':chosen['analysis_id'],
                            'task_id':chosen['task']['id'],'source_task_id':chosen['task']['id'],'reused':True,
                            'total_claims':chosen['total'],'completed_claims':chosen['completed'],
                            'verdict_counts':counts,'source_runtime_snapshot_id':chosen['task'].get('runtime_snapshot_id')}
                task=provenance.decorate({'id':uuid4().hex,'type':'research_execution','status':'succeeded',
                    'progress':100,'stage':'人工选择独立评审版本；未重跑分析，模型判断尚需人工核对',
                    'result_id':identifier,'error':None,'created_at':tasks.now(),'updated_at':tasks.now(),
                    **provenance.fields(run),'execution_id':identifier,'review_selection_id':value['id'],
                    'analysis_ids':selection.analysis_ids,
                    'execution_artifacts':[{'kind':'claim_review','id':r['id'],'relation':'reused',
                        'analysis_id':r['analysis_id'],'source_task_id':r['task']['id']} for r in material]},
                    source=run,parent_task_id=run['task_id'])
                derived.update(**provenance.fields(task));derived['task_id']=task['id']
                db.execute('INSERT INTO tasks VALUES (?,?)',(task['id'],json.dumps(task,ensure_ascii=False)))
                db.execute('INSERT INTO research_executions VALUES (?,?,?,?,?)',
                    (identifier,plan_id,run['revision'],identifier,json.dumps(derived,ensure_ascii=False)))
        response=coverage.start_coverage(plan_id,identifier,
            coverage.CoverageRequest(review_ids=ids,research_questions=value['research_questions']),background_tasks)
        return {**response,'execution_id':identifier,'review_selection_id':value['id']}
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


def verify_linked_run(run):
    """覆盖及报告继续使用被选择的评审内容，不读取被改写的检查点冒充原版本。"""
    record=coverage.read(run['review_selection_id'])
    ids=[p['analysis']['analysis_id'] for p in run['papers']]
    if (record.get('kind')!='execution_review_selection' or record['plan_id']!=run['plan_id']
            or record['plan_revision']!=run['revision'] or record['runtime_bindings']!=run['runtime_bindings']
            or record['selection']!={'paper_ids':[p['paper_id'] for p in run['papers']],'analysis_ids':ids}):
        raise RuntimeUnavailable('评审关联的执行或分析版本已变化。')
    if {key:fingerprint(reviews.load_analysis(key)) for key in ids}!=record['analysis_fingerprints']:
        raise RuntimeUnavailable('评审关联的原分析内容已变化。')
    complete_reviews(record['review_ids'],ids)
    material=coverage.review_material(record['review_ids'],ids)
    if {r['id']:fingerprint(r) for r in material}!=record['review_fingerprints']:
        raise RuntimeUnavailable('已关联评审内容已变化，不能冒充原评审版本继续生成结果。')
    if [p['review']['review_id'] for p in run['papers'] if p.get('review',{}).get('review_id')]!=record['review_ids']:
        raise RuntimeUnavailable('执行中的评审编号与固定选择不一致。')
