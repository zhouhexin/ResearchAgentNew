"""覆盖缺口触发的显式、受限 arXiv 检索；复用检索图和初筛，不自动下载。"""
from copy import deepcopy
import json
import time

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.research_search import build_search_graph
from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import coverage, provenance, runtime_bindings, tasks
from api.rereads import BudgetedModel
from storage import reread_budget as budget
from tools.arxiv_search import search_arxiv_page
from tools.execution_config import scope as tool_scope
from tools.venue_search.catalog import selected_venues

router=APIRouter(prefix='/api/v1',tags=['受限补检索'])


class SearchConfirmation(BaseModel):
    model_config=ConfigDict(extra='forbid')
    question_id:str=Field(max_length=30)
    suggestion_index:int=Field(ge=0)
    queries:list[str]=Field(min_length=1,max_length=2)
    per_query:int=Field(default=5,ge=1,le=10)
    max_calls:int=Field(default=8,ge=2,le=24)
    total_seconds:int=Field(default=300,ge=10,le=1800)
    source:str=Field(default='arxiv',pattern='^arxiv$')

    @model_validator(mode='after')
    def valid(self):
        self.queries=[q.strip() for q in self.queries]
        if any(not q or len(q)>500 for q in self.queries) or len(set(self.queries))!=len(self.queries):
            raise ValueError('请输入1–2个不重复的检索词，每项最多500字。')
        return self


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS supplement_search_checkpoints (id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)')


def check_source(source):
    try:coverage.verify_request(source)
    except HTTPException as exc:
        if exc.status_code>=500:raise
        raise RuntimeUnavailable('原覆盖输入已失效，请重新核对材料并生成覆盖报告。') from None


def check_scope(scope):
    if selected_venues(scope) or scope.get('verified_main_only') or any(s.strip().lower()!='arxiv' for s in scope['requested_sources']):
        raise HTTPException(422,detail='当前仅支持 arXiv 补检索，原计划有其他来源/发表约束，请另建明确授权的检索计划。')


def verify(record):
    result=coverage.read(record['coverage_result_id'])
    check_source(coverage.read(result['request_id']))
    if fingerprint(result)!=record['coverage_sha256']:
        raise RuntimeUnavailable('原覆盖报告已变化，不能继续补检索。')


def enqueue(record, background):
    verify(record)
    binding=runtime_bindings.restore(record['runtime_bindings'],'search')
    parent=tasks.get_task(record['coverage_task_id'])
    with tasks.connection() as db:
        related=[json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
    prior=next((t for t in related if t.get('supplement_request_id')==record['id']),None)
    if prior and prior['status'] in {'queued','running','succeeded'}:
        return {'task_id':prior['id'],'request_id':record['id'],'reused':True}
    task=tasks.create_task(task_type='supplement_search',stage='等待补检索与摘要初筛',deduplicate=True,dedup_key=record['id'],metadata={
        **provenance.fields(parent),**binding.metadata,'parent_task_id':parent['id'],
        'runtime_bindings':record['runtime_bindings'],'supplement_request_id':record['id'],
        'coverage_result_id':record['coverage_result_id'],'supplement_question_id':record['question_id'],
        'supplement_suggestion_index':record['suggestion_index'], 'research_questions':[record['question']], 'resumed_from_task_id':prior['id'] if prior else None})
    if not task.get('_reused'): background.add_task(worker,task['id'])
    return {'task_id':task['id'],'request_id':record['id'],'reused':bool(task.get('_reused'))}


@router.post('/coverage/{result_id}/supplement-search',status_code=202)
def start(result_id:str, body:SearchConfirmation, background_tasks:BackgroundTasks):
    try:
        result=coverage.read(result_id)
        if result.get('kind')!='result':raise HTTPException(422,detail='需要已完成的覆盖报告。')
        source=coverage.read(result['request_id']);check_source(source)
        question=next((q for q in result['report']['questions'] if q['question_id']==body.question_id),None)
        if not question or body.suggestion_index>=len(question['suggestions']) or question['status']=='answered':
            raise HTTPException(422,detail='请选择仍有缺口的问题及补检索建议。')
        suggestion=question['suggestions'][body.suggestion_index]
        if suggestion['action']!='search_later':raise HTTPException(422,detail='此建议应先补读已有材料。')
        scope=source['plan_snapshot']['scope']
        check_scope(scope)
        # 不编辑原计划；补检索记录保存实际问题、查询及原计划快照。
        key=fingerprint([result_id,body.model_dump(),source['runtime_bindings']])
        identifier='supplement_request_'+key[:40]
        try:record=coverage.read(identifier)
        except HTTPException as exc:
            if exc.status_code!=404:raise
            plan=deepcopy(source['plan_snapshot'])
            plan.update(research_questions=[question['question']],search_queries=body.queries)
            record={'id':identifier,'kind':'supplement_request','policy_version':'bounded-arxiv-search-v1',
                'coverage_result_id':result_id,'coverage_sha256':fingerprint(result),
                'coverage_task_id':result['source_task_id'],'plan_id':source['plan_id'],
                'plan_revision':source['plan_revision'],'execution_id':source['execution_id'],
                'question_id':body.question_id,'question':question['question'],'suggestion_index':body.suggestion_index,
                'suggestion':suggestion,'plan':plan,'per_query':body.per_query,'created_at':result['created_at'],
                'runtime_bindings':source['runtime_bindings'],
                'budget':{'max_calls':body.max_calls,'total_seconds':body.total_seconds,'max_rounds':1,
                          'max_candidates':len(body.queries)*body.per_query,'max_queries':len(body.queries),
                          'automatic_downloads':0,'automatic_analysis_calls':0}}
            coverage.store(record)
        return enqueue(record,background_tasks)
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.post('/supplement-search/{request_id}/resume',status_code=202)
def resume(request_id:str,background_tasks:BackgroundTasks):
    try:
        record=coverage.read(request_id)
        if record.get('kind')!='supplement_request':raise HTTPException(422,detail='补检索请求不存在。')
        return enqueue(record,background_tasks)
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None


@router.get('/coverage/{result_id}/supplement-search')
def history(result_id:str):
    coverage.read(result_id)
    with tasks.connection() as db:
        related=[json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
    return {'items':[{'task':t,'request_id':t['supplement_request_id'],'budget':budget.summary(t['supplement_request_id'])}
        for t in related if t.get('type')=='supplement_search' and t.get('coverage_result_id')==result_id]}


@router.get('/supplement-search/{request_id}')
def detail(request_id:str):
    record=coverage.read(request_id)
    if record.get('kind')!='supplement_request':raise HTTPException(404,detail='补检索不存在。')
    try: saved=coverage.read('supplement_progress_'+request_id.removeprefix('supplement_request_'))
    except HTTPException as exc:
        if exc.status_code!=404:raise
        saved=None
    # 可变进度沿用 SQLite；完成结果另存不可变 PostgreSQL 产物。
    with tasks.connection() as db:
        initialize(db)
        row=db.execute('SELECT payload FROM supplement_search_checkpoints WHERE id=?',('search_'+request_id,)).fetchone()
    return {'request':record,'run':saved['run'] if saved else json.loads(row[0]) if row else None,'result':saved,'budget':budget.summary(request_id)}


@runtime_bindings.task_worker
def worker(task_id):
    task=tasks.get_task(task_id)
    if task['status'] not in {'queued','running'}:return
    record=None;acquired=False
    try:
        record=coverage.read(task['supplement_request_id'])
        acquired=budget.acquire(record,task_id)
        if not acquired:return
        tasks.update_task(task_id,status='running',stage='恢复补检索检查点，核对原问题与预算')
        verify(record);binding=runtime_bindings.restore(record['runtime_bindings'],'search')
        result_id='supplement_progress_'+record['id'].removeprefix('supplement_request_')
        try: saved=coverage.read(result_id)
        except HTTPException as exc:
            if exc.status_code!=404:raise
            saved=None
        if saved:
            tasks.update_task(task_id,status='succeeded',progress=100,stage='已恢复保存的补检索结果',result_id=record['id'],error=None)
            return
        run_id='search_'+record['id']
        with tasks.connection() as db:
            initialize(db)
            row=db.execute('SELECT payload FROM supplement_search_checkpoints WHERE id=?',(run_id,)).fetchone()
            run=json.loads(row[0]) if row else {'id':run_id,'plan_id':record['plan_id'],'revision':record['plan_revision'],
                'goal':record['question'],'plan':record['plan'],'per_query':record['per_query'],'created_at':tasks.now(),
                'queries':[{'number':i,'query':q,'status':'pending','items':[],'total':None} for i,q in enumerate(record['plan']['search_queries'],1)],
                'candidates':[],'screenings':{},'duplicate_count':0,'skipped_count':0,'attempts':0,
                'model_name':binding.runtime.model,'source':'arxiv','notice':'受限实时补检索；arXiv 预印本身份，不证明正式发表、全文结论或领域覆盖。',
                'supplement_request_id':record['id'],'source_coverage_result_id':record['coverage_result_id']}
        run.update(task_id=task_id,status='running',attempts=run['attempts']+1,**provenance.fields(task),**binding.metadata)
        def checkpoint(value):
            verify(record);binding.check()
            value.update(task_id=task_id,status='running',updated_at=tasks.now())
            with tasks.connection() as db:
                initialize(db)
                db.execute('INSERT OR REPLACE INTO supplement_search_checkpoints VALUES (?,?,?,?,?)',
                    (run_id,record['plan_id'],record['plan_revision'],record['id'],json.dumps(value,ensure_ascii=False)))
        checkpoint(run)
        def fetch(query,**kwargs):
            verify(record);binding.check()
            index,_=budget.reserve(record['id'],task_id)
            budget.finish(record['id'],index,status='started',kind='arxiv_query',query=query)
            try:
                page=search_arxiv_page(query,**kwargs,deadline=budget.summary(record['id'])['deadline_epoch'])
                if len(page['items'])>record['per_query']:raise ValueError('来源返回候选超出确认预算。')
                if time.time()>budget.summary(record['id'])['deadline_epoch']:
                    raise budget.ReadingStopped('SUPPLEMENT_DEADLINE_EXCEEDED','补检索期限已到，迟到结果未发布。')
                binding.check();verify(record)
                budget.finish(record['id'],index,status='returned',finished_at=tasks.now())
                return page
            except Exception as exc:
                budget.finish(record['id'],index,status='failed',error_type=type(exc).__name__)
                raise
        with tool_scope(binding.tools['search']):
            model=BudgetedModel(record,task_id,binding,check_inputs=verify)
            graph=build_search_graph(model,fetch=fetch,checkpoint=checkpoint,
                progress=lambda **event:tasks.update_task(task_id,**event),method=binding.settings['method'])
            finished=graph.invoke({'run':run},config={'recursion_limit':5})['run']
        verify(record);binding.check()
        finished.update(status='succeeded',stop_reason='confirmed_queries_screened',budget=budget.summary(record['id']))
        result_id='supplement_progress_'+record['id'].removeprefix('supplement_request_')
        coverage.store({'id':result_id,'kind':'supplement_result','request_id':record['id'],'run':finished})
        tasks.update_task(task_id,status='succeeded',progress=100,stage='补检索与初筛完成，等待人工选文；未下载或分析',result_id=record['id'],error=None)
    except Exception as exc:
        if isinstance(exc,(RuntimeUnavailable,budget.ReadingStopped)):
            code=exc.code.replace('REREAD_','SUPPLEMENT_');message=str(exc).replace('补读','补检索');retryable=False
        elif record and (usage:=budget.summary(record['id'])) and time.time()>usage['deadline_epoch']:
            code='SUPPLEMENT_DEADLINE_EXCEEDED';message='补检索执行期限已到，已保存部分保留，继续不会重置期限。';retryable=False
        else:
            code='SUPPLEMENT_SEARCH_FAILED';message='补检索或初筛中断，已保存候选和预算保留，可继续原请求。';retryable=True
        tasks.update_task(task_id,status='failed',stage='补检索未完成，检查点和原报告保留',
            error={'code':code,'message':message,'retryable':retryable},budget=budget.summary(record['id']) if record else None)
    finally:
        if acquired:budget.release(record['id'],task_id)
