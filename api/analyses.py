"""页面发起论文分析，复用 CLI 的模型、结构化输出及原文证据核验。"""
import json
import logging
import math
import os
from pathlib import Path
import sqlite3
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from agent.analysis_validator import EvidenceValidationError, export_analysis
from agent.llm_runtime import analysis_llm, model_http_backend, model_name, model_endpoint, validate_model_configuration
from agent.codex_model import CodexExecutionError
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from agent.paper_analyzer import StructuredAnalysisError
from agent.batch_analysis import BatchAnalysisFailed, analyze_batches, plan_fingerprint, split_context
from agent.paper_context import assert_document_unchanged, load_paper_context, verify_paper_context as verify_context, DocumentChangedError
from api.analysis_results import complete_analysis, load_result, load_results, with_current_state
from api import tasks
from api.tasks import create_task, update_task, get_task, connection
from storage.analysis_batches import AnalysisBatchStore
from storage.pdf_reparse import ensure_pdf_reading_order
from api.uploads import paper_exists
from storage.database import make_db
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord

router = APIRouter(prefix='/api/v1/analyses', tags=['论文分析'])
ERROR_DIR = Path(__file__).resolve().parents[1] / 'outputs' / 'api_analysis_errors'
logger = logging.getLogger(__name__)


class AnalysisRequest(BaseModel):
    paper_id: int = Field(ge=1)
    max_chunks: int = Field(default=6, ge=2, le=50)
    scope: Literal['full', 'sample', 'fast'] = 'full'
    fast_max_chunks: int = Field(default=18, ge=8, le=50)
    batch_size: int = Field(default=2, ge=1, le=10)
    force_restart: bool = False
    resume_task_id: str | None = None



def model_settings():
    validate_model_configuration()
    try:
        timeout = float(os.getenv('LLM_READ_TIMEOUT', '400'))
        retries = int(os.getenv('LLM_MAX_RETRIES', '0'))
    except ValueError:
        raise ValueError('模型超时或重试次数配置无效。') from None
    if not math.isfinite(timeout) or timeout <= 0 or retries < 0:
        raise ValueError('模型超时须为正数，重试次数不能为负数。')
    method = os.getenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')
    if method not in {'json_schema', 'function_calling'}:
        raise ValueError('STRUCTURED_OUTPUT_METHOD 必须为 json_schema 或 function_calling。')
    return {'timeout': timeout, 'retries': retries, 'method': method}



def safe_source(url):
    return '本地导入 PDF' if url.lower().startswith('file:') else url


def batch_settings():
    try:
        max_chars = int(os.getenv('ANALYSIS_BATCH_MAX_CHARS', '4000'))
        retries = int(os.getenv('ANALYSIS_BATCH_RETRIES', '1'))
    except ValueError:
        raise ValueError('每批字符预算和重试次数须为整数。') from None
    if max_chars < 1 or not 0 <= retries <= 3:
        raise ValueError('每批字符预算须为正数，额外重试次数须为 0–3。')
    return max_chars, retries


def run_analysis(task_id, context, settings, batches=None, run_key=None, fingerprint=None):
    engine = None
    try:
        batches = batches or split_context(context, settings.get('batch_size', 2), settings.get('batch_max_chars', 4000))
        store = AnalysisBatchStore(tasks.TASK_DB_PATH)
        store.prepare(run_key, fingerprint, batches)
        update_task(task_id, status='running', progress=0, stage='正在检查原文并准备分批分析',
                    document_id=context.document_id, selected_chunks=len(context.sources), total_chunks=context.total_chunks,
                    total_batches=len(batches), completed_batches=0, current_batch=1)
        engine, Session = make_db(os.environ['DATABASE_URL'])
        def check(batch):
            with Session.begin() as session:
                verify_context(session, batch)
        check(context)
        def save(batch, index, analysis):
            with Session.begin() as session:
                verify_context(session, batch)
                store.save_completed(run_key, index, analysis)
        def progress(**event):
            index, total, completed = event['index'], event['total'], event['completed']
            label = {'cached': '已保存，直接继续', 'running': '正在请求模型并核验',
                     'completed': '核验通过，已保存', 'retrying': '本批失败，准备只重试这一批'}[event['state']]
            delay = event.get('retry_delay_seconds', 0)
            if delay:
                label = f'连接或服务暂时失败，{delay} 秒后重试本批'
            update_task(task_id, progress=round(90 * completed / total),
                        stage=f'第 {index}/{total} 批：{label}', current_batch=index,
                        completed_batches=completed, total_batches=total,
                        batch_attempt=event['attempt'], resumed_batches=event['resumed'], retry_delay_seconds=delay)
        analysis, summary = analyze_batches(context, batches, run_key=run_key, store=store,
            llm_factory=analysis_llm, read_timeout=settings['timeout'], method=settings['method'],
            batch_retries=settings.get('batch_retries', 1), check_context=check, save_batch=save, progress=progress)
        update_task(task_id, progress=95, stage='各批次均已保存，正在合并并检查完整报告')
        try:
            assert_document_unchanged(os.environ['DATABASE_URL'], context)
        except RuntimeError:
            raise DocumentChangedError() from None
        payload = export_analysis(analysis, context)
        payload['selected_evidence_ids'] = list(context.sources)
        payload['model_name'] = model_name()
        payload['batch_summary'] = dict(summary, batch_size=settings.get('batch_size', 2), scope=settings.get('scope', 'full'))
        payload['document']['source_url'] = safe_source(payload['document']['source_url'])
        for evidence in payload['evidence_index'].values():
            evidence['source_url'] = safe_source(evidence['source_url'])
        with Session.begin() as session:
            verify_context(session, context)
            complete_analysis(task_id, payload)
    except Exception as exc:
        diagnostic = None
        failed_batch = exc.index if isinstance(exc, BatchAnalysisFailed) else None
        if isinstance(exc, BatchAnalysisFailed):
            exc = exc.cause
        if isinstance(exc, CodexExecutionError):
            code, message, diagnostic = exc.code, str(exc), exc.diagnostic
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, settings['timeout'])
            code, message = 'MODEL_TIMEOUT', diagnostic['message'] + ' 已完成批次保留，可继续未完成批次。'
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度及模型配置。'
        elif isinstance(exc, EvidenceValidationError):
            issues = exc.diagnostics.get('issues', [])
            ids = ', '.join(sorted({issue['evidence_id'] for issue in issues}))
            detail = f'（{len(issues)} 处，片段 {ids}）' if issues else ''
            code, message = 'EVIDENCE_VALIDATION_FAILED', '当前批次引用未通过原文核验' + detail + '。请检查引文对照；PDF 文本若有双栏交错须重新解析。已核验批次保留。'
        elif isinstance(exc, StructuredAnalysisError):
            code, message = 'STRUCTURED_ANALYSIS_FAILED', str(exc)
        elif isinstance(exc, DocumentChangedError):
            code, message = 'DOCUMENT_CHANGED', '分析期间全文或片段已更新，未发布本次结果，请重新分析。'
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'ANALYSIS_STORAGE_ERROR', '无法保存分析结果，请检查服务端存储空间和目录权限。'
        elif isinstance(exc, SQLAlchemyError):
            code, message = 'DATABASE_ERROR', '无法读取或核对原文，请检查数据库后重试。'
        else:
            code, message = 'ANALYSIS_FAILED', '分析任务失败，未发布本次结果，请检查服务日志后重试。'
        if diagnostic is not None or isinstance(exc, (EvidenceValidationError, StructuredAnalysisError)):
            try:
                ERROR_DIR.mkdir(parents=True, exist_ok=True)
                (ERROR_DIR / f'{task_id}.json').write_text(json.dumps(diagnostic if diagnostic is not None else exc.diagnostics, ensure_ascii=False, indent=2), encoding='utf-8')
            except OSError:
                logger.warning('无法保存分析诊断：task=%s', task_id)
        log_diagnostic = diagnostic
        if isinstance(exc, StructuredAnalysisError):
            log_diagnostic = {key: exc.diagnostics.get(key) for key in
                              ('method', 'finish_reason', 'parsing_error_type', 'validation_errors')}
        elif isinstance(exc, EvidenceValidationError):
            log_diagnostic = {'issue_count': len(exc.diagnostics.get('issues', [])),
                              'issues': [{key: issue.get(key) for key in ('category', 'finding_index', 'evidence_id', 'reason')}
                                         for issue in exc.diagnostics.get('issues', [])]}
        logger.warning('论文分析失败：task=%s error_type=%s code=%s diagnostic=%s', task_id, type(exc).__name__, code, log_diagnostic)
        try:
            update_task(task_id, status='failed', failed_batch=failed_batch, stage=(f'第 {failed_batch} 批失败；已完成批次已保存' if failed_batch else '论文分析失败；已核验批次保留'), error={'code': code, 'message': message, 'retryable': exc.retryable if isinstance(exc, CodexExecutionError) else True, **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存分析任务失败状态：task=%s', task_id)
    finally:
        if engine is not None:
            engine.dispose()


@router.post('', status_code=202)
def start_analysis(body: AnalysisRequest, background_tasks: BackgroundTasks):
    try:
        paper_exists(body.paper_id)
        settings = model_settings()
        max_chars, batch_retries = batch_settings()
        with connection() as db:
            for row in db.execute('SELECT payload FROM tasks'):
                active = json.loads(row[0])
                if active.get('type') == 'paper_analysis' and active.get('paper_id') == body.paper_id and active.get('status') in {'queued', 'running'}:
                    if active.get('analysis_scope','full')!=body.scope:
                        raise HTTPException(409,detail='该论文已有不同阅读模式的分析正在运行，请完成后再切换模式。')
                    if body.scope=='fast' and active.get('requested_max_chunks')!=body.fast_max_chunks:
                        raise HTTPException(409,detail='该论文已有不同片段预算的快速阅读任务正在运行。')
                    return {'task_id': active['id'], 'reused': True}
        reparsed = ensure_pdf_reading_order(os.environ['DATABASE_URL'], body.paper_id, db_factory=make_db)
        if reparsed:
            logger.info('分析前已更新 PDF 解析与分片：paper=%s document=%s chunks=%s→%s',
                        body.paper_id, reparsed['document_id'], reparsed['previous_chunks'], reparsed['total_chunks'])
        if body.scope=='fast':
            context=load_paper_context(os.environ['DATABASE_URL'],body.paper_id,max_chunks=body.fast_max_chunks,fast=True)
        else:
            context = load_paper_context(os.environ['DATABASE_URL'], body.paper_id,
                                         max_chunks=None if body.scope == 'full' else body.max_chunks)
        batches = split_context(context, body.batch_size, max_chars)
        fingerprint = plan_fingerprint(context, batches, model=model_name(),
            base_url=model_endpoint(), method=settings['method'], scope=body.scope,
            batch_size=body.batch_size, max_chars=max_chars)
        store = AnalysisBatchStore(tasks.TASK_DB_PATH)
        if body.resume_task_id:
            old = get_task(body.resume_task_id)
            if body.force_restart or old.get('type') != 'paper_analysis' or old.get('paper_id') != body.paper_id:
                raise HTTPException(422, detail='不能继续这项任务，请选择对应论文。')
            if old.get('plan_fingerprint') != fingerprint:
                if not reparsed:
                    raise HTTPException(409, detail='原文、模型或分批设置已改变，旧进度不能混用。请重新开始分析。')
                run_key = store.latest_run(fingerprint) or fingerprint
            else:
                run_key = old['batch_run_key']
        elif body.force_restart:
            run_key = fingerprint + '_' + uuid4().hex
        else:
            run_key = store.latest_run(fingerprint) or fingerprint
        settings.update(batch_size=body.batch_size, batch_max_chars=max_chars, batch_retries=batch_retries, scope=body.scope)
        task = create_task(body.paper_id, task_type='paper_analysis', stage='等待分批分析', deduplicate=True,
            metadata={'batch_run_key': run_key, 'plan_fingerprint': fingerprint, 'analysis_scope': body.scope,
                      'batch_size': body.batch_size, 'requested_max_chunks': body.fast_max_chunks if body.scope=='fast' else body.max_chunks,
                      **({'reading_selection':context.reading_selection} if context.reading_selection else {}),
                      'total_batches': len(batches), 'completed_batches': 0, 'selected_chunks': len(context.sources),
                      'total_chunks': context.total_chunks, 'batch_retries': batch_retries,
                      'model_name': model_name(), 'http_backend': model_http_backend(), 'read_timeout': settings['timeout'],
                      **({'source_reparsed': reparsed} if reparsed else {})})
        if not task.get('_reused'):
            background_tasks.add_task(run_analysis, task['id'], context, settings, batches, run_key, fingerprint)
        return {'task_id': task['id'], 'reused': bool(task.get('_reused')),
                **({'source_reparsed': True} if reparsed else {})}
    except SQLAlchemyError:
        raise HTTPException(503, detail='暂时无法读取论文原文，请检查数据库。') from None
    except ValueError as exc:
        message = str(exc)
        if '尚未关联全文' in message:
            message = '这篇论文尚未导入全文，请先上传 PDF 或获取原文。'
        elif '文档没有原文片段' in message:
            message = '全文没有可分析的片段，请重新解析 PDF；扫描版可能需要先做 OCR。'
        raise HTTPException(422, detail=message) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建分析任务，请检查本地存储。') from None


@router.get('')
def list_analyses(paper_id: int | None = Query(default=None, ge=1)):
    return read_analyses(load_paper_id=paper_id)


def read_analyses(load_paper_id=None, analysis_id=None):
    engine = None
    try:
        if analysis_id is not None:
            payload = load_result(analysis_id)
            if payload is None:
                raise HTTPException(404, detail='分析结果不存在。')
            payloads = [payload]
        else:
            payloads = load_results(load_paper_id)
        if not payloads:
            return []
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session() as session:
            values = with_current_state(payloads, session)
        return values[0] if analysis_id is not None else values
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取分析历史或核对原文版本。') from None
    finally:
        if engine is not None:
            engine.dispose()


@router.get('/progress/{paper_id:int}')
def analysis_progress(paper_id: int):
    try:
        with connection() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
        candidates = [row for row in rows if row.get('paper_id') == paper_id and row.get('type') == 'paper_analysis']
        return max(candidates, key=lambda row: row['created_at']) if candidates else None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取分批进度。') from None


@router.get('/tasks/{task_id}/batches')
def analysis_batch_steps(task_id: str):
    task = get_task(task_id)
    if task.get('type') != 'paper_analysis':
        raise HTTPException(422, detail='这不是论文分析任务。')
    try:
        return {'task_id': task_id, 'batches': AnalysisBatchStore(tasks.TASK_DB_PATH).steps(task['batch_run_key']) if task.get('batch_run_key') else []}
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取已保存的批次。') from None


@router.get('/{analysis_id}')
def get_analysis(analysis_id: str):
    return read_analyses(analysis_id=analysis_id)
