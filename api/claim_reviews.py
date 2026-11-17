"""固定分析版本的辅助语义评审；独立任务、逐条检查点，不改写分析。"""
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from agent.analysis_schema import PaperAnalysis
from agent.analysis_validator import CATEGORIES, EvidenceSource, PaperContext, validate_analysis
from agent.claim_verifier import POLICY_VERSION, ReviewPrecheckError, prepare_claim
from agent.claim_reference_verifier import QUOTE_PROTOCOL
from agent.llm_runtime import analysis_llm, model_name, model_endpoint, model_http_backend
from agent.paper_context import verify_paper_context, DocumentChangedError
from api.analyses import model_settings, safe_source
from api.analysis_results import load_result
from api.tasks import connection, now, get_task
from evaluation.claim_quality import (fingerprint, run_reviews, review_run_key, error_info, checked_cache,
                                      atomic_json, request_matches)
from storage.database import make_db
from storage.db_models import PaperRecord, PaperDocumentRecord

router = APIRouter(prefix='/api/v1', tags=['辅助语义评审'])
RUN_ROOT = Path(__file__).resolve().parents[1] / 'outputs' / 'claim_reviews'
CONTEXT_POLICY = 'cited-chunks-v1'


class SourcesChanged(ReviewPrecheckError):
    code = 'DOCUMENT_CHANGED'


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS claim_reviews (
        id TEXT PRIMARY KEY, analysis_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS claim_reviews_analysis ON claim_reviews(analysis_id, created_at)')


def read_record(db, identifier):
    row = db.execute('SELECT payload FROM claim_reviews WHERE id=?', (identifier,)).fetchone()
    return json.loads(row[0]) if row else None


def verify_snapshot(snapshot):
    """核对当前数据库的身份、哈希、全部片段、页码和逐字引文。"""
    engine = None
    try:
        if load_result(snapshot['id']) != snapshot:
            raise SourcesChanged('分析版本已改变，请重新选择分析。')
        engine, Session = make_db(os.environ['DATABASE_URL'])
        with Session.begin() as session:
            document = session.scalar(select(PaperDocumentRecord).where(
                PaperDocumentRecord.id == snapshot['document']['id']).with_for_update())
            paper = session.get(PaperRecord, snapshot['paper']['id'])
            if (document is None or paper is None or paper.title != snapshot['paper']['title']
                    or paper.doi != snapshot['paper'].get('doi')
                    or safe_source(document.source_url) != snapshot['document']['source_url']):
                raise SourcesChanged('论文身份或全文来源已改变，请重新分析。')
            if document.source_type != 'pdf':
                raise ReviewPrecheckError('首版辅助评审支持带页码的 PDF 分析；当前全文不是 PDF。')
            index = snapshot['evidence_index']
            if any(not re.fullmatch(r'E[1-9][0-9]*', key) or value['source_url'] != snapshot['document']['source_url']
                   for key, value in index.items()):
                raise SourcesChanged('分析中的证据编号或来源无效。')
            context = PaperContext(paper.id, paper.title, paper.doi, document.id,
                snapshot['document']['content_sha256'], document.source_url, snapshot['document']['total_chunks'],
                {key: EvidenceSource(key, value['text'], document.source_url, value.get('page_number'), value.get('section_hint'))
                 for key, value in index.items()})
            verify_paper_context(session, context)
            validate_analysis(PaperAnalysis.model_validate(snapshot['analysis']), context)
    except (DocumentChangedError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ReviewPrecheckError):
            raise
        raise SourcesChanged('分析、全文或证据片段已变化，旧评审仅供历史查看，请重新分析。') from None
    finally:
        if engine is not None:
            engine.dispose()


def build_items(snapshot):
    analysis = PaperAnalysis.model_validate(snapshot['analysis'])
    items = []
    for category in CATEGORIES:
        for index, finding in enumerate(getattr(analysis, category)):
            evidence = {}
            for ref in finding.evidence:
                source = snapshot['evidence_index'][ref.evidence_id]
                if ref.evidence_id in evidence:
                    # 同片段多条引句使用完整真实片段，全部保留且不拼接引句。
                    evidence[ref.evidence_id]['quote'] = source['text']
                else:
                    evidence[ref.evidence_id] = {'evidence_id': ref.evidence_id, 'page_number': source['page_number'],
                        'quote': ref.quote, 'context': source['text']}
            payload = {'sample_id': f'{category}-{index}', 'paper_title': snapshot['paper']['title'],
                       'statement': finding.statement, 'evidence': list(evidence.values())}
            payload['input_hash'] = fingerprint(payload)
            items.append(dict(payload, category=category, finding_index=index))
    return items


def configuration(snapshot, settings):
    tokens = int(os.getenv('CLAIM_REVIEW_MAX_OUTPUT_TOKENS', '2000'))
    max_chars = int(os.getenv('CLAIM_REVIEW_MAX_INPUT_CHARS', '24000'))
    if tokens < 1 or max_chars < 1:
        raise ValueError('辅助评审输入和输出预算必须为正整数。')
    endpoint = model_endpoint()
    parsed = urlsplit(endpoint)
    return {'policy_version': POLICY_VERSION, 'quote_protocol': QUOTE_PROTOCOL,
        'analysis_id': snapshot['id'], 'snapshot_sha256': fingerprint(snapshot),
        'context_policy': CONTEXT_POLICY, 'provider': model_http_backend(), 'model_name': model_name(),
        'endpoint': parsed.scheme + '://' + (parsed.hostname or ''), 'endpoint_sha256': fingerprint(endpoint),
        'timeout': settings['timeout'], 'sdk_retries': settings['retries'], 'structured_method': settings['method'],
        'max_output_tokens': tokens, 'max_input_chars': max_chars, 'trust_env': True,
        'runtime_options_sha256': fingerprint({key: os.getenv(key, '') for key in
            ('LLM_ENABLE_THINKING', 'LLM_MAX_OUTPUT_TOKENS', 'CODEX_USE_SYSTEM_PROXY')})}


def item_results(record):
    folder = RUN_ROOT / record['id'] / record['run_key'][:20] / 'items'
    values = []
    for item in record['items']:
        path = folder / f"{item['sample_id']}.json"
        try:
            saved = json.loads(path.read_text()) if path.exists() else {}
            if not isinstance(saved, dict):
                raise ValueError('无效的评审记录')
        except (ValueError, TypeError):
            saved = {'status': 'failed', 'error': {'code': 'CLAIM_REVIEW_STORAGE_ERROR', 'message': '评审检查点损坏，请继续评审以恢复。'}}
        if saved.get('status') == 'succeeded':
            try:
                valid = checked_cache(saved, record['run_key'], item, prepare_claim(item, max_input_chars=record['config']['max_input_chars']),
                                      quote_protocol=record['config'].get('quote_protocol', 'verbatim-v1'))
            except ReviewPrecheckError:
                valid = False
            if not valid:
                saved = {'status': 'failed', 'error': {'code': 'CLAIM_REVIEW_INVALID', 'message': '已保存评审未通过输入与引文核验，请继续评审。'}}
        error = saved.get('error')
        values.append(dict(item, status=saved.get('status', 'not_attempted'), review=saved.get('review'),
            error={key: error[key] for key in ('code', 'message') if key in error} if error else None,
            quote_repairs=saved.get('quote_repairs', []),
            quote_protocol=saved.get('quote_protocol', record['config'].get('quote_protocol', 'verbatim-v1')),
            legacy_source_review_id=(saved.get('legacy_import') or {}).get('source_review_id')))
    return values


def public_record(record):
    try:
        verify_snapshot(record['snapshot'])
        current = True
    except ReviewPrecheckError:
        current = False
    values = item_results(record)
    return {key: record[key] for key in ('id', 'analysis_id', 'paper_id', 'document_id', 'content_sha256',
            'created_at', 'model_name', 'context_policy', 'policy_version')} | {
        'task': get_task(record['task_id']), 'is_current': current, 'items': values,
        'quote_protocol': record['config'].get('quote_protocol', 'verbatim-v1'),
        'legacy_reused': sum(v['status'] == 'succeeded' and v['quote_protocol'] == 'legacy-import' for v in values),
        'total': len(values), 'completed': sum(v['status'] == 'succeeded' for v in values)}


def import_legacy_results(db, target):
    """显式复用同材料同配置的旧成功结果，保留原始请求与协议来源。"""
    if target['config'].get('quote_protocol') != QUOTE_PROTOCOL:
        return 0
    rows = db.execute('SELECT payload FROM claim_reviews WHERE analysis_id=? AND id<>? ORDER BY created_at DESC',
                      (target['analysis_id'], target['id'])).fetchall()
    imported = set()
    for row in rows:
        source = json.loads(row[0])
        if source['config'].get('quote_protocol', 'verbatim-v1') != 'verbatim-v1':
            continue
        source_config, target_config = dict(source['config']), dict(target['config'])
        source_config.pop('quote_protocol', None)
        target_config.pop('quote_protocol', None)
        if source_config != target_config or source['snapshot'] != target['snapshot'] or source['items'] != target['items']:
            continue
        expected = review_run_key(source['items'], source['config'], method=source['config']['structured_method'],
                                  max_input_chars=source['config']['max_input_chars'])
        if expected != source['run_key']:
            continue
        task_row = db.execute('SELECT payload FROM tasks WHERE id=?', (source['task_id'],)).fetchone()
        if task_row and json.loads(task_row[0])['status'] in {'queued', 'running'}:
            raise HTTPException(409, detail='旧版评审仍在运行，请等待本轮结束后再切换引用方式。')
        source_dir = RUN_ROOT / source['id'] / expected[:20]
        target_dir = RUN_ROOT / target['id'] / target['run_key'][:20]
        for item in target['items']:
            key = item['sample_id']
            if key in imported:
                continue
            try:
                claim = prepare_claim(item, max_input_chars=target['config']['max_input_chars'])
                saved = json.loads((source_dir/'items'/f'{key}.json').read_text())
                request = json.loads((source_dir/'requests'/f'{key}.json').read_text())
                if not checked_cache(saved, expected, item, claim) or not request_matches(request, claim, item, 'verbatim-v1'):
                    continue
                value = dict(saved, run_key=target['run_key'], quote_protocol='legacy-import', resumed=True,
                    legacy_import={'source_review_id': source['id'], 'source_run_key': expected,
                                   'source_record': saved, 'source_request': request}, updated_at=now())
                if not checked_cache(value, target['run_key'], item, claim, quote_protocol=QUOTE_PROTOCOL):
                    continue
                atomic_json(target_dir/'items'/f'{key}.json', value)
                atomic_json(target_dir/'requests'/f'{key}.json', dict(request, imported_from_review=source['id']))
                imported.add(key)
            except (ValueError, KeyError, TypeError, FileNotFoundError):
                continue
    return len(imported)


def owned_update(record, task_id, **changes):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        active = read_record(db, record['id'])
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        task = json.loads(row[0]) if row else None
        if not active or active['task_id'] != task_id or not task or task['status'] not in {'queued', 'running'}:
            raise RuntimeError('评审任务已结束或被替代')
        task.update(changes, updated_at=now())
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))


def run_job(record, task_id):
    try:
        owned_update(record, task_id, status='running', stage='核对固定分析版本与原文', progress=0)
        verify_snapshot(record['snapshot'])
        config = record['config']
        @contextmanager
        def factory():
            with analysis_llm(read_timeout=config['timeout'], retries=config['sdk_retries'], trust_env=True,
                              max_output_tokens=config['max_output_tokens']) as llm:
                yield llm
        def check(item):
            verify_snapshot(record['snapshot'])
        def progress(number, total, key, status):
            labels = {'requesting': '正在请求模型', 'succeeded': '已核验保存', 'resumed': '复用已核验结果',
                      'recovered': '已恢复保存的候选', 'failed': '本条未完成',
                      'precheck_failed': '本条原文核验失败', 'not_attempted': '本条尚未请求'}
            owned_update(record, task_id, progress=round(95 * (number - (status == 'requesting')) / total),
                stage=f'第 {number}/{total} 条：{labels[status]}')
        _, results = run_reviews(record['items'], factory, RUN_ROOT / record['id'], config,
            method=config['structured_method'], max_input_chars=config['max_input_chars'],
            progress=progress, verify_input=check)
        verify_snapshot(record['snapshot'])
        completed = sum(r['status'] == 'succeeded' for r in results)
        failed = next((r.get('error') for r in results if r['status'] != 'succeeded'), None)
        owned_update(record, task_id, status='succeeded' if completed == len(results) else 'failed', progress=100,
            result_id=record['id'], completed_claims=completed, total_claims=len(results),
            stage=f'辅助评审完成：{completed}/{len(results)} 条' if not failed else f'已保存 {completed}/{len(results)} 条，可继续未完成项',
            error={key: failed[key] for key in ('code', 'message') if key in failed} | {'retryable': True} if failed else None)
    except Exception as exc:
        if isinstance(exc, (sqlite3.Error, OSError, SQLAlchemyError)):
            error = {'code': 'CLAIM_REVIEW_STORAGE_ERROR', 'message': '无法读取原文或保存评审，请检查数据库与本地存储。'}
        else:
            error, _ = error_info(exc)
        try:
            owned_update(record, task_id, status='failed', stage='评审未完成，已保存结果保留',
                         error={key: error[key] for key in ('code', 'message')} | {'retryable': True})
        except (RuntimeError, sqlite3.Error, OSError):
            pass


@router.post('/analyses/{analysis_id}/claim-reviews', status_code=202)
def start_review(analysis_id: str, background_tasks: BackgroundTasks):
    try:
        snapshot = load_result(analysis_id)
        if snapshot is None:
            raise HTTPException(404, detail='分析版本不存在。')
        verify_snapshot(snapshot)
        items = build_items(snapshot)
        if not items:
            raise HTTPException(422, detail='这份分析没有可评审的结论。')
        settings = model_settings()
        config = configuration(snapshot, settings)
        key = review_run_key(items, config, method=config['structured_method'], max_input_chars=config['max_input_chars'])
        identifier = 'claim_review_' + key
        with connection() as db:
            initialize(db)
            db.execute('BEGIN IMMEDIATE')
            existing = read_record(db, identifier)
            if existing:
                row = db.execute('SELECT payload FROM tasks WHERE id=?', (existing['task_id'],)).fetchone()
                task = json.loads(row[0]) if row else None
                if task and (task['status'] in {'queued', 'running'} or
                             task['status'] == 'succeeded' and all(v['status'] == 'succeeded' for v in item_results(existing))):
                    return {'task_id': task['id'], 'review_id': identifier, 'reused': True}
            task_id, timestamp = uuid4().hex, now()
            record = existing or {'id': identifier, 'analysis_id': analysis_id, 'paper_id': snapshot['paper']['id'],
                'document_id': snapshot['document']['id'], 'content_sha256': snapshot['document']['content_sha256'],
                'created_at': timestamp, 'snapshot': deepcopy(snapshot), 'items': items, 'config': config, 'run_key': key,
                'model_name': config['model_name'], 'policy_version': POLICY_VERSION, 'context_policy': CONTEXT_POLICY}
            if not existing:
                record['legacy_imported_claims'] = import_legacy_results(db, record)
            record['task_id'] = task_id
            task = {'id': task_id, 'type': 'claim_review', 'paper_id': record['paper_id'], 'analysis_id': analysis_id,
                'claim_review_id': identifier, 'status': 'queued', 'progress': 0, 'stage': '等待逐条辅助语义评审',
                'result_id': None, 'error': None, 'model_name': config['model_name'], 'http_backend': model_http_backend(),
                'read_timeout': config['timeout'], 'created_at': timestamp, 'updated_at': timestamp}
            db.execute('INSERT OR REPLACE INTO claim_reviews VALUES (?,?,?,?)',
                       (identifier, analysis_id, record['created_at'], json.dumps(record, ensure_ascii=False)))
            db.execute('INSERT INTO tasks VALUES (?,?)', (task_id, json.dumps(task, ensure_ascii=False)))
        background_tasks.add_task(run_job, record, task_id)
        return {'task_id': task_id, 'review_id': identifier, 'reused': False}
    except SourcesChanged as exc:
        raise HTTPException(409, detail=str(exc)) from None
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, detail=str(exc) if isinstance(exc, ReviewPrecheckError) else '评审配置或分析材料无效。') from None
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取原文或保存评审任务。') from None


@router.get('/analyses/{analysis_id}/claim-reviews')
def list_reviews(analysis_id: str):
    try:
        if load_result(analysis_id) is None:
            raise HTTPException(404, detail='分析版本不存在。')
        with connection() as db:
            initialize(db)
            rows = db.execute('SELECT payload FROM claim_reviews WHERE analysis_id=? ORDER BY created_at DESC, id DESC', (analysis_id,)).fetchall()
        return {'items': [public_record(json.loads(row[0])) for row in rows]}
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取辅助评审结果或核对原文状态。') from None


@router.get('/claim-reviews/{identifier}')
def get_review(identifier: str):
    try:
        with connection() as db:
            initialize(db)
            record = read_record(db, identifier)
        if record is None:
            raise HTTPException(404, detail='辅助评审不存在。')
        return public_record(record)
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取辅助评审或核对原文状态。') from None
