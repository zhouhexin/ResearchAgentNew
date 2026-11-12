"""少量片段一批；逐批核验与保存；最后直接合并五类结论。"""
from dataclasses import replace
import hashlib
import json
import logging
from time import sleep

from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import ValidationError

from agent.analysis_schema import PaperAnalysis
from agent.analysis_validator import CATEGORIES, EvidenceValidationError, validate_analysis
from agent.paper_analyzer import SYSTEM_PROMPT, StructuredAnalysisError, analyze_paper, analysis_retry_feedback
from agent.model_diagnostics import model_connection_diagnostic
from agent.codex_model import CodexExecutionError

logger = logging.getLogger(__name__)


class BatchAnalysisFailed(Exception):
    def __init__(self, index, cause):
        super().__init__(f'第 {index} 批分析失败')
        self.index, self.cause = index, cause


def split_context(context, batch_size=2, max_chars=4000):
    if not 1 <= batch_size <= 10 or max_chars < 1:
        raise ValueError('每批片段数须为 1–10，字符预算须为正数。')
    batches, selected, size = [], {}, 0
    for key, source in context.sources.items():
        if len(source.text) > max_chars:
            raise ValueError('有单个原文片段超过每批字符预算，请增大 ANALYSIS_BATCH_MAX_CHARS 或重新分片；不会跳过这段原文。')
        if selected and (len(selected) >= batch_size or size + len(source.text) > max_chars):
            batches.append(replace(context, sources=selected))
            selected, size = {}, 0
        selected[key] = source
        size += len(source.text)
    if selected:
        batches.append(replace(context, sources=selected))
    if not batches:
        raise ValueError('没有可分析的原文片段。')
    return batches


def plan_fingerprint(context, batches, *, model, base_url, method, scope, batch_size=None, max_chars=None):
    data = {'version': 'batch-analysis-v1', 'prompt': SYSTEM_PROMPT,
            'schema': PaperAnalysis.model_json_schema(), 'model': model, 'base_url': base_url,
            'method': method, 'scope': scope, 'batch_size': batch_size, 'max_chars': max_chars, 'paper_id': context.paper_id,
            'document_id': context.document_id, 'hash': context.document_sha256,
            'title': context.paper_title, 'doi': context.doi, 'total_chunks': context.total_chunks,
            'batches': [[{'id': key, 'text': source.text, 'page': source.page_number,
                         'section': source.section_hint, 'source': source.source_url}
                        for key, source in batch.sources.items()] for batch in batches]}
    if scope=='fast':data['reading_selection']=context.reading_selection
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def merge_analyses(analyses, context):
    grouped = {category: [] for category in CATEGORIES}
    for category in CATEGORIES:
        by_statement = {}
        for analysis in analyses:
            for finding in getattr(analysis, category):
                key = finding.statement.strip()
                if key not in by_statement:
                    item = finding.model_copy(deep=True)
                    by_statement[key] = item
                    grouped[category].append(item)
                else:
                    item = by_statement[key]
                    seen = {(ref.evidence_id, ref.quote) for ref in item.evidence}
                    for ref in finding.evidence:
                        if (ref.evidence_id, ref.quote) not in seen:
                            item.evidence.append(ref.model_copy(deep=True))
                            seen.add((ref.evidence_id, ref.quote))
    merged = PaperAnalysis(**grouped)
    validate_analysis(merged, context)
    return merged


def retryable(exc):
    if isinstance(exc, CodexExecutionError):
        return exc.retryable
    if isinstance(exc, APIStatusError):
        return exc.status_code in {408, 409, 429} or exc.status_code >= 500
    return isinstance(exc, (APITimeoutError, APIConnectionError, EvidenceValidationError, StructuredAnalysisError))


def retry_delay(exc, attempt):
    # 网络和上游服务错误需要恢复时间；格式/引用修正可以立即发起。
    if isinstance(exc, (APIConnectionError, APIStatusError, CodexExecutionError)):
        return min(2 ** attempt, 8)
    return 0


def analyze_batches(context, batches, *, run_key, store, llm_factory, read_timeout, method,
                    batch_retries=1, check_context=None, save_batch=None, progress=None):
    if not 0 <= batch_retries <= 3:
        raise ValueError('每批额外重试次数须为 0–3。')
    progress = progress or (lambda **kwargs: None)
    results, resumed = [], 0
    for index, batch in enumerate(batches, 1):
        if check_context:
            check_context(batch)
        cached = store.get_completed(run_key, index)
        if cached is not None:
            try:
                cached = PaperAnalysis.model_validate(cached)
                validate_analysis(cached, batch)
            except (ValidationError, EvidenceValidationError):
                cached = None
        if cached is not None:
            results.append(cached)
            resumed += 1
            progress(index=index, completed=len(results), total=len(batches), attempt=0, resumed=resumed, state='cached')
            continue
        feedback = store.get_feedback(run_key,index)
        for attempt in range(1, batch_retries + 2):
            progress(index=index, completed=len(results), total=len(batches), attempt=attempt, resumed=resumed, state='running')
            store.begin_attempt(run_key, index)
            try:
                # SDK 不再叠加重试；每批的尝试次数由本层统一控制。
                with llm_factory(read_timeout=read_timeout, retries=0, trust_env=True) as llm:
                    analysis = analyze_paper(llm, batch, method=method, max_repair_attempts=0, retry_feedback=feedback,allow_quote_selection=True)
                if save_batch:
                    save_batch(batch, index, analysis)
                else:
                    if check_context:
                        check_context(batch)
                    store.save_completed(run_key, index, analysis)
            except Exception as exc:
                if isinstance(exc, (EvidenceValidationError, StructuredAnalysisError)):
                    feedback = analysis_retry_feedback(exc)
                store.failed(run_key, index, type(exc).__name__,feedback=feedback)
                if isinstance(exc, APIConnectionError):
                    logger.warning('模型请求失败：batch=%s/%s attempt=%s error_type=%s diagnostic=%s',
                                   index, len(batches), attempt, type(exc).__name__, model_connection_diagnostic(exc))
                if not retryable(exc) or attempt > batch_retries:
                    raise BatchAnalysisFailed(index, exc) from exc
                delay = retry_delay(exc, attempt)
                progress(index=index, completed=len(results), total=len(batches), attempt=attempt, resumed=resumed,
                         state='retrying', retry_delay_seconds=delay)
                if delay:
                    sleep(delay)
                continue
            results.append(analysis)
            progress(index=index, completed=len(results), total=len(batches), attempt=attempt, resumed=resumed, state='completed')
            break
    merged = merge_analyses(results, context)
    return merged, {'total_batches': len(batches), 'completed_batches': len(results), 'resumed_batches': resumed}
