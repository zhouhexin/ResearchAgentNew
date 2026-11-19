"""P2 人工确认的查询补读：固定原文、共享调用预算、另存分析；不自动检索。"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import json
import os
import time
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.analysis_schema import PaperAnalysis
from agent.analysis_validator import export_analysis
from agent.batch_analysis import BatchAnalysisFailed, analyze_batches, merge_analyses, split_context
from agent.context_identity import context_identity
from agent.llm_runtime import analysis_llm
from agent.paper_context import DocumentChangedError, load_paper_context, verify_paper_context
from agent.runtime_snapshot import RuntimeSnapshot, RuntimeUnavailable, fingerprint
from agent.targeted_reading import POLICY, candidates
from api import analyses, coverage, provenance, research_execution, runtime_bindings, tasks
from api.analysis_results import complete_analysis, load_result
from evaluation.claim_quality import error_info
from storage.analysis_batches import AnalysisBatchStore
from storage import reread_budget as budget
from storage.runtime_versions import save_immutable

router = APIRouter(prefix='/api/v1', tags=['问题导向补读'])


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    question_id: str = Field(max_length=30)
    suggestion_index: int = Field(ge=0)
    paper_id: int = Field(gt=0)
    locator: str = Field(default='', max_length=500)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    preview_id: str = Field(max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)
    max_calls: int = Field(default=8, ge=1, le=24)
    total_seconds: int = Field(default=600, ge=10, le=1800)


def inputs(result_id):
    result = coverage.read(result_id)
    if result.get('kind') != 'result':
        raise HTTPException(422, detail='需要已完成的覆盖报告。')
    record = coverage.read(result['request_id'])
    coverage.verify_request(record)
    return result, record


def fixed_context(paper_id, analysis_id):
    baseline = load_result(analysis_id)
    if baseline is None or baseline['paper']['id'] != paper_id:
        raise RuntimeUnavailable('原分析缺失或论文归属不符。')
    research_execution.verify_library_analysis(baseline)
    context = load_paper_context(os.environ['DATABASE_URL'], paper_id, max_chunks=None,
                                 document_id=baseline['document']['id'])
    engine, Session = analyses.make_db(os.environ['DATABASE_URL'])
    try:
        with Session.begin() as session:
            verify_paper_context(session, context)
    except DocumentChangedError:
        raise RuntimeUnavailable('固定原文或片段位置已变化，请重新解析并新建任务。') from None
    finally:
        engine.dispose()
    if context.document_sha256 != baseline['document']['content_sha256']:
        raise RuntimeUnavailable('原分析的文档版本已变化。')
    return context, baseline


def verify(record):
    _, source = inputs(record['coverage_result_id'])
    if record['analysis_id'] not in source['selection']['analysis_ids']:
        raise RuntimeUnavailable('补读分析不属于原覆盖输入。')
    context, baseline = fixed_context(record['paper_id'], record['analysis_id'])
    if context_identity(context) != record['context_sha256']:
        raise RuntimeUnavailable('补读原文、编号或位置已变化，请重新定位并确认。')
    return context, baseline


@router.post('/coverage/{result_id}/reread-preview')
def preview(result_id: str, body: PreviewRequest):
    try:
        result, source = inputs(result_id)
        run = coverage.source_run(source['plan_id'], source['execution_id'])
        if run.get('supplement_round', 0) >= 1:
            raise HTTPException(422, detail='本分支已完成一轮补读；请先人工复核，后续研究另建计划。')
        question = next((q for q in result['report']['questions'] if q['question_id']==body.question_id), None)
        if question is None or body.suggestion_index >= len(question['suggestions']):
            raise HTTPException(422, detail='补读建议不存在。')
        suggestion = question['suggestions'][body.suggestion_index]
        if suggestion['action']!='read_existing' or body.paper_id not in suggestion['paper_ids']:
            raise HTTPException(422, detail='只能定位本建议明确列出的已有论文。')
        index = source['selection']['paper_ids'].index(body.paper_id)
        analysis_id = source['selection']['analysis_ids'][index]
        context, baseline = fixed_context(body.paper_id, analysis_id)
        binding = runtime_bindings.restore(source['runtime_bindings'], 'analysis')
        entries = candidates(context, question['question'], suggestion['target'],
                             baseline.get('selected_evidence_ids', baseline['evidence_index']), body.locator)
        record = {'id': 'reread_preview_'+uuid4().hex, 'kind': 'reread_preview', 'policy_version': POLICY,
                  'coverage_result_id': result_id, 'analysis_id': analysis_id, 'paper_id': body.paper_id,
                  'document_id': context.document_id, 'document_sha256': context.document_sha256,
                  'context_sha256': context_identity(context), 'created_at': tasks.now(), 'candidates': entries,
                  'suggestion_index': body.suggestion_index,
                  'focus': {'question_id': body.question_id, 'question': question['question'],
                            'gaps': question['gaps'], 'target': suggestion['target'], 'locator': body.locator},
                  'runtime_bindings': source['runtime_bindings'], 'plan_id': source['plan_id'],
                  'execution_id': source['execution_id'], 'coverage_task_id': result['source_task_id'],
                  'per_batch_max_chars': binding.settings['batch_max_chars'], 'max_selected_chunks': 12,
                  'candidate_count': len(entries), 'total_chunks': context.total_chunks}
        coverage.store(record)
        return record
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None


def enqueue(record, background_tasks):
    verify(record)
    binding = runtime_bindings.restore(record['runtime_bindings'], 'analysis')
    with tasks.connection() as db:
        previous = next((json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')
                         if json.loads(r[0]).get('reread_request_id')==record['id']), None)
    if previous and previous['status'] in {'queued','running','succeeded'}:
        return {'task_id': previous['id'], 'request_id': record['id'], 'reused': True}
    parent = tasks.get_task(record['coverage_task_id'])
    task = tasks.create_task(record['paper_id'], task_type='paper_reread', stage='等待按确认片段补读',
        deduplicate=True, dedup_key=record['id'], metadata={**provenance.fields(parent), **binding.metadata,
            'runtime_bindings': record['runtime_bindings'], 'parent_task_id': parent['id'],
            'reread_request_id': record['id'], 'coverage_result_id': record['coverage_result_id'],
            'base_analysis_id': record['analysis_id'], 'reread_question_id': record['focus']['question_id'],
            'reread_suggestion_index': record['suggestion_index'], 'research_questions': [record['focus']['question']],
            'resumed_from_task_id': previous['id'] if previous else None})
    if not task.get('_reused'):
        background_tasks.add_task(worker, task['id'])
    return {'task_id': task['id'], 'request_id': record['id'], 'reused': bool(task.get('_reused'))}


@router.post('/rereads', status_code=202)
def confirm(body: ConfirmRequest, background_tasks: BackgroundTasks):
    try:
        record = coverage.read(body.preview_id)
        if record.get('kind') != 'reread_preview':
            raise HTTPException(422, detail='请先定位原文，再确认补读。')
        context, _ = verify(record)
        ids = body.evidence_ids
        allowed = {v['evidence_id']: v for v in record['candidates']}
        if len(set(ids))!=len(ids) or any(key not in allowed or key not in context.sources for key in ids):
            raise HTTPException(422, detail='只能选择本次预览中的真实 E 编号，不得重复或跨论文。')
        selected = replace(context, sources={key: value for key,value in context.sources.items() if key in ids})
        binding = runtime_bindings.restore(record['runtime_bindings'], 'analysis')
        batches = split_context(selected, binding.settings['batch_size'], binding.settings['batch_max_chars'])
        if len(batches)>body.max_calls:
            raise HTTPException(422, detail=f'至少需要 {len(batches)} 次批次调用，请减少片段或调整预算；引用修复也占用调用。')
        key = fingerprint([body.model_dump(), record['context_sha256'], record['runtime_bindings']])
        request_id = 'reread_request_'+key[:40]
        try:
            request = coverage.read(request_id)
        except HTTPException as exc:
            if exc.status_code!=404: raise
            request = {**record, 'id': request_id, 'kind': 'reread_request', 'preview_id': body.preview_id,
                       'selected_evidence_ids': list(selected.sources),
                       'budget': {'max_calls': body.max_calls, 'total_seconds': body.total_seconds,
                                  'max_rounds': 1, 'papers': 1, 'chunks': len(ids), 'batch_retries': 0},
                       'created_at': record['created_at']}
            coverage.store(request)
        return enqueue(request, background_tasks)
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None


@router.post('/rereads/{request_id}/resume', status_code=202)
def resume(request_id: str, background_tasks: BackgroundTasks):
    try:
        record = coverage.read(request_id)
        if record.get('kind')!='reread_request':
            raise HTTPException(422, detail='补读请求不存在。')
        return enqueue(record, background_tasks)
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None


@router.get('/coverage/{result_id}/rereads')
def history(result_id: str):
    coverage.read(result_id)
    with tasks.connection() as db:
        related = [json.loads(r[0]) for r in db.execute('SELECT payload FROM tasks ORDER BY rowid DESC')]
    return {'items': [{'task': t, 'request_id': t['reread_request_id'], 'budget': budget.summary(t['reread_request_id'])}
                      for t in related if t.get('type')=='paper_reread' and t.get('coverage_result_id')==result_id]}


class BudgetedModel:
    def __init__(self, record, task_id, binding, structured=None, check_inputs=None):
        self.record, self.task_id, self.binding, self.structured = record, task_id, binding, structured
        self.check_inputs = check_inputs or verify

    def with_structured_output(self, *args, **kwargs):
        return BudgetedModel(self.record, self.task_id, self.binding, (args, kwargs), self.check_inputs)

    def invoke(self, *args, **kwargs):
        self.check_inputs(self.record)
        self.binding.check()
        index, remaining = budget.reserve(self.record['id'], self.task_id)
        # 预算导出的实际超时也另存快照；模型、凭据、Prompt 与原根任务一致。
        runtime = RuntimeSnapshot.model_validate(self.binding.runtime.model_dump() | {
            'read_timeout': min(self.binding.runtime.read_timeout, remaining), 'retries': 0})
        try:
            provenance.transaction(lambda session: save_immutable(session, runtime))
            budget.finish(self.record['id'], index, runtime_snapshot_id=runtime.id, timeout=runtime.read_timeout,
                          status='started')
            with analysis_llm(runtime_snapshot=runtime) as model:
                if self.structured:
                    model = model.with_structured_output(*self.structured[0], **self.structured[1])
                result = model.invoke(*args, **kwargs)
            if time.time()>budget.summary(self.record['id'])['deadline_epoch']:
                raise budget.ReadingStopped('REREAD_DEADLINE_EXCEEDED', '补读响应超过执行期限，未发布迟到结果；已核验批次保留。')
            self.binding.check()
            self.check_inputs(self.record)
            budget.finish(self.record['id'], index, status='returned', finished_at=tasks.now())
            return result
        except Exception as exc:
            budget.finish(self.record['id'], index, status='failed', error_type=type(exc).__name__, finished_at=tasks.now())
            raise


@runtime_bindings.task_worker
def worker(task_id):
    task = tasks.get_task(task_id)
    if task['status'] not in {'queued', 'running'}:
        return
    record, acquired = None, False
    try:
        record = coverage.read(task['reread_request_id'])
        acquired = budget.acquire(record, task_id)
        if not acquired: return
        tasks.update_task(task_id, status='running', progress=5, stage='核对补读范围、固定模型和共享预算')
        context, baseline = verify(record)
        binding = runtime_bindings.restore(record['runtime_bindings'], 'analysis')
        selected = replace(context, sources={k: context.sources[k] for k in record['selected_evidence_ids']})
        batches = split_context(selected, binding.settings['batch_size'], binding.settings['batch_max_chars'])
        store = AnalysisBatchStore(tasks.TASK_DB_PATH)
        store.prepare(record['id'], fingerprint(record), batches)
        @contextmanager
        def factory(**_):
            yield BudgetedModel(record, task_id, binding)
        def check(_):
            verify(record)
            binding.check()
        def progress(**event):
            tasks.update_task(task_id, progress=int(85*event['completed']/event['total']),
                stage=f"补读第 {event['index']}/{event['total']} 批：{event['state']}",
                total_batches=event['total'], completed_batches=event['completed'])
        added, summary = analyze_batches(selected, batches, run_key=record['id'], store=store, llm_factory=factory,
            read_timeout=binding.runtime.read_timeout, method=binding.settings['method'], batch_retries=0,
            check_context=check, progress=progress, research_focus=record['focus'])
        # 只合并确定性核验通过的旧、新结论，不让模型改写旧分析。
        previous_ids = baseline.get('selected_evidence_ids', list(baseline['evidence_index']))
        union_ids = set(previous_ids) | set(selected.sources)
        union = replace(context, sources={k:v for k,v in context.sources.items() if k in union_ids},
                        reading_selection={'policy_version': POLICY, 'scope': 'supplemental',
                            'base_analysis_id': baseline['id'], 'previous_evidence_ids': previous_ids,
                            'reread_evidence_ids': list(selected.sources), 'focus': record['focus'],
                            'selected_chunks': len(union_ids), 'total_chunks': context.total_chunks,
                            'selected_pages': sorted({s.page_number for k,s in context.sources.items() if k in union_ids and s.page_number is not None}),
                            'sections': [], 'missing_sections': [], 'budget': len(union_ids), 'strategy': 'confirmed_query_selection',
                            'note': '原分析与本次确认片段的阅读范围并集；旧结论原样保留，新结论尚待独立评审。不据未读范围断言论文没有相关内容。'})
        merged = merge_analyses([PaperAnalysis.model_validate(baseline['analysis']), added], union)
        payload = export_analysis(merged, union)
        payload.update(selected_evidence_ids=list(union.sources), **binding.metadata,
            source_task_id=task_id, **provenance.fields(task),
            reread_request_id=record['id'], base_analysis_id=baseline['id'],
            coverage_result_id=record['coverage_result_id'], budget=budget.summary(record['id']),
            supplemental_analysis=added.model_dump(mode='json'),
            batch_summary={**summary, 'scope': 'supplemental', 'stop_reason': 'confirmed_chunks_completed',
                           'batch_size': binding.settings['batch_size']})
        payload['document']['source_url'] = analyses.safe_source(payload['document']['source_url'])
        for item in payload['evidence_index'].values(): item['source_url'] = analyses.safe_source(item['source_url'])
        check(union)
        engine, Session = analyses.make_db(os.environ['DATABASE_URL'])
        try:
            with Session.begin() as session:
                verify_paper_context(session, union)
                complete_analysis(task_id, payload)
        finally:
            engine.dispose()
    except Exception as exc:
        if isinstance(exc, BatchAnalysisFailed): exc = exc.cause
        if isinstance(exc, (RuntimeUnavailable, budget.ReadingStopped)):
            error = {'code': exc.code, 'message': str(exc), 'retryable': False}
        else:
            info, _ = error_info(exc)
            error = {'code': info['code'], 'message': info['message'], 'retryable': True}
        tasks.update_task(task_id, status='failed', stage='补读未完成；原分析、覆盖报告及已核验批次保留',
                          error=error, budget=budget.summary(record['id']) if record else None)
    finally:
        if acquired: budget.release(record['id'], task_id)


@router.post('/rereads/{task_id}/recheck', status_code=202)
def recheck(task_id: str, background_tasks: BackgroundTasks):
    """人工再次确认：建立分析选择的新分支，再用原问题检查；不改原执行。"""
    try:
        task = tasks.get_task(task_id)
        if task.get('type')!='paper_reread' or task['status']!='succeeded':
            raise HTTPException(422, detail='请先完成补读。')
        record = coverage.read(task['reread_request_id'])
        verify(record)
        result, source = inputs(record['coverage_result_id'])
        updated = load_result(task['result_id'])
        if not updated or updated.get('reread_request_id')!=record['id']:
            raise RuntimeUnavailable('补读产物缺失或不属于此任务。')
        research_execution.verify_library_analysis(updated)
        identifier = 'execution_reread_'+fingerprint([record['id'], updated['id']])[:32]
        with tasks.connection() as db:
            research_execution.initialize(db)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM research_executions WHERE id=?', (identifier,)).fetchone()
            if not row:
                original = research_execution.read_execution(db, source['plan_id'], source['execution_id'])
                derived = deepcopy(original)
                derived.pop('review_selection_id', None)
                derived.update(selection_kind='reread')
                derived.update(id=identifier, selection_key=identifier, review_after_analysis=False,
                    supplement_round=original.get('supplement_round',0)+1, source_execution_id=original['id'],
                    source_coverage_result_id=result['id'], reread_request_id=record['id'],
                    created_at=tasks.now(), updated_at=tasks.now(), status='succeeded', error=None)
                for item in derived['papers']:
                    if item['paper_id']==record['paper_id']:
                        item['analysis'] = {'status':'succeeded','analysis_id':updated['id'],
                            'task_id':task_id,'source_task_id':task_id,'reused':True,
                            'scope':'supplemental','reading_selection':updated['reading_selection'],
                            'source_runtime_snapshot_id':updated['runtime_snapshot_id'],
                            'source_agent_version_id':updated['agent_version_id']}
                        item.pop('review',None)  # 旧分析评审不能移植到新分析。
                        if item.get('library_input'):
                            item['library_input'].update(analysis_id=updated['id'],
                                analysis_sha256=research_execution.input_fingerprint(updated), context_sha256=None)
                selection_task = provenance.decorate({'id':uuid4().hex, 'type':'research_execution',
                    'status':'succeeded','progress':100,'stage':'人工确认补读后的新分析选择；未重新执行原计划',
                    'result_id':identifier,'error':None,'created_at':tasks.now(),'updated_at':tasks.now(),
                    **provenance.fields(task), 'execution_id':identifier,'parent_task_id':task_id,
                    'source_execution_id':original['id'], 'source_coverage_result_id':result['id'],
                    'execution_artifacts':[{'kind':'analysis','id':p['analysis']['analysis_id'],'relation':'reused'} for p in derived['papers']]},
                    source=task, parent_task_id=task_id)
                derived['task_id']=selection_task['id']
                derived.update(**provenance.fields(selection_task))
                db.execute('INSERT INTO tasks VALUES (?,?)',(selection_task['id'],json.dumps(selection_task,ensure_ascii=False)))
                db.execute('INSERT INTO research_executions VALUES (?,?,?,?,?)',
                    (identifier,derived['plan_id'],derived['revision'],identifier,json.dumps(derived,ensure_ascii=False)))
        response = coverage.start_coverage(source['plan_id'], identifier,
            coverage.CoverageRequest(research_questions=[q['question'] for q in source['input']['questions']]), background_tasks)
        return {**response,'execution_id':identifier,'plan_id':source['plan_id']}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
