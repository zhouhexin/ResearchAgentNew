"""将已人工阅读的补检索候选接回原问题；只选择版本，不隐式阅读或改写历史。"""
from copy import deepcopy
import json
import os
import re
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import analyses, coverage, provenance, research_execution, reviews, runtime_bindings, supplement_search, tasks
from api.analysis_results import load_result
from storage.db_models import PaperRecord
from storage.repository import normalize_doi
from tools.arxiv_pdf import arxiv_id, normalize_title

router=APIRouter(prefix='/api/v1',tags=['补充阅读关联'])


class ReadingSelection(BaseModel):
    model_config=ConfigDict(extra='forbid')
    candidate_id:str=Field(min_length=1,max_length=100)
    analysis_id:str=Field(min_length=1,max_length=100)


class AdoptReadings(BaseModel):
    model_config=ConfigDict(extra='forbid')
    readings:list[ReadingSelection]=Field(min_length=1,max_length=3)
    reason:str=Field(min_length=1,max_length=1000)

    @model_validator(mode='after')
    def valid(self):
        self.reason=self.reason.strip()
        if not self.reason:raise ValueError('请说明补充材料与研究缺口的关系。')
        if len({r.candidate_id for r in self.readings})!=len(self.readings):raise ValueError('不能重复选择同一候选。')
        if len({r.analysis_id for r in self.readings})!=len(self.readings):raise ValueError('不能重复选择分析版本。')
        return self


def completed(request_id):
    detail=supplement_search.detail(request_id)
    if not detail['result']:raise HTTPException(422,detail='请先完成补检索与初筛，部分候选暂不能加入执行。')
    supplement_search.verify(detail['request'])
    with tasks.connection() as db:
        related=[json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
    parent=next((t for t in related if t.get('supplement_request_id')==request_id and t['status']=='succeeded'),None)
    if parent is None:raise HTTPException(422,detail='请先恢复补检索任务的成功状态。')
    result=coverage.read(detail['request']['coverage_result_id'])
    source=coverage.read(result['request_id'])
    return detail,parent,source


def same_paper(candidate,paper):
    if normalize_title(candidate['title'])!=normalize_title(paper.title):return False
    expected=candidate.get('arxiv_id')
    actual=arxiv_id(paper.url) or arxiv_id(paper.doi)
    if expected and actual and re.sub(r'v[1-9][0-9]*$', '', actual)==re.sub(r'v[1-9][0-9]*$', '', expected):return True
    doi=normalize_doi(candidate.get('published_doi'))
    return bool(doi and doi==normalize_doi(paper.doi))


def choices(detail):
    engine,Session=analyses.make_db(os.environ['DATABASE_URL'])
    try:
        with Session() as session:library=list(session.scalars(select(PaperRecord)))
    finally:engine.dispose()
    output=[]
    run=detail['result']['run']
    for candidate in run['candidates']:
        options=[]
        for paper in library:
            if same_paper(candidate,paper):
                options.extend({'paper_id':paper.id,'analysis_id':a['id'],'scope':a.get('batch_summary',{}).get('scope','unknown'),
                    'selected_chunks':a['document']['selected_chunks'],'total_chunks':a['document']['total_chunks']}
                    for a in analyses.read_analyses(load_paper_id=paper.id) if a.get('is_current'))
        output.append({'candidate_id':candidate['candidate_id'],'title':candidate['title'],
            'screening':run['screenings'].get(candidate['candidate_id']),'analyses':options})
    return output


@router.get('/supplement-search/{request_id}/readings')
def available(request_id:str):
    try:
        detail,_,source=completed(request_id)
        return {'items':choices(detail),'remaining_slots':max(0,5-len(source['selection']['paper_ids']))}
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.post('/supplement-search/{request_id}/readings',status_code=202)
def adopt(request_id:str,body:AdoptReadings,background_tasks:BackgroundTasks):
    try:
        detail,parent,source=completed(request_id)
        binding=runtime_bindings.restore(source['runtime_bindings'],'coverage')
        options={c['candidate_id']:c for c in choices(detail)}
        original_ids=source['selection']['paper_ids']
        if len(original_ids)+len(body.readings)>5:
            raise HTTPException(422,detail='当前比较/覆盖最多5篇，请另建计划选择材料；不会自动丢弃原论文。')
        additions=[]
        for item in body.readings:
            candidate=options.get(item.candidate_id)
            match=next((a for a in candidate['analyses'] if a['analysis_id']==item.analysis_id),None) if candidate else None
            if not match:raise HTTPException(422,detail='候选、论文与有效分析版本不匹配，请先收藏、下载并分析对应论文。')
            if match['paper_id'] in original_ids or any(a['paper_id']==match['paper_id'] for a in additions):
                raise HTTPException(422,detail='该论文已在原执行或本次选择中，请使用原论文补读入口，不重复加入。')
            snapshot=load_result(item.analysis_id)
            research_execution.verify_library_analysis(snapshot)
            additions.append({**match,'candidate_id':item.candidate_id,'analysis_sha256':fingerprint(snapshot)})
        selection=reviews.ReviewSelection(paper_ids=original_ids+[a['paper_id'] for a in additions],
            analysis_ids=source['selection']['analysis_ids']+[a['analysis_id'] for a in additions])
        reviews.prepare_selection(selection)
        identity=fingerprint([request_id,body.model_dump(),selection.model_dump(),[a['analysis_sha256'] for a in additions]])[:40]
        record={'id':'adopt_readings_'+identity,'kind':'supplement_reading_selection',
            'request_id':request_id,'search_result_sha256':fingerprint(detail['result']),
            'source_execution_id':source['execution_id'],'source_coverage_result_id':detail['request']['coverage_result_id'],
            'source_plan_id':source['plan_id'],'source_plan_revision':source['plan_revision'],
            'selection':selection.model_dump(),'additions':additions,'reason':body.reason,
            'source_search_created_at':detail['result']['run']['created_at'],'runtime_bindings':source['runtime_bindings']}
        try:
            saved=coverage.read(record['id'])
            if {k:v for k,v in saved.items() if k!='created_at'}!=record:
                raise RuntimeUnavailable('已保存的补充阅读选择与当前输入不一致。')
            record=saved
        except HTTPException as exc:
            if exc.status_code!=404:raise
            record['created_at']=tasks.now()
        coverage.store(record)
        identifier='execution_adopt_'+identity
        # 固定分析选择构成派生执行，不声称执行了下载、分析或辅助评审。
        with tasks.connection() as db:
            research_execution.initialize(db);db.execute('BEGIN IMMEDIATE')
            exists=db.execute('SELECT payload FROM research_executions WHERE id=?',(identifier,)).fetchone()
            if not exists:
                original=research_execution.read_execution(db,source['plan_id'],source['execution_id'])
                derived=deepcopy(original)
                derived.pop('review_selection_id',None)
                derived.update(id=identifier,selection_key=identifier,review_after_analysis=False,
                    source_execution_id=original['id'],source_coverage_result_id=record['source_coverage_result_id'],
                    reading_selection_id=record['id'],supplement_request_id=request_id,selection_kind='supplement_readings',
                    created_at=tasks.now(),updated_at=tasks.now(),status='succeeded',error=None)
                for a in additions:
                    snapshot=load_result(a['analysis_id']);doc=snapshot['document']
                    derived['papers'].append({'candidate_id':a['candidate_id'],'paper_id':a['paper_id'],'title':snapshot['paper']['title'],
                        'screening':deepcopy(options[a['candidate_id']]['screening']),
                        'fulltext':{'status':'succeeded','document_id':doc['id'],'reused':True},
                        'analysis':{'status':'succeeded','analysis_id':snapshot['id'],'reused':True,
                            'scope':a['scope'],'reading_selection':snapshot.get('reading_selection'),
                            'source_task_id':snapshot.get('source_task_id'),
                            'source_runtime_snapshot_id':snapshot.get('runtime_snapshot_id'),
                            'source_agent_version_id':snapshot.get('agent_version_id')}})
                task=provenance.decorate({'id':uuid4().hex,'type':'research_execution','status':'succeeded','progress':100,
                    'stage':'人工确认补充阅读版本；下载和分析为已有结果，新材料尚未关联辅助评审',
                    'result_id':identifier,'error':None,'created_at':tasks.now(),'updated_at':tasks.now(),
                    **provenance.fields(parent),'execution_id':identifier,'parent_task_id':parent['id'],
                    'reading_selection_id':record['id'],'source_execution_id':original['id'],
                    'analysis_ids':selection.analysis_ids,
                    'execution_artifacts':[{'kind':'analysis','id':key,'relation':'reused'} for key in selection.analysis_ids]},
                    source=parent,parent_task_id=parent['id'])
                derived.update(**provenance.fields(task));derived['task_id']=task['id']
                db.execute('INSERT INTO tasks VALUES (?,?)',(task['id'],json.dumps(task,ensure_ascii=False)))
                db.execute('INSERT INTO research_executions VALUES (?,?,?,?,?)',
                    (identifier,derived['plan_id'],derived['revision'],identifier,json.dumps(derived,ensure_ascii=False)))
        binding.check()
        response=coverage.start_coverage(source['plan_id'],identifier,
            coverage.CoverageRequest(research_questions=[q['question'] for q in source['input']['questions']]),background_tasks)
        return {**response,'execution_id':identifier,'reading_selection_id':record['id']}
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None
