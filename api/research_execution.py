"""计划版本下的人工选文 → 全文 → 全量分析；复用既有任务和证据校验。"""
from copy import deepcopy
import hashlib
import json
import logging
import sqlite3
from uuid import uuid4
from typing import Literal
from agent.fast_reading import VERSION as FAST_VERSION

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api import analyses, fulltext, papers, tasks
from api.research_search import initialize as initialize_search, read_plan, public_run
from api.schemas import PaperBatchRequest

router=APIRouter(prefix='/api/v1/agent/plans',tags=['计划全文与分析执行'])
logger=logging.getLogger(__name__)
ACTIVE={'queued','running'}


class ExecutionRequest(BaseModel):
    model_config=ConfigDict(extra='forbid')
    expected_revision:int=Field(ge=1)
    search_run_id:str=Field(min_length=1,max_length=100)
    candidate_ids:list[str]=Field(min_length=2,max_length=5)
    analysis_scope:Literal['full','fast']='full'

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.candidate_ids))!=len(self.candidate_ids):raise ValueError('不能重复选择同一候选论文。')
        return self


def initialize(db):
    initialize_search(db)
    db.execute('''CREATE TABLE IF NOT EXISTS research_executions (
        id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
        selection_key TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS execution_plan ON research_executions(plan_id,revision)')


def read_execution(db,plan_id,execution_id):
    row=db.execute('SELECT payload FROM research_executions WHERE id=? AND plan_id=?',(execution_id,plan_id)).fetchone()
    if not row:raise HTTPException(404,detail='该计划下不存在此执行记录。')
    return json.loads(row[0])


def public_execution(db,run):
    run=public_run(db,deepcopy(run))
    for item in run['papers']:
        for step in ('fulltext','analysis'):
            child=item[step].get('task_id')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(child,)).fetchone() if child else None
            item[step]['task']=json.loads(row[0]) if row else None
    return run


def checkpoint(run,*,finish=False):
    with tasks.connection() as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        saved=read_execution(db,run['plan_id'],run['id'])
        row=db.execute('SELECT payload FROM tasks WHERE id=?',(run['task_id'],)).fetchone()
        if saved['task_id']!=run['task_id'] or not row:raise RuntimeError('执行已被替代')
        task=json.loads(row[0])
        if task['status'] not in ACTIVE:raise RuntimeError('执行任务已结束')
        done=sum(p['analysis']['status']=='succeeded' for p in run['papers'])
        run.update(updated_at=tasks.now(),status=('succeeded' if done==len(run['papers']) else 'failed') if finish else 'running')
        error=None if not finish or done==len(run['papers']) else {'code':'PLAN_EXECUTION_INCOMPLETE','message':'部分论文未完成，已完成结果保留。可继续未完成部分。','retryable':True}
        run['error']=error
        task.update(status=run['status'],error=error,updated_at=run['updated_at'],
                    progress=100 if run['status']=='succeeded' else int(100*sum(p[s]['status']=='succeeded' for p in run['papers'] for s in ('fulltext','analysis'))/(2*len(run['papers']))),
                    stage='本轮全文与分析完成；比较和综述尚未执行' if run['status']=='succeeded' else '部分论文未完成，可继续' if finish else run.get('stage','正在执行全文与分析'),
                    result_id=run['id'] if finish else None)
        db.execute('UPDATE research_executions SET payload=? WHERE id=?',(json.dumps(run,ensure_ascii=False),run['id']))
        db.execute('UPDATE tasks SET payload=? WHERE id=?',(json.dumps(task,ensure_ascii=False),run['task_id']))


def documents(paper_id):
    return [d for d in papers.list_paper_documents(paper_id) if d.get('chunk_count',0)>0]


def reusable_analysis(paper_id,scope="full"):
    full=None;fast=None
    for result in analyses.read_analyses(load_paper_id=paper_id):
        doc=result['document']
        if not result.get('is_current'):continue
        if result.get('batch_summary',{}).get('scope')=='full' and doc.get('selected_chunks',0)==doc.get('total_chunks') and doc.get('total_chunks',0)>0:
            full=full or result
        selection=result.get('reading_selection') or {}
        if result.get('batch_summary',{}).get('scope')=='fast' and selection.get('policy_version')==FAST_VERSION and selection.get('budget')==18:
            fast=fast or result
    return (full or fast) if scope=='fast' else full



class StepFailed(Exception):
    def __init__(self,error):self.error=error


def execute_child(run,item,step,starter):
    jobs=BackgroundTasks()
    response=starter(jobs)
    child_id=response['task_id']
    item[step].update(status='running',task_id=child_id,reused=bool(response.get('reused')),error=None)
    checkpoint(run)
    # 所有当前全文与分析后台动作都是同步函数；与 HTTP 入口共用同一实现。
    # 先保存关联再执行，即使进程中断也能找到子任务和其分批检查点。
    for job in jobs.tasks:
        if not job.is_async:job.func(*job.args,**job.kwargs)
        else:raise RuntimeError('不支持的异步子任务')
    child=tasks.get_task(child_id)
    if child['status']!='succeeded':
        raise StepFailed(child.get('error') or {'code':'CHILD_TASK_RUNNING','message':'该论文已有任务正在执行。请待其完成后继续本轮，不会重复创建任务。','retryable':True})
    return child


def run_execution(run):
    try:
        checkpoint(run)
        # 先收藏全部候选，保存身份映射，再获取全文。浏览器不能提交论文元数据替换快照。
        seen=set()
        for item in run['papers']:
            try:
                if not item['paper_id']:
                    response=papers.save_papers_batch(PaperBatchRequest.model_validate({'papers':[item['candidate']]}))
                    result=response['results'][0]
                    if result['status']=='rejected':raise StepFailed({'code':'PAPER_IDENTITY_CONFLICT','message':result['reason'],'retryable':False})
                    item['paper_id']=result['paper_id'];checkpoint(run)
                if item['paper_id'] in seen:raise StepFailed({'code':'DUPLICATE_PAPER','message':'所选候选关联到同一篇论文，请选择不同论文另建本轮。','retryable':False})
                seen.add(item['paper_id'])
                item['selection_error']=None
            except StepFailed as exc:item['selection_error']=exc.error;checkpoint(run)
        for item in run['papers']:
            step='fulltext'
            # 每次继续都重新核对当前原文；不能沿用旧成功标记掩盖已删除/变更的全文。
            item['fulltext']['status']='pending'
            item['analysis']['status']='pending'
            try:
                if item.get('selection_error'):raise StepFailed(item['selection_error'])
                paper_id=item['paper_id']
                run['stage']='正在获取与分析：'+item['title'];checkpoint(run)
                existing=reusable_analysis(paper_id,run.get('analysis_scope','full'))
                docs=documents(paper_id)
                document_reused=bool(docs)
                if not docs:
                    execute_child(run,item,'fulltext',lambda jobs:fulltext.fetch_fulltext(paper_id,jobs))
                    docs=documents(paper_id)
                if not docs:raise StepFailed({'code':'NO_READABLE_DOCUMENT','message':'全文没有可分析片段，请上传或重新解析 PDF 后继续。','retryable':True})
                document=next((d for d in docs if existing and d['id']==existing['document']['id']),docs[0])
                item['fulltext'].update(status='succeeded',document_id=document['id'],content_sha256=document.get('content_sha256'),reused=document_reused,error=None)
                checkpoint(run);step='analysis'
                analysis_reused=existing is not None
                if existing is None:
                    execute_child(run,item,'analysis',lambda jobs:analyses.start_analysis(analyses.AnalysisRequest(paper_id=paper_id,scope=run.get('analysis_scope','full')),jobs))
                    existing=reusable_analysis(paper_id,run.get('analysis_scope','full'))
                if existing is None:raise StepFailed({'code':'ANALYSIS_NOT_CURRENT','message':'未取得与当前全文一致的完整分析，请核对原文后继续。','retryable':True})
                item['fulltext']['document_id']=existing['document']['id']
                item['fulltext']['content_sha256']=existing['document'].get('content_sha256')
                item['analysis'].update(status='succeeded',analysis_id=existing['id'],reused=analysis_reused,error=None,scope=existing.get('batch_summary',{}).get('scope','full'),reading_selection=existing.get('reading_selection'))
            except (StepFailed,HTTPException) as exc:
                error=exc.error if isinstance(exc,StepFailed) else {'code':'EXECUTION_STEP_FAILED','message':exc.detail if isinstance(exc.detail,str) else '步骤未能执行，请检查任务详情后继续。','retryable':True}
                item[step].update(status='failed',error=error)
            except Exception as exc:
                logger.warning('计划执行步骤失败：execution=%s error_type=%s',run['id'],type(exc).__name__)
                item[step].update(status='failed',error={'code':'EXECUTION_STEP_FAILED','message':'步骤执行异常，请检查数据库、模型配置或任务详情后继续。','retryable':True})
            checkpoint(run)
        # 不把样本分析、过期结果或仅成功提交子任务视为闭环完成。
        checkpoint(run,finish=True)
    except Exception as exc:
        logger.warning('计划执行中断：execution=%s error_type=%s',run['id'],type(exc).__name__)
        tasks.update_task(run['task_id'],status='failed',stage='本轮执行中断，已保存结果保留',error={'code':'PLAN_EXECUTION_INTERRUPTED','message':'执行中断，可继续未完成部分。','retryable':True})


def enqueue(db,run):
    task_id=uuid4().hex;timestamp=tasks.now()
    run.update(task_id=task_id,status='queued',error=None,updated_at=timestamp,attempts=run.get('attempts',0)+1)
    task=dict(id=task_id,type='research_execution',paper_id=None,plan_id=run['plan_id'],plan_revision=run['revision'],
              execution_id=run['id'],search_run_id=run['search_run_id'],status='queued',stage='等待获取全文并逐篇分析',progress=0,result_id=None,error=None,created_at=timestamp,updated_at=timestamp)
    db.execute('INSERT INTO tasks VALUES (?,?)',(task_id,json.dumps(task,ensure_ascii=False)))
    db.execute('INSERT OR REPLACE INTO research_executions VALUES (?,?,?,?,?)',(run['id'],run['plan_id'],run['revision'],run['selection_key'],json.dumps(run,ensure_ascii=False)))


def active_execution(db,plan_id):
    return next((r for row in db.execute('SELECT payload FROM research_executions WHERE plan_id=?',(plan_id,))
                 if (r:=public_run(db,json.loads(row[0])))['status'] in ACTIVE),None)


@router.post('/{plan_id}/executions',status_code=202)
def start_execution(plan_id:str,body:ExecutionRequest,background_tasks:BackgroundTasks):
    try:
        with tasks.connection() as db:
            initialize(db);db.execute('BEGIN IMMEDIATE')
            saved=read_plan(db,plan_id)
            if saved['revision']!=body.expected_revision:raise HTTPException(409,detail='计划版本已改变，请刷新后重新选择。')
            row=db.execute('SELECT payload FROM research_search_runs WHERE id=? AND plan_id=?',(body.search_run_id,plan_id)).fetchone()
            search=public_run(db,json.loads(row[0])) if row else None
            if not search or search['revision']!=saved['revision'] or search['status']!='succeeded':raise HTTPException(422,detail='请选择当前计划版本下已完成检索与初筛的记录。')
            actions=[s['action'] for s in search['plan']['steps']]
            if 'acquire_fulltext' not in actions or 'analyze_papers' not in actions or actions.index('acquire_fulltext')>actions.index('analyze_papers') or any(a not in {'search_papers','screen_papers','acquire_fulltext','analyze_papers'} for a in actions[:actions.index('analyze_papers')]):
                raise HTTPException(422,detail='计划须按顺序包含全文获取和单篇分析步骤，且前置动作已接通。')
            candidates={p['candidate_id']:p for p in search['candidates']}
            if any(i not in candidates or not candidates[i].get('identity_verified') or i not in search['screenings'] for i in body.candidate_ids):raise HTTPException(422,detail='所选候选不存在、身份有冲突或尚未初筛。')
            key=hashlib.sha256(json.dumps([body.search_run_id,sorted(body.candidate_ids)]).encode()).hexdigest()
            if body.analysis_scope=='fast':key=hashlib.sha256(json.dumps([key,'fast',FAST_VERSION]).encode()).hexdigest()
            active=active_execution(db,plan_id)
            if active:
                if active['selection_key']!=key:raise HTTPException(409,detail='该计划已有本轮任务执行中，请完成后再更换论文。')
                return dict(task_id=active['task_id'],execution_id=active['id'],reused=True)
            rows=db.execute('SELECT payload FROM research_executions WHERE plan_id=? AND selection_key=?',(plan_id,key)).fetchall()
            run=json.loads(rows[-1][0]) if rows else None
            if run and public_run(db,run)['status']=='succeeded':return dict(task_id=run['task_id'],execution_id=run['id'],reused=True)
            if not run:
                run=dict(id='execution_'+uuid4().hex,plan_id=plan_id,revision=saved['revision'],plan=search['plan'],goal=search['goal'],analysis_scope=body.analysis_scope,search_run_id=search['id'],selection_key=key,created_at=tasks.now(),step_numbers={action:[s['number'] for s in search['plan']['steps'] if s['action']==action] for action in ('acquire_fulltext','analyze_papers')},
                         papers=[dict(candidate_id=i,title=candidates[i]['title'],candidate=candidates[i],screening=search['screenings'][i],paper_id=None,fulltext={'status':'pending'},analysis={'status':'pending'}) for i in body.candidate_ids])
            enqueue(db,run)
        background_tasks.add_task(run_execution,run)
        return dict(task_id=run['task_id'],execution_id=run['id'],reused=False)
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='暂时无法创建计划执行记录。') from None


@router.post('/{plan_id}/executions/{execution_id}/resume',status_code=202)
def resume_execution(plan_id:str,execution_id:str,background_tasks:BackgroundTasks):
    try:
        with tasks.connection() as db:
            initialize(db);db.execute('BEGIN IMMEDIATE');run=read_execution(db,plan_id,execution_id)
            active=active_execution(db,plan_id)
            if active:
                if active['id']!=execution_id:raise HTTPException(409,detail='同一计划已有其他执行任务运行中。')
                return dict(task_id=active['task_id'],execution_id=execution_id,reused=True)
            if public_run(db,run)['status']=='succeeded':return dict(task_id=run['task_id'],execution_id=execution_id,reused=True)
            enqueue(db,run)
        background_tasks.add_task(run_execution,run)
        return dict(task_id=run['task_id'],execution_id=execution_id,reused=False)
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='暂时无法继续执行。') from None


@router.get('/{plan_id}/executions')
def list_executions(plan_id:str,search_run_id:str|None=None):
    try:
        with tasks.connection() as db:
            initialize(db);read_plan(db,plan_id)
            rows=db.execute('SELECT payload FROM research_executions WHERE plan_id=? ORDER BY rowid DESC',(plan_id,)).fetchall()
            return {'items':[public_execution(db,r) for row in rows if (r:=json.loads(row[0])) and (not search_run_id or r['search_run_id']==search_run_id)]}
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='暂时无法读取执行记录。') from None
