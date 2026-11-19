"""固定计划执行的覆盖报告；不执行模型给出的补读/检索建议。"""
from copy import deepcopy
import json
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from agent.question_coverage import POLICY, CoverageError, build_input, assess_coverage
from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import claim_reviews, provenance, research_execution, reviews, runtime_bindings, tasks
from evaluation.claim_quality import error_info
from storage.coverage_reports import CoverageArtifact
from storage.research_provenance import save_once
from api.analysis_runtime import RuntimeStorageUnavailable

router = APIRouter(prefix='/api/v1', tags=['研究问题覆盖检查'])


class CoverageRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    research_questions: list[str] | None = Field(default=None, min_length=1, max_length=6)
    review_ids: list[str] | None = Field(default=None, max_length=5)
    resume_request_id: str | None = Field(default=None, max_length=100)

    @model_validator(mode='after')
    def valid(self):
        if self.research_questions is not None:
            self.research_questions = [q.strip() for q in self.research_questions]
            if any(not q or len(q)>1000 for q in self.research_questions) or len(set(self.research_questions)) != len(self.research_questions):
                raise ValueError('请填写1–6个不重复的研究问题，每项最多1000字。')
        if self.resume_request_id and (self.research_questions is not None or self.review_ids is not None):
            raise ValueError('继续原覆盖任务不能同时修改问题或评审版本。')
        if self.review_ids is not None and len(set(self.review_ids)) != len(self.review_ids):
            raise ValueError('不能重复选择同一评审。')
        return self


def store(value):
    copy = deepcopy(value)
    envelope = {'content': copy, 'content_sha256': fingerprint(copy)}
    provenance.transaction(lambda session: save_once(session, CoverageArtifact, value['id'], envelope))


def read(identifier):
    def query(session):
        row = session.get(CoverageArtifact, identifier)
        return row.payload if row else None
    envelope = provenance.transaction(query)
    if envelope is None:
        raise HTTPException(404, detail='覆盖报告或请求不存在。')
    if envelope['content']['id'] != identifier or fingerprint(envelope['content']) != envelope['content_sha256']:
        raise RuntimeUnavailable('覆盖产物内容校验失败。')
    return envelope['content']


def source_run(plan_id, execution_id):
    with tasks.connection() as db:
        research_execution.initialize(db)
        run = research_execution.public_execution(db, research_execution.read_execution(db, plan_id, execution_id))
    if not all(p['analysis']['status']=='succeeded' and p['analysis'].get('analysis_id') for p in run['papers']):
        raise HTTPException(422, detail='请先完成本轮所有论文的分析。')
    if run.get('review_selection_id'):
        from api.execution_review_links import verify_linked_run
        verify_linked_run(run)
    # 评审可失败或缺失；它的状态作为辅助信息进入覆盖检查，不伪装成语义判定。
    return run


def review_material(review_ids, analysis_ids):
    result = []
    for identifier in review_ids:
        detail = claim_reviews.get_review(identifier)
        if detail['analysis_id'] not in analysis_ids or not detail['is_current']:
            raise CoverageError('所选评审不属于本轮分析，或已经失效。')
        if any(v['analysis_id']==detail['analysis_id'] for v in result):
            raise CoverageError('每个分析版本最多选择一份评审。')
        result.append({key: detail[key] for key in ('id', 'analysis_id', 'is_current', 'items', 'quote_protocol')})
    return result


def verify_request(record):
    run = source_run(record['plan_id'], record['execution_id'])
    if (run['revision'] != record['plan_revision'] or run['plan'] != record['plan_snapshot']
            or [p['analysis']['analysis_id'] for p in run['papers']] != record['selection']['analysis_ids']):
        raise RuntimeUnavailable('原执行的计划或分析关联已变化，不能混用覆盖报告。')
    selection = reviews.ReviewSelection.model_validate(record['selection'])
    reviews.prepare_selection(selection)
    if {key: fingerprint(reviews.load_analysis(key)) for key in selection.analysis_ids} != record['analysis_fingerprints']:
        raise RuntimeUnavailable('原分析内容已变化，不会切换到较新分析。')
    material = review_material(record['review_ids'], selection.analysis_ids)
    if fingerprint(material) != record['reviews_fingerprint']:
        raise RuntimeUnavailable('原评审输入或检查点已变化，请新建覆盖检查。')


def review_summary(material, analysis_ids):
    by_analysis = {v['analysis_id']: v for v in material}
    output = []
    for key in analysis_ids:
        review = by_analysis.get(key)
        if not review:
            output.append({'analysis_id': key, 'status': 'not_reviewed', 'human_verdict': None})
            continue
        output.append({'analysis_id': key, 'review_id': review['id'], 'human_verdict': None,
            'items': [{k: item.get(k) for k in ('category', 'finding_index', 'statement', 'status', 'review', 'error')}
                      for item in review['items']]})
    return output


@runtime_bindings.task_worker
def worker(request_id):
    # decorator expects task_id as first argument; request is loaded from task metadata.
    task = tasks.get_task(request_id)
    try:
        record = read(task['coverage_request_id'])
        binding = runtime_bindings.for_task(task['id'], 'coverage')
        if not binding:
            raise RuntimeUnavailable('原覆盖检查运行版本不可用。')
        tasks.update_task(task['id'], status='running', progress=10, stage='正在核对固定问题、分析与评审输入')
        verify_request(record)
        binding.check()
        result_id = 'coverage_result_' + record['id'].removeprefix('coverage_request_')
        try:
            saved = read(result_id)
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            saved = None
        if saved is None:
            tasks.update_task(task['id'], progress=30, stage='正在逐项评估问题覆盖，仅生成待人工确认建议')
            with binding.llm() as llm:
                report = assess_coverage(llm, record['input'], binding.settings['method'])
            verify_request(record)
            binding.check()
            saved = {'id': result_id, 'kind': 'result', 'request_id': record['id'],
                     'plan_id': record['plan_id'], 'execution_id': record['execution_id'],
                     'created_at': tasks.now(), 'source_task_id': task['id'], 'report': report,
                     'analysis_ids': record['selection']['analysis_ids'], 'review_ids': record['review_ids'],
                     'reading_coverage': [{k: p[k] for k in ('paper_id', 'title', 'analysis_id', 'reading_scope', 'selected_chunks', 'total_chunks', 'reading_selection')} for p in record['input']['papers']],
                     **provenance.fields(task), **binding.metadata}
            store(saved)
        tasks.update_task(task['id'], status='succeeded', progress=100, stage='覆盖报告已保存，补读建议尚未执行', result_id=saved['id'], error=None)
    except Exception as exc:
        if isinstance(exc, RuntimeUnavailable):
            error = {'code': exc.code, 'message': str(exc), 'retryable': False}
        elif isinstance(exc, CoverageError):
            error = {'code': exc.code, 'message': str(exc), 'retryable': True}
        elif isinstance(exc, HTTPException):
            error = {'code': 'COVERAGE_INPUTS_CHANGED', 'message': '固定分析、评审或原文不可用，请核对后新建任务。', 'retryable': False}
        else:
            error, _ = error_info(exc)
            error = {k: error[k] for k in ('code', 'message')} | {'retryable': True}
        tasks.update_task(task['id'], status='failed', stage='覆盖检查未完成，原分析与报告保留', error=error)


@router.post('/agent/plans/{plan_id}/executions/{execution_id}/coverage', status_code=202)
def start_coverage(plan_id: str, execution_id: str, body: CoverageRequest, background_tasks: BackgroundTasks):
    try:
        run = source_run(plan_id, execution_id)
        if body.resume_request_id:
            record = read(body.resume_request_id)
            if record['kind'] != 'request' or record['plan_id'] != plan_id or record['execution_id'] != execution_id:
                raise HTTPException(422, detail='只能继续本次执行的覆盖请求。')
            bundle = record['runtime_bindings']
            binding = runtime_bindings.restore(bundle, 'coverage')
            verify_request(record)
        else:
            bundle = runtime_bindings.bundle_for(run)
            if not bundle:
                raise RuntimeUnavailable('覆盖检查需要启用 RESEARCH_RUNTIME_PINNING，并使用含 coverage 配置的新计划。')
            if 'coverage' not in bundle:
                raise RuntimeUnavailable('原计划没有覆盖检查的固定配置，请更新代码后新建研究计划；不会套用当前默认值。')
            binding = runtime_bindings.restore(bundle, 'coverage')
            selection = reviews.ReviewSelection(paper_ids=[p['paper_id'] for p in run['papers']],
                analysis_ids=[p['analysis']['analysis_id'] for p in run['papers']])
            snapshots, _ = reviews.prepare_selection(selection)
            ids = selection.analysis_ids
            review_ids = body.review_ids if body.review_ids is not None else [p['review']['review_id'] for p in run['papers'] if p.get('review',{}).get('review_id')]
            material = review_material(review_ids, ids)
            questions = body.research_questions if body.research_questions is not None else run['plan']['research_questions']
            payload = build_input(questions, snapshots, review_summary(material, ids), binding.settings['max_input_chars'])
            record = {'id': 'coverage_request_' + uuid4().hex, 'kind': 'request', 'plan_id': plan_id,
                'execution_id': execution_id, 'plan_revision': run['revision'], 'plan_snapshot': run['plan'],
                'input': payload, 'selection': selection.model_dump(), 'review_ids': review_ids,
                'analysis_fingerprints': {key: fingerprint(reviews.load_analysis(key)) for key in ids},
                'reviews_fingerprint': fingerprint(material), 'created_at': tasks.now(), 'policy_version': POLICY,
                'runtime_bindings': bundle, **binding.metadata}
        key = fingerprint({k: record[k] for k in ('input', 'selection', 'review_ids', 'analysis_fingerprints', 'reviews_fingerprint', 'runtime_bindings')})
        key = fingerprint([key, plan_id, execution_id, run['revision'], record['plan_snapshot']])
        # 复用已成功报告；失败请求保留，通过同一输入键恢复原请求与配置。
        with tasks.connection() as db:
            candidates = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
        previous = next((t for t in reversed(candidates) if t.get('type')=='coverage_check' and t.get('dedup_key')==key), None)
        if previous:
            record = read(previous['coverage_request_id'])
            if previous['status'] == 'succeeded':
                try:
                    saved = read(previous['result_id'])
                    if saved.get('request_id') != record['id']:
                        raise RuntimeUnavailable('原覆盖报告与请求不匹配。')
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
                    previous = dict(previous, status='failed')
            if previous['status'] in {'queued','running','succeeded'}:
                verify_request(record)
                return {'task_id': previous['id'], 'request_id': record['id'], 'reused': True}
        if not previous:
            store(record)
        task = tasks.create_task(task_type='coverage_check', stage='等待生成研究问题覆盖报告', deduplicate=True, dedup_key=key,
            metadata={**provenance.fields(run), **runtime_bindings.metadata(bundle, 'coverage'),
                'parent_task_id': run['task_id'], 'plan_id': plan_id, 'plan_revision': record['plan_revision'],
                'execution_id': execution_id, 'coverage_request_id': record['id'],
                'analysis_ids': record['selection']['analysis_ids'], 'research_questions': [q['question'] for q in record['input']['questions']],
                'resumed_from_task_id': previous['id'] if previous else None})
        if not task.get('_reused'):
            background_tasks.add_task(worker, task['id'])
        return {'task_id': task['id'], 'request_id': task['coverage_request_id'], 'reused': bool(task.get('_reused'))}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except CoverageError as exc:
        raise HTTPException(422, detail={'code': exc.code, 'message': str(exc)}) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法保存覆盖检查任务。') from None


@router.get('/agent/plans/{plan_id}/executions/{execution_id}/coverage')
def list_coverage(plan_id: str, execution_id: str):
    with tasks.connection() as db:
        research_execution.initialize(db)
        research_execution.read_execution(db, plan_id, execution_id)
        related = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
    items = []
    for task in reversed(related):
        if task.get('type')=='coverage_check' and task.get('plan_id')==plan_id and task.get('execution_id')==execution_id:
            items.append({'request_id': task['coverage_request_id'], 'task': task})
    return {'items': items}


@router.get('/coverage/{result_id}')
def get_coverage(result_id: str):
    try:
        result = read(result_id)
        if result['kind'] != 'result':
            raise HTTPException(404, detail='覆盖报告不存在。')
        try:
            verify_request(read(result['request_id']))
            current = True
        except HTTPException as exc:
            if exc.status_code >= 500:
                raise
            current = False
        except RuntimeStorageUnavailable:
            raise
        except ValueError:
            current = False
        return dict(result, is_current=current)
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None


def markdown(result):
    labels = {'answered': '已回答', 'partial': '部分回答', 'unanswered': '未回答', 'conflicting': '存在冲突'}
    lines = ['# 研究问题覆盖报告', '', '> 模型辅助判断，待人工核对；仅限本轮材料，不代表完整领域覆盖。补读/检索建议未执行。', '',
             f"计划：{result['plan_id']}；执行：{result['execution_id']}；来源任务：{result['source_task_id']}",
             f"运行快照：{result['runtime_snapshot_id']}；当前材料有效：{result['is_current']}", '', '## 阅读范围', '']
    for p in result['reading_coverage']:
        lines.append(f"- P{p['paper_id']} {p['title']}：{p['reading_scope']}，{p['selected_chunks']}/{p['total_chunks']} 片段；{p['analysis_id']}")
    for q in result['report']['questions']:
        lines.extend(['', f"## {q['question_id']} {q['question']} · {labels[q['status']]}", '', q['answer'], '', q['reason']])
        lines.extend('- 缺口：'+gap for gap in q['gaps'])
        for c in q['citations']:
            lines.extend(['', f"原文 {c['ref_id']} · {c['paper_title']} · 页码 {c['page_number']}"])
            for span in c['source_spans']:
                lines.extend('> '+line for line in span['text'].splitlines())
        lines.extend(f"- 待确认建议：{s['action']}；论文 {s['paper_ids']}；{s['target']}；{s['rationale']}" for s in q['suggestions'])
    lines.extend(['', '## 限制', '', *('- '+s for s in result['report']['limitations'])])
    return '\n'.join(lines) + '\n'


@router.get('/coverage/{result_id}/files/{format}')
def download(result_id: str, format: str):
    if format not in {'json','md'}:
        raise HTTPException(422, detail='仅支持 JSON 和 Markdown。')
    result = get_coverage(result_id)
    return Response(json.dumps(result, ensure_ascii=False, indent=2) if format=='json' else markdown(result),
        media_type='application/json' if format=='json' else 'text/markdown',
        headers={'Content-Disposition': f'attachment; filename="question_coverage.{format}"', 'X-Content-Type-Options': 'nosniff'})
