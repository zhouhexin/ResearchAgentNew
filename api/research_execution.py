"""计划版本下的人工选文 → 全文 → 全量分析；复用既有任务和证据校验。"""
from copy import deepcopy
import hashlib
import json
import logging
import os
import sqlite3
from uuid import uuid4
from typing import Literal
from agent.fast_reading import VERSION as FAST_VERSION

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from api import analyses, claim_reviews, fulltext, papers, tasks
from api import runtime_bindings, provenance
from agent.runtime_snapshot import RuntimeUnavailable
from agent.runtime_snapshot import fingerprint as input_fingerprint
from agent.context_identity import context_identity
from sqlalchemy.exc import SQLAlchemyError
from tools.execution_config import scope as tool_scope
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
    review_after_analysis: bool = False

    @model_validator(mode='after')
    def unique(self):
        if len(set(self.candidate_ids))!=len(self.candidate_ids):raise ValueError('不能重复选择同一候选论文。')
        return self


class LibraryPaperSelection(BaseModel):
    model_config = ConfigDict(extra='forbid')
    paper_id: int = Field(ge=1)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=100)


class LibraryExecutionRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    papers: list[LibraryPaperSelection] = Field(min_length=2, max_length=5)
    analysis_scope: Literal['full', 'fast'] = 'full'
    review_after_analysis: bool = False

    @model_validator(mode='after')
    def unique(self):
        if len({p.paper_id for p in self.papers}) != len(self.papers):
            raise ValueError('请选择不同的文献库论文。')
        return self


def library_context(paper_id, scope):
    return analyses.load_paper_context(os.environ['DATABASE_URL'], paper_id,
        max_chunks=18 if scope == 'fast' else None, **({'fast': True} if scope == 'fast' else {}))


def verify_library_analysis(snapshot):
    from storage.db_models import PaperDocumentRecord
    from review.loader import parse_analysis
    from review.db_guard import verify_sources_in_session
    copy = deepcopy(snapshot)
    engine, Session = analyses.make_db(os.environ['DATABASE_URL'])
    try:
        with Session.begin() as session:
            document = session.get(PaperDocumentRecord, copy['document']['id'])
            if document is None:
                raise RuntimeUnavailable('选定分析的原文缺失。')
            copy['document']['source_url'] = document.source_url
            for evidence in copy['evidence_index'].values():
                evidence['source_url'] = document.source_url
            verify_sources_in_session(session, [parse_analysis(copy)])
    except ValueError as exc:
        raise RuntimeUnavailable('选定分析的身份、原文或引用位置已变化，请重新确认。') from None
    finally:
        engine.dispose()


def prepare_library_paper(selection, scope):
    paper = papers.get_paper(selection.paper_id)
    available = analyses.read_analyses(load_paper_id=selection.paper_id)
    if selection.analysis_id:
        existing = next((a for a in available if a['id'] == selection.analysis_id and a.get('is_current')), None)
        if existing is None:
            raise HTTPException(422, detail='指定分析缺失、失效或不属于所选论文。')
    else:
        existing = reusable_analysis(selection.paper_id, scope)
    docs = documents(selection.paper_id)
    if not docs:
        raise HTTPException(422, detail=f'P{selection.paper_id} 没有可读全文，请先上传并解析 PDF。')
    if existing:
        verify_library_analysis(existing)
        document = next((doc for doc in docs if doc['id'] == existing['document']['id']), None)
        if document is None:
            raise HTTPException(422, detail='指定分析对应的全文不可用。')
        context_hash = None
    else:
        context = library_context(selection.paper_id, scope)
        document = next(doc for doc in docs if doc['id'] == context.document_id)
        context_hash = context_identity(context)
    return {'candidate_id': f'library:P{selection.paper_id}', 'paper_id': selection.paper_id, 'title': paper['title'],
        'screening': {'decision': 'uncertain', 'basis': 'user_library_selection'},
        'library_input': {'document_id': document['id'], 'content_sha256': document['content_sha256'],
                          'paper_title': paper['title'], 'paper_doi': paper.get('doi'),
                          'context_sha256': context_hash, 'analysis_id': existing['id'] if existing else None,
                          'analysis_sha256': input_fingerprint(existing) if existing else None},
        'fulltext': {'status': 'pending'}, 'analysis': {'status': 'pending',
            **({'analysis_id': existing['id']} if existing else {})}}


def verify_library_input(item, scope):
    fixed = item['library_input']
    paper = papers.get_paper(item['paper_id'])
    if paper['title'] != fixed['paper_title'] or paper.get('doi') != fixed['paper_doi']:
        raise RuntimeUnavailable('文献库论文身份已变化，请重新确认选文。')
    docs = documents(item['paper_id'])
    if not any(d['id'] == fixed['document_id'] and d['content_sha256'] == fixed['content_sha256'] for d in docs):
        raise RuntimeUnavailable('文献库选定的全文已变化或缺失，请新建本轮执行。')
    if fixed['context_sha256'] and context_identity(library_context(item['paper_id'], scope)) != fixed['context_sha256']:
        raise RuntimeUnavailable('文献库选定的原文片段或位置已变化，请新建本轮执行。')
    if fixed['analysis_id']:
        existing = next((a for a in analyses.read_analyses(load_paper_id=item['paper_id'])
                         if a['id'] == fixed['analysis_id'] and a.get('is_current')), None)
        if existing is None or input_fingerprint(existing) != fixed['analysis_sha256']:
            raise RuntimeUnavailable('文献库选定的分析版本已变化或失效，不会替换为最新分析。')
        verify_library_analysis(existing)


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
        for step in execution_steps(run):
            child=item[step].get('task_id')
            row=db.execute('SELECT payload FROM tasks WHERE id=?',(child,)).fetchone() if child else None
            item[step]['task']=json.loads(row[0]) if row else None
    return run


def execution_steps(run):
    return ('fulltext', 'analysis', 'review') if run.get('review_after_analysis') else ('fulltext', 'analysis')


def paper_complete(run, paper):
    return all(paper.get(step, {}).get('status') == 'succeeded' for step in execution_steps(run))


def checkpoint(run,*,finish=False):
    with tasks.connection() as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        saved=read_execution(db,run['plan_id'],run['id'])
        row=db.execute('SELECT payload FROM tasks WHERE id=?',(run['task_id'],)).fetchone()
        if saved['task_id']!=run['task_id'] or not row:raise RuntimeError('执行已被替代')
        task=json.loads(row[0])
        if task['status'] not in ACTIVE:raise RuntimeError('执行任务已结束')
        done=sum(paper_complete(run,p) for p in run['papers'])
        run.update(updated_at=tasks.now(),status=('succeeded' if done==len(run['papers']) else 'failed') if finish else 'running')
        error=None if not finish or done==len(run['papers']) else {'code':'PLAN_EXECUTION_INCOMPLETE','message':'部分论文未完成，已完成结果保留。可继续未完成部分。','retryable':True}
        run['error']=error
        task.update(status=run['status'],error=error,updated_at=run['updated_at'],
                    progress=100 if run['status']=='succeeded' else int(100*sum(p.get(s,{}).get('status')=='succeeded' for p in run['papers'] for s in execution_steps(run))/(len(execution_steps(run))*len(run['papers']))),
                    stage=('本轮全文、分析与辅助评审完成；模型判断仍需人工核对' if run.get('review_after_analysis') else '本轮全文与分析完成；比较和综述尚未执行') if run['status']=='succeeded' else '部分论文未完成，可继续' if finish else run.get('stage','正在执行全文与分析'),
                    result_id=run['id'] if finish else None)
        if run.get('interaction_id'):
            task['execution_artifacts'] = [
                {'kind': 'analysis', 'id': p['analysis']['analysis_id'],
                 'relation': 'reused' if p['analysis'].get('reused') else 'output',
                 'source_task_id': p['analysis'].get('source_task_id') or p['analysis'].get('task_id'),
                 'source_agent_version_id': p['analysis'].get('source_agent_version_id'),
                 'source_runtime_snapshot_id': p['analysis'].get('source_runtime_snapshot_id')}
                for p in run['papers'] if p['analysis'].get('analysis_id') and p['analysis']['status'] == 'succeeded']
            task['execution_artifacts'].extend(
                {'kind': 'claim_review', 'id': p['review']['review_id'], 'relation': 'reused' if p['review'].get('reused') else 'output',
                 'source_task_id': p['review'].get('task_id'), 'analysis_id': p['review']['analysis_id']}
                for p in run['papers'] if p.get('review', {}).get('status') == 'succeeded' and p['review'].get('review_id'))
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
    context = dict(tasks.CURRENT_TASK.get() or {})
    context['plan_step'] = run['step_numbers']['acquire_fulltext' if step == 'fulltext' else 'analyze_papers'][0]
    context['execution_phase'] = step
    token = tasks.CURRENT_TASK.set(context)
    try:
        response=starter(jobs)
    finally:
        tasks.CURRENT_TASK.reset(token)
    child_id=response['task_id']
    if step == 'review':
        item[step]['review_id'] = response['review_id']
    history = item[step].setdefault('task_history', [])
    if child_id not in history:
        history.append(child_id)
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


def review_analysis(run, item, analysis):
    """只调度固定分析的评审；不修改分析结论，不以支持率决定流程通过。"""
    record = item.setdefault('review', {'status': 'pending'})
    if record.get('analysis_id') not in {None, analysis['id']}:
        raise RuntimeUnavailable('本轮分析版本已变化，不能把旧评审关联到新分析。请新建执行。')
    record.update(analysis_id=analysis['id'], status='pending')
    checkpoint(run)
    context = dict(tasks.CURRENT_TASK.get() or {}, execution_review=True,
                   analysis_task_id=analysis.get('source_task_id'))
    token = tasks.CURRENT_TASK.set(context)
    try:
        child = execute_child(run, item, 'review', lambda jobs: claim_reviews.start_review(
            analysis['id'], jobs, resume_review_id=record.get('review_id')))
        detail = claim_reviews.get_review(record['review_id'])
        if not detail['is_current'] or detail['analysis_id'] != analysis['id'] or detail['completed'] != detail['total']:
            raise StepFailed({'code': 'CLAIM_REVIEW_INCOMPLETE', 'message': '辅助评审尚未完整核验，已完成分析保留。', 'retryable': True})
        record.update(status='succeeded', completed_claims=detail['completed'], total_claims=detail['total'],
                      verdict_counts={label: sum(v.get('review', {}).get('verdict') == label for v in detail['items'])
                                      for label in ('supported', 'contradicted', 'insufficient', 'needs_review')},
                      error=None, source_runtime_snapshot_id=child.get('runtime_snapshot_id'))
    finally:
        tasks.CURRENT_TASK.reset(token)


@runtime_bindings.task_worker
def run_execution(run):
    try:
        tasks.update_task(run['task_id'], status='running', stage='正在恢复计划执行上下文')
        binding = runtime_bindings.for_task(run['task_id'], 'analysis')
        with tool_scope(binding.tools['search'] if binding else None):
            _run_execution(run, binding)
    except RuntimeUnavailable as exc:
        tasks.update_task(run['task_id'], status='failed', error={'code': exc.code, 'message': str(exc), 'retryable': False},
                          stage='原运行配置不可恢复，已完成结果保留')


def _run_execution(run, binding=None):
    try:
        checkpoint(run)
        # 先收藏全部候选，保存身份映射，再获取全文。浏览器不能提交论文元数据替换快照。
        seen=set()
        for item in run['papers']:
            if binding:
                binding.check()
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
            if binding:
                binding.check()
            # 每次继续都重新核对当前原文；不能沿用旧成功标记掩盖已删除/变更的全文。
            item['fulltext']['status']='pending'
            item['analysis']['status']='pending'
            try:
                if item.get('selection_error'):raise StepFailed(item['selection_error'])
                paper_id=item['paper_id']
                run['stage']='正在获取与分析：'+item['title'];checkpoint(run)
                if run.get('source') == 'library':
                    verify_library_input(item, run.get('analysis_scope', 'full'))
                fixed_id = item['analysis'].get('analysis_id') if run.get('review_after_analysis') or run.get('source') == 'library' else None
                if fixed_id:
                    existing = next((a for a in analyses.read_analyses(load_paper_id=paper_id)
                                     if a['id'] == fixed_id and a.get('is_current')), None)
                    if existing is None:
                        raise RuntimeUnavailable('本轮固定分析已失效或缺失，请新建执行；不会切换到较新的分析。')
                    if item['analysis'].get('output_sha256') and input_fingerprint(existing) != item['analysis']['output_sha256']:
                        raise RuntimeUnavailable('本轮固定分析内容已变化，不能继续旧评审或报告。')
                else:
                    existing=None if run.get('source') == 'library' else reusable_analysis(paper_id,run.get('analysis_scope','full'))
                docs=documents(paper_id)
                document_reused=bool(docs)
                if not docs:
                    if run.get('source') == 'library':
                        raise RuntimeUnavailable('本轮文献库全文不可用，请先补齐原文后新建执行。')
                    execute_child(run,item,'fulltext',lambda jobs:fulltext.fetch_fulltext(paper_id,jobs))
                    docs=documents(paper_id)
                if not docs:raise StepFailed({'code':'NO_READABLE_DOCUMENT','message':'全文没有可分析片段，请上传或重新解析 PDF 后继续。','retryable':True})
                document=next((d for d in docs if existing and d['id']==existing['document']['id']),docs[0])
                item['fulltext'].update(status='succeeded',document_id=document['id'],content_sha256=document.get('content_sha256'),reused=document_reused,error=None)
                checkpoint(run);step='analysis'
                analysis_reused=existing is not None
                if existing is None:
                    resume_id = item['analysis'].get('task_id') if binding else None
                    if resume_id and not tasks.get_task(resume_id).get('runtime_snapshot_id'):
                        raise RuntimeUnavailable('旧分析子任务没有固定配置，不能冒充原版本续跑。')
                    child=execute_child(run,item,'analysis',lambda jobs:analyses.start_analysis(analyses.AnalysisRequest(paper_id=paper_id,scope=run.get('analysis_scope','full'),resume_task_id=resume_id,
                        expected_context_sha256=item.get('library_input',{}).get('context_sha256')),jobs))
                    existing=next((a for a in analyses.read_analyses(load_paper_id=paper_id) if a['id'] == child.get('result_id') and a.get('is_current')),None) if run.get('source') == 'library' else reusable_analysis(paper_id,run.get('analysis_scope','full'))
                if existing is None:raise StepFailed({'code':'ANALYSIS_NOT_CURRENT','message':'未取得与当前全文一致的完整分析，请核对原文后继续。','retryable':True})
                item['fulltext']['document_id']=existing['document']['id']
                item['fulltext']['content_sha256']=existing['document'].get('content_sha256')
                item['analysis'].update(status='succeeded',analysis_id=existing['id'],reused=analysis_reused,error=None,scope=existing.get('batch_summary',{}).get('scope','full'),reading_selection=existing.get('reading_selection'))
                if run.get('source') == 'library':
                    item['analysis']['output_sha256'] = input_fingerprint(existing)
                if binding:
                    item['analysis']['source_task_id'] = existing.get('source_task_id')
                    item['analysis']['source_agent_version_id'] = existing.get('agent_version_id')
                    item['analysis']['source_runtime_snapshot_id'] = existing.get('runtime_snapshot_id')
                if run.get('review_after_analysis'):
                    step = 'review'
                    run['stage'] = '正在辅助评审：' + item['title']
                    review_analysis(run, item, existing)
            except (StepFailed,HTTPException) as exc:
                error=exc.error if isinstance(exc,StepFailed) else exc.detail if isinstance(exc.detail,dict) and 'code' in exc.detail else {'code':'EXECUTION_STEP_FAILED','message':exc.detail if isinstance(exc.detail,str) else '步骤未能执行，请检查任务详情后继续。','retryable':True}
                item[step].update(status='failed',error=error)
            except RuntimeUnavailable as exc:
                item.setdefault(step, {}).update(status='failed',error={'code': exc.code, 'message': str(exc), 'retryable': False})
            except Exception as exc:
                logger.warning('计划执行步骤失败：execution=%s error_type=%s',run['id'],type(exc).__name__)
                item[step].update(status='failed',error={'code':'EXECUTION_STEP_FAILED','message':'步骤执行异常，请检查数据库、模型配置或任务详情后继续。','retryable':True})
            checkpoint(run)
        # 不把样本分析、过期结果或仅成功提交子任务视为闭环完成。
        checkpoint(run,finish=True)
    except Exception as exc:
        logger.warning('计划执行中断：execution=%s error_type=%s',run['id'],type(exc).__name__)
        tasks.update_task(run['task_id'],status='failed',stage='本轮执行中断，已保存结果保留',error={'code':exc.code if isinstance(exc, RuntimeUnavailable) else 'PLAN_EXECUTION_INTERRUPTED','message':str(exc) if isinstance(exc, RuntimeUnavailable) else '执行中断，可继续未完成部分。','retryable':not isinstance(exc, RuntimeUnavailable)})


def enqueue(db,run):
    task_id=uuid4().hex;timestamp=tasks.now()
    previous_task_id = run.get('task_id')
    run.update(task_id=task_id,status='queued',error=None,updated_at=timestamp,attempts=run.get('attempts',0)+1)
    task=dict(id=task_id,type='research_execution',paper_id=None,plan_id=run['plan_id'],plan_revision=run['revision'],
              execution_id=run['id'],search_run_id=run['search_run_id'],status='queued',stage='等待获取全文并逐篇分析',progress=0,result_id=None,error=None,created_at=timestamp,updated_at=timestamp)
    task.update(provenance.fields(run))
    task.update(runtime_bindings.metadata(run.get('runtime_bindings'), 'analysis'))
    task = provenance.decorate(task, source=run, parent_task_id=run.get('source_task_id'))
    if previous_task_id:
        task['resumed_from_task_id'] = previous_task_id
    run.update(provenance.fields(task))
    db.execute('INSERT INTO tasks VALUES (?,?)',(task_id,json.dumps(task,ensure_ascii=False)))
    db.execute('INSERT OR REPLACE INTO research_executions VALUES (?,?,?,?,?)',(run['id'],run['plan_id'],run['revision'],run['selection_key'],json.dumps(run,ensure_ascii=False)))


def active_execution(db,plan_id):
    return next((r for row in db.execute('SELECT payload FROM research_executions WHERE plan_id=?',(plan_id,))
                 if (r:=public_run(db,json.loads(row[0])))['status'] in ACTIVE),None)


@router.post('/{plan_id}/library-executions', status_code=202)
def start_library_execution(plan_id: str, body: LibraryExecutionRequest, background_tasks: BackgroundTasks):
    try:
        with tasks.connection() as db:
            initialize(db)
            saved = read_plan(db, plan_id)
        if saved['revision'] != body.expected_revision:
            raise HTTPException(409, detail='计划版本已变化，请保存并重新确认选文。')
        actions = [step['action'] for step in saved['plan']['steps']]
        if ('select_library_papers' not in actions or 'analyze_papers' not in actions
                or actions.index('select_library_papers') > actions.index('analyze_papers')
                or any(a not in {'select_library_papers', 'acquire_fulltext', 'analyze_papers'}
                       for a in actions[:actions.index('analyze_papers')])):
            raise HTTPException(422, detail='计划须先选择已有文献，再分析论文；不混用外部检索的前置步骤。')
        bundle = runtime_bindings.bundle_for(saved)
        if not bundle:
            raise HTTPException(422, detail='文献库执行需要启用 RESEARCH_RUNTIME_PINNING，固定角色配置。')
        runtime_bindings.restore(bundle, 'analysis')
        if body.review_after_analysis:
            runtime_bindings.restore(bundle, 'claim_review')
        selected = [prepare_library_paper(p, body.analysis_scope) for p in body.papers]
        key = input_fingerprint({'source': 'library-v1', 'plan_id': plan_id, 'revision': saved['revision'],
            'papers': [{'paper_id': p['paper_id'], 'input': p['library_input']} for p in selected],
            'scope': body.analysis_scope, 'review_after_analysis': body.review_after_analysis, 'bindings': bundle})
        with tasks.connection() as db:
            initialize(db); db.execute('BEGIN IMMEDIATE')
            if read_plan(db, plan_id)['revision'] != body.expected_revision:
                raise HTTPException(409, detail='选文期间计划已修改，请重新确认。')
            active = active_execution(db, plan_id)
            if active:
                if active['selection_key'] != key:
                    raise HTTPException(409, detail='该计划已有不同选文或范围的任务执行中。')
                return {'task_id': active['task_id'], 'execution_id': active['id'], 'reused': True}
            row = db.execute('SELECT payload FROM research_executions WHERE plan_id=? AND selection_key=? ORDER BY rowid DESC',
                             (plan_id, key)).fetchone()
            run = json.loads(row[0]) if row else None
            if run and public_run(db, run)['status'] == 'succeeded':
                return {'task_id': run['task_id'], 'execution_id': run['id'], 'reused': True}
            if run is None:
                run = dict(id='execution_'+uuid4().hex, plan_id=plan_id, revision=saved['revision'],
                    plan=deepcopy(saved['plan']), goal=saved['goal'], source='library', search_run_id=None,
                    selection_key=key, analysis_scope=body.analysis_scope, review_after_analysis=body.review_after_analysis,
                    created_at=tasks.now(), papers=selected,
                    step_numbers={action: [s['number'] for s in saved['plan']['steps'] if s['action'] == action]
                                  for action in ('select_library_papers', 'acquire_fulltext', 'analyze_papers')})
                run.update(provenance.plan_fields(saved), runtime_bindings=bundle, source_task_id=saved.get('source_task_id'))
                if body.review_after_analysis:
                    for item in run['papers']:
                        item['review'] = {'status': 'pending'}
            enqueue(db, run)
        background_tasks.add_task(run_execution, run)
        return {'task_id': run['task_id'], 'execution_id': run['id'], 'reused': False}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except SQLAlchemyError:
        raise HTTPException(503, detail='暂时无法核对文献库和原文，请检查数据库。') from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法保存文献库执行记录。') from None


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
            if body.review_after_analysis:
                key=hashlib.sha256(json.dumps([key,'analysis-review-v1']).encode()).hexdigest()
                if not search.get('runtime_bindings') and not runtime_bindings.enabled():
                    raise HTTPException(422,detail='自动辅助评审需要启用 RESEARCH_RUNTIME_PINNING，先固定各角色配置。')
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
                run.update(provenance.fields(search), runtime_bindings=runtime_bindings.bundle_for(search), source_task_id=search['task_id'])
                run['review_after_analysis'] = body.review_after_analysis
                if body.review_after_analysis:
                    runtime_bindings.restore(run['runtime_bindings'], 'claim_review')
                    for item in run['papers']:
                        item['review'] = {'status': 'pending'}
            enqueue(db,run)
        background_tasks.add_task(run_execution,run)
        return dict(task_id=run['task_id'],execution_id=run['id'],reused=False)
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None
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
    except RuntimeUnavailable as exc:raise runtime_bindings.unavailable(exc) from None
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='暂时无法继续执行。') from None


@router.get('/{plan_id}/executions')
def list_executions(plan_id:str,search_run_id:str|None=None):
    try:
        with tasks.connection() as db:
            initialize(db);read_plan(db,plan_id)
            rows=db.execute('SELECT payload FROM research_executions WHERE plan_id=? ORDER BY rowid DESC',(plan_id,)).fetchall()
            return {'items':[public_execution(db,r) for row in rows if (r:=json.loads(row[0])) and (not search_run_id or r['search_run_id']==search_run_id)]}
    except (sqlite3.Error,OSError):raise HTTPException(503,detail='暂时无法读取执行记录。') from None
