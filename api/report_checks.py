"""综述独立审查与原协议回放。固定输入、有限调用、保留成功检查点。"""
from pathlib import Path
import json
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from api import coverage, provenance, runtime_bindings, tasks
from api.review_results import load_review
from review import report_review
from review.synthesis import (ReviewDraft, check_review_request, invoke_review_request,
                              ReviewValidationError, validate_review)
from storage.research_experiments import Checkpoints, ExperimentStopped

router = APIRouter(prefix='/api/v1/reviews', tags=['综述独立审查与回放'])
CHECKPOINTS = Path(__file__).resolve().parents[1] / 'outputs/report_checks.sqlite3'


class StartCheck(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    kind: Literal['independent_review', 'original_replay']
    max_calls: int = Field(default=100, ge=1, le=500)
    total_seconds: int = Field(default=1800, ge=10, le=86400)
    resume_request_id: str | None = Field(default=None, max_length=100)


def source(report_id):
    stored = load_review(report_id)
    if stored is None:
        raise HTTPException(404, detail='综述不存在。')
    return stored[0]


def verify(record):
    report = source(record['report_id'])
    if fingerprint(report) != record['report_sha256']:
        raise RuntimeUnavailable('原综述或冻结材料已变化，不切换到新版本。')
    operation = 'report_review' if record['kind'] == 'independent_review' else 'review'
    binding = runtime_bindings.restore(record['runtime_bindings'], operation)
    if record['kind'] == 'original_replay':
        request = report.get('synthesis_request')
        if not request:
            raise RuntimeUnavailable('历史报告没有实际综述请求，原协议回放不可用。')
        check_review_request(request)
        generation = tasks.get_task(report['source_task_id'])
        if (generation.get('synthesis_request') != request
                or generation.get('runtime_bindings') != record['runtime_bindings']
                or report.get('agent_version_id') != binding.version.id
                or report.get('runtime_snapshot_id') != binding.runtime.id):
            raise RuntimeUnavailable('报告实际请求与原任务运行版本不符。')
    return report, binding


def error_record(exc):
    from evaluation.claim_quality import error_info
    error = error_info(exc)[0]
    error['code'] = getattr(exc, 'code', None) or (
        'REVIEW_VALIDATION_FAILED' if isinstance(exc, ReviewValidationError) else error['code'])
    if isinstance(exc, ReviewValidationError):
        error['diagnostic'] = {key: exc.diagnostics.get(key) for key in
            ('validation_reason', 'section', 'point_index', 'invalid_refs', 'candidate_draft')}
    error['retryable'] = not isinstance(exc, RuntimeUnavailable)
    return error


def checked_success(saved, record, unit, report, binding):
    """缓存须重新核验输入身份、角色版本、原文位置与实际输出协议。"""
    try:
        if (saved.get('pending') or saved.get('status') != 'succeeded' or saved.get('error') is not None
                or saved.get('unit') != unit or saved['outcome']['case_id'] != unit['unit_id']
                or saved['outcome']['repetition'] != 1
                or saved['outcome']['input_hash'] != fingerprint([record['report_sha256'], unit])
                or saved.get('agent_version_id') != binding.version.id
                or saved.get('runtime_snapshot_id') != binding.runtime.id):
            raise ValueError('cache identity mismatch')
        if record['kind'] == 'independent_review':
            provided = report_review.input_for(report, unit, binding.settings['max_input_chars'])
            expected = report_review.materialize(saved['result']['review'], provided, report)
        else:
            request = report['synthesis_request']
            check_review_request(request)
            draft = ReviewDraft.model_validate(saved['result']['draft'])
            validate_review(draft, set(request['allowed_refs']))
            expected = {'draft': draft.model_dump(), 'request_sha256': request['sha256'],
                        'same_draft': draft.model_dump() == report['draft'],
                        'mode': 'frozen_original_protocol', 'output_equality_required': False}
        if expected != saved['result']:
            raise ValueError('cache content mismatch')
    except (KeyError, TypeError, ValueError):
        raise RuntimeUnavailable('综述成功检查点未通过输入、版本或原文复验；拒绝复用。') from None
    return saved


def publish_result(task, record, store, units, results, binding, stop_error=None):
    by_unit = {v['unit']['unit_id']: v for v in results}
    items = [by_unit.get(unit['unit_id']) or {'unit': unit, 'status': 'not_attempted',
             'error': stop_error, 'result': None} for unit in units]
    failed = sum(v['status'] != 'succeeded' for v in items)
    complete = not failed and stop_error is None
    result = {'id': 'report_check_result_' + uuid4().hex, 'kind': 'report_check_result',
        'check_kind': record['kind'], 'request_id': record['id'], 'report_id': record['report_id'],
        'report_sha256': record['report_sha256'], 'source_task_id': task['id'],
        **provenance.fields(task), 'items': items, 'denominator': len(units),
        'failed': failed, 'complete': complete, 'stop_error': stop_error,
        'human_review_completed': False,
        'usage': {'calls_reserved': store.create(record['id'], record['budget'])['calls_reserved'],
                  'runtime_retries': binding.runtime.retries, 'retry_slots_included': True,
                  'actual_transport_calls': None, 'tokens': None, 'cost': None},
        'attempts': list(store.items(record['id']).values())}
    coverage.store(result)
    tasks.update_task(task['id'], status='succeeded' if complete else 'failed',
        progress=100 if stop_error is None else int(100 * len(results) / max(1, len(units))),
        stage='综述检查完成，语义判断待人工复核' if complete else '综述检查停止，部分结果与完整分母已保存',
        result_id=result['id'], error=stop_error or ({'code': 'REPORT_CHECK_INCOMPLETE',
            'message': '失败项已保留，可在原预算内继续。', 'retryable': True} if failed else None))


@runtime_bindings.task_worker
def worker(task_id):
    task = tasks.get_task(task_id)
    store = Checkpoints(CHECKPOINTS)
    token = None
    record, binding, units, results = None, None, [], []
    try:
        record = coverage.read(task['report_check_request_id'])
        store.create(record['id'], record['budget'])
        token = store.acquire(record['id'], record['budget']['total_seconds'] + 60)
        if not token:
            raise RuntimeUnavailable('此综述检查已有执行者。')
        report, binding = verify(record)
        tasks.update_task(task_id, status='running', progress=5, stage='核对冻结综述与原角色配置')
        units = report_review.units(report) if record['kind'] == 'independent_review' else [{'unit_id': 'original_replay'}]
        for unit in units:
            report, binding = verify(record)
            previous = [v for v in store.items(record['id']).values() if v['outcome']['case_id'] == unit['unit_id']]
            success = next((v for v in previous if v.get('status') == 'succeeded'), None)
            if success:
                results.append(checked_success(success, record, unit, report, binding))
                continue
            if any(v['pending'] for v in previous):
                raise RuntimeUnavailable('上次调用中断，是否完成未知；不自动重复调用，请新建有独立预算的检查。')
            attempt = len(previous) + 1
            key = fingerprint([unit['unit_id'], attempt])
            pending = {'pending': True, 'attempt': attempt, 'status': 'interrupted',
                'outcome': {'case_id': unit['unit_id'], 'repetition': 1, 'input_hash': fingerprint([record['report_sha256'], unit])},
                'unit': unit, 'agent_version_id': binding.version.id, 'runtime_snapshot_id': binding.runtime.id}
            # 原协议可能启用客户端重试：预算预留其全部可能请求，不隐藏重试。
            budget = store.create(record['id'], record['budget'])
            if budget['calls_reserved'] + 1 + binding.runtime.retries > record['budget']['max_calls']:
                raise ExperimentStopped('EXPERIMENT_BUDGET_EXHAUSTED')
            store.reserve(record['id'], token, (key, pending))
            for _ in range(binding.runtime.retries):
                store.reserve(record['id'], token)
            completed = {**pending, 'pending': False}
            try:
                with binding.llm() as llm:
                    store.check_deadline(record['id'], token)
                    store.mark_started(record['id'], token, key, binding.runtime.id)
                    if record['kind'] == 'independent_review':
                        value = report_review.assess(llm, report, unit, method=binding.settings['method'],
                            max_chars=binding.settings['max_input_chars'])
                    else:
                        draft, _ = invoke_review_request(llm, report['synthesis_request'])
                        value = {'draft': draft.model_dump(), 'request_sha256': report['synthesis_request']['sha256'],
                            'same_draft': draft.model_dump() == report['draft'], 'mode': 'frozen_original_protocol',
                            'output_equality_required': False}
                verify(record)
                store.check_deadline(record['id'], token)
                completed.update(status='succeeded', result=value, error=None)
            except Exception as exc:
                completed.update(status='failed', error=error_record(exc))
                store.finish_item(record['id'], token, key, completed)
                if isinstance(exc, RuntimeUnavailable):
                    results.append(completed)
                    raise
            else:
                store.finish_item(record['id'], token, key, completed)
            results.append(completed)
            tasks.update_task(task_id, progress=5 + int(90 * len(results) / max(1, len(units))),
                              stage=f'已检查 {len(results)}/{len(units)} 条')
        publish_result(task, record, store, units, results, binding)
    except Exception as exc:
        error = error_record(exc)
        if token and units and binding:
            try:
                publish_result(task, record, store, units, results, binding, error)
            except Exception:
                tasks.update_task(task_id, status='failed', stage='综述检查停止，已有检查点保留', error=error)
        else:
            tasks.update_task(task_id, status='failed', stage='综述检查停止，已有检查点保留', error=error)
    finally:
        if token:
            store.release(record['id'], token)


@router.post('/{report_id}/checks', status_code=202)
def start(report_id: str, body: StartCheck, background_tasks: BackgroundTasks):
    try:
        report = source(report_id)
        if body.resume_request_id:
            record = coverage.read(body.resume_request_id)
            if record.get('kind') != body.kind or record.get('report_id') != report_id:
                raise HTTPException(422, detail='继续任务与原综述检查不符。')
        else:
            bundle = report.get('runtime_bindings')
            # 原协议回放绝不补采当前配置；独立审查可显式新建当前版本。
            if body.kind == 'original_replay' and (not bundle or not report.get('synthesis_request')):
                raise RuntimeUnavailable('历史报告缺少实际请求或运行配置，无法按原协议回放。')
            if not bundle:
                bundle = runtime_bindings.capture_bundle()
            record = {'id': 'report_check_request_' + uuid4().hex, 'kind': body.kind,
                'report_id': report_id, 'report_sha256': fingerprint(report), 'runtime_bindings': bundle,
                'budget': {'max_calls': body.max_calls, 'total_seconds': body.total_seconds}, 'created_at': tasks.now()}
            verify(record)
            coverage.store(record)
        verify(record)
        with tasks.task_scope(report['source_task_id']):
            operation = 'report_review' if body.kind == 'independent_review' else 'review'
            task = tasks.create_task(task_type='report_check', deduplicate=True, dedup_key=record['id'],
                stage='等待综述独立审查' if body.kind == 'independent_review' else '等待按原协议回放',
                metadata={'report_check_request_id': record['id'], 'report_id': report_id, 'check_kind': body.kind,
                          **runtime_bindings.metadata(record['runtime_bindings'], operation)})
        if not task.get('_reused'):
            background_tasks.add_task(worker, task['id'])
        return {'task_id': task['id'], 'request_id': record['id'], 'reused': bool(task.get('_reused'))}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None


@router.get('/{report_id}/checks')
def history(report_id: str):
    source(report_id)
    with tasks.connection() as db:
        values = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
    return {'items': sorted([v for v in values if v.get('type') == 'report_check' and v.get('report_id') == report_id],
                            key=lambda v: v['created_at'], reverse=True)}


@router.get('/{report_id}/checks/{result_id}')
def detail(report_id: str, result_id: str):
    result = coverage.read(result_id)
    if result.get('kind') != 'report_check_result' or result.get('report_id') != report_id:
        raise HTTPException(404, detail='综述检查结果不存在。')
    return {**result, 'source_unchanged': fingerprint(source(report_id)) == result['report_sha256']}
