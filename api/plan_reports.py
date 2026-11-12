"""固定计划执行 → 比较 → 综述 → 现有导出；报告与全文分析进度分开保存。"""
from copy import deepcopy
import hashlib
import json
import sqlite3
from uuid import uuid4

from fastapi import APIRouter,BackgroundTasks,HTTPException
from pydantic import BaseModel,ConfigDict,Field,model_validator

from api import tasks,reviews
from api.research_execution import initialize as initialize_execution,read_execution,public_run
from agent.llm_runtime import model_name,model_endpoint

router=APIRouter(prefix='/api/v1/agent/plans',tags=['计划比较与综述'])
ACTIVE={'queued','running'}


class ReportRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    research_question:str=Field(min_length=1,max_length=1000)
    comparison_dimensions:list[str]=Field(min_length=1,max_length=10)

    @model_validator(mode='after')
    def normalize(self):
        self.research_question=self.research_question.strip()
        self.comparison_dimensions=[d.strip() for d in self.comparison_dimensions]
        if not self.research_question or any(not d or len(d)>150 for d in self.comparison_dimensions) or len(set(self.comparison_dimensions))!=len(self.comparison_dimensions):raise ValueError('请填写研究问题和不重复的比较维度（每项最多150字）。')
        return self


def initialize(db):
    initialize_execution(db)
    db.execute('''CREATE TABLE IF NOT EXISTS plan_reports (
        id TEXT PRIMARY KEY,plan_id TEXT NOT NULL,execution_id TEXT NOT NULL,
        input_key TEXT NOT NULL,payload TEXT NOT NULL)''')


def source_run(plan_id,execution_id):
    with tasks.connection() as db:
        initialize(db);run=public_run(db,read_execution(db,plan_id,execution_id))
    if run['status']!='succeeded' or not all(p['analysis']['status']=='succeeded' and p['analysis'].get('analysis_id') for p in run['papers']):raise HTTPException(422,detail='请先完成本轮所选论文的全文与分析。')
    actions=[s['action'] for s in run['plan']['steps']]
    if 'compare_papers' not in actions or 'generate_review' not in actions or actions.index('compare_papers')>actions.index('generate_review'):raise HTTPException(422,detail='该版本计划未按顺序包含比较与综述步骤，请调整计划后执行。')
    return run


def selection(run):
    return reviews.ReviewSelection(paper_ids=[p['paper_id'] for p in run['papers']],analysis_ids=[p['analysis']['analysis_id'] for p in run['papers']])


def context(run,dimensions):
    coverage=[]
    for p in run['papers']:
        snapshot=reviews.load_analysis(p['analysis']['analysis_id'])
        if snapshot is None or snapshot['paper']['id']!=p['paper_id']:raise HTTPException(422,detail='本轮分析版本已缺失或归属不符，请重新分析。')
        doc=snapshot['document'];reading=snapshot.get('reading_selection') or {}
        coverage.append(dict(paper_id=p['paper_id'],analysis_id=snapshot['id'],scope=snapshot.get('batch_summary',{}).get('scope','unknown'),selected_chunks=doc['selected_chunks'],total_chunks=doc['total_chunks'],note=reading.get('note','仅综合所选分析与证据，不代表完整领域查新。'),reading_selection=reading or None))
    return dict(plan_id=run['plan_id'],plan_revision=run['revision'],execution_id=run['id'],goal=run['goal'],comparison_dimensions=dimensions,reading_coverage=coverage)


def write(record,**changes):
    with tasks.connection() as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        saved=db.execute('SELECT payload FROM plan_reports WHERE id=?',(record['id'],)).fetchone()
        if not saved or json.loads(saved[0])['task_id']!=record['task_id']:raise RuntimeError('报告执行已替代')
        if tasks_state(db,record['task_id'])['status'] not in ACTIVE:raise RuntimeError('任务已结束')
        record.update(changes)
        db.execute('UPDATE plan_reports SET payload=? WHERE id=?',(json.dumps(record,ensure_ascii=False),record['id']))


def tasks_state(db,task_id):
    row=db.execute('SELECT payload FROM tasks WHERE id=?',(task_id,)).fetchone()
    return json.loads(row[0]) if row else {'status':'failed','error':{'code':'TASK_MISSING','message':'任务记录缺失'}}


def worker(record):
    try:
        tasks.update_task(record['task_id'],status='running',stage='正在核对本轮固定分析版本并生成比较矩阵',progress=10)
        # 每次重试都复核当前证据；沿用固定分析编号，不切换到最新版本。
        body=reviews.ReviewSelection.model_validate(record['selection'])
        matrix=reviews.compare_papers(body)
        write(record,comparison=matrix)
        if record.get('review_task_id'):
            previous=tasks.get_task(record['review_task_id'])
            if previous['status']=='succeeded' and previous.get('result_id'):
                result=reviews.get_review(previous['result_id'])
                if result.get('is_current') and result['analysis_ids']==record['selection']['analysis_ids'] and result.get('research_context')==record['context'] and result['topic']==record['research_question']:
                    write(record,status='succeeded',review_id=result['id'],error=None)
                    tasks.update_task(record['task_id'],status='succeeded',progress=100,stage='已恢复本轮已核验综述，可导出报告',result_id=result['id'],error=None)
                    return
        jobs=BackgroundTasks()
        response=reviews.create_review(reviews.ReviewRequest(**record['selection'],topic=record['research_question']),jobs,research_context=record['context'])
        write(record,review_task_id=response['task_id'])
        tasks.update_task(record['task_id'],stage='比较矩阵已保存，正在生成并核验综述',progress=30)
        for job in jobs.tasks:
            if job.is_async:raise RuntimeError('不支持的后台动作')
            job.func(*job.args,**job.kwargs)
        child=tasks.get_task(response['task_id'])
        if child['status']!='succeeded':
            error=child.get('error') or {'code':'REVIEW_RUNNING','message':'相同综述已有任务运行，待其完成后重试。','retryable':True}
            write(record,status='failed',error=error)
            tasks.update_task(record['task_id'],status='failed',stage='综述未完成，比较结果已保留',error=error);return
        result=reviews.get_review(child['result_id'])
        if not result.get('is_current'):raise HTTPException(409,detail='报告对应的原文已更新，请重新分析。')
        write(record,status='succeeded',review_id=result['id'],error=None)
        tasks.update_task(record['task_id'],status='succeeded',progress=100,stage='本轮比较与综述完成，可导出报告',result_id=result['id'],error=None)
    except Exception as exc:
        error={'code':'PLAN_REPORT_FAILED','message':exc.detail if isinstance(exc,HTTPException) and isinstance(exc.detail,str) else '比较或综述执行失败；已有分析和比较保留，请重试。','retryable':True}
        try:
            write(record,status='failed',error=error)
            tasks.update_task(record['task_id'],status='failed',stage='本轮报告未完成',error=error)
        except (sqlite3.Error,OSError,RuntimeError):pass


@router.get('/{plan_id}/executions/{execution_id}/comparison')
def comparison(plan_id:str,execution_id:str):
    run=source_run(plan_id,execution_id)
    matrix=reviews.compare_papers(selection(run))
    return dict(matrix,context=context(run,run['plan']['comparison_dimensions']))


@router.post('/{plan_id}/executions/{execution_id}/reports',status_code=202)
def start_report(plan_id:str,execution_id:str,body:ReportRequest,background_tasks:BackgroundTasks):
    run=source_run(plan_id,execution_id)
    # 在排队前检查分析有效性和归属；正式生成/发布前仍执行原有核验。
    selected=selection(run);reviews.prepare_selection(selected)
    info=context(run,body.comparison_dimensions)
    try:
        settings=reviews.model_settings();output_tokens=reviews.review_output_tokens()
    except ValueError as exc:raise HTTPException(422,detail=str(exc)) from None
    key=hashlib.sha256(json.dumps([execution_id,selected.model_dump(),body.model_dump(),model_name(),model_endpoint(),settings['method'],reviews.WRITING_PROFILE,output_tokens],sort_keys=True).encode()).hexdigest()
    try:
        with tasks.connection() as db:
            initialize(db);db.execute('BEGIN IMMEDIATE')
            rows=[json.loads(r[0]) for r in db.execute('SELECT payload FROM plan_reports WHERE execution_id=?',(execution_id,))]
            for r in rows:
                state=tasks_state(db,r['task_id'])
                if state['status'] in ACTIVE:
                    if r['input_key']!=key:raise HTTPException(409,detail='本轮已有综述正在生成，请完成后再修改研究问题。')
                    return {'task_id':r['task_id'],'report_id':r['id'],'reused':True}
            record=next((r for r in reversed(rows) if r['input_key']==key),None)
            if record and tasks_state(db,record['task_id'])['status']=='succeeded':return {'task_id':record['task_id'],'report_id':record['id'],'reused':True}
            timestamp=tasks.now();task_id=uuid4().hex
            if record is None:record=dict(id='plan_report_'+uuid4().hex,plan_id=plan_id,execution_id=execution_id,input_key=key,revision=run['revision'],selection=selected.model_dump(),research_question=body.research_question,context=info,created_at=timestamp,comparison=None,review_id=None)
            record.update(task_id=task_id,status='queued',error=None)
            task=dict(id=task_id,type='plan_report',paper_id=None,plan_id=plan_id,plan_revision=run['revision'],search_run_id=run['search_run_id'],execution_id=execution_id,status='queued',progress=0,stage='等待比较与综述',result_id=None,error=None,created_at=timestamp,updated_at=timestamp)
            db.execute('INSERT INTO tasks VALUES (?,?)',(task_id,json.dumps(task,ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO plan_reports VALUES (?,?,?,?,?)',(record['id'],plan_id,execution_id,key,json.dumps(record,ensure_ascii=False)))
        background_tasks.add_task(worker,record)
        return {'task_id':task_id,'report_id':record['id'],'reused':False}
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='无法保存计划报告任务。') from None


@router.get('/{plan_id}/executions/{execution_id}/reports')
def list_reports(plan_id:str,execution_id:str):
    with tasks.connection() as db:
        initialize(db);read_execution(db,plan_id,execution_id)
        items=[]
        for row in db.execute('SELECT payload FROM plan_reports WHERE plan_id=? AND execution_id=? ORDER BY rowid DESC',(plan_id,execution_id)):
            record=deepcopy(json.loads(row[0]));task=tasks_state(db,record['task_id']);record.update(status=task['status'],error=task['error'],task=task)
            if record.get('review_task_id'):record['review_task']=tasks_state(db,record['review_task_id'])
            items.append(record)
        return {'items':items}
