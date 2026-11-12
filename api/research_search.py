"""研究计划的检索与初筛执行：版本快照、持久化检查点和恢复。"""
import hashlib
import json
import logging
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field
import requests

from agent.codex_model import CodexExecutionError
from agent.llm_runtime import analysis_llm, model_name, model_endpoint
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from agent.research_search import SEARCH_VERSION, SCREEN_PROMPT, ScreeningBatch, ScreeningError, build_search_graph
from api.analyses import model_settings
from api.research_plans import initialize as initialize_plans
from api.tasks import connection, now, update_task
from tools.arxiv_search import search_arxiv_page
from tools.venue_search.cvpr import search_cvpr_page, search_venue_page, VERSION as CVPR_VERSION, VENUE_SEARCH_VERSION, PROCEEDINGS_VERSION
from tools.open_access_pdf import FulltextError
from tools.venue_search.catalog import selected_venues, validate_venues, supported_sources
from tools.venue_search.extended import VERSION as EXTENDED_VERSION, SLUGS as EXTENDED_SLUGS

router = APIRouter(prefix='/api/v1/agent/plans', tags=['计划检索与初筛'])
logger = logging.getLogger(__name__)


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    per_query: int = Field(default=10, ge=1, le=20)
    refresh: bool = False


def initialize(db):
    initialize_plans(db)
    db.execute('''CREATE TABLE IF NOT EXISTS research_search_runs (
        id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, revision INTEGER NOT NULL,
        fingerprint TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS research_search_plan ON research_search_runs(plan_id, revision)')


def read_plan(db, plan_id):
    row = db.execute('SELECT payload FROM research_plans WHERE id=?', (plan_id,)).fetchone()
    if not row:
        raise HTTPException(404, detail='研究计划不存在。')
    return json.loads(row[0])


def public_run(db, run):
    # 服务重启时通用任务表会标记中断，检查点保持原样，读取时以任务状态为准。
    row = db.execute('SELECT payload FROM tasks WHERE id=?', (run['task_id'],)).fetchone()
    if row:
        task = json.loads(row[0])
        run.update(status=task['status'], error=task['error'])
    return run


def write_checkpoint(run, task_id, *, complete=False):
    with connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM research_search_runs WHERE id=?', (run['id'],)).fetchone()
        task_row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not row or not task_row or json.loads(row[0])['task_id'] != task_id:
            raise RuntimeError('检索执行已被新的任务替代')
        task = json.loads(task_row[0])
        if task['status'] not in {'queued', 'running'}:
            raise RuntimeError('检索任务已结束')
        run.update(status='succeeded' if complete else 'running', updated_at=now(), error=None)
        db.execute('UPDATE research_search_runs SET payload=? WHERE id=?', (json.dumps(run, ensure_ascii=False), run['id']))
        if complete:
            task.update(status='succeeded', progress=100, stage='文献检索与摘要初筛完成，等待人工确认',
                        result_id=run['id'], error=None, updated_at=now())
            db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))


def run_search(task_id, run, settings):
    try:
        update_task(task_id, status='running', progress=2, stage='正在恢复已完成的检索与初筛检查点')
        with analysis_llm(read_timeout=settings['timeout'], retries=settings['retries'], trust_env=True,
                          max_output_tokens=6000) as llm:
            slugs = selected_venues(run)
            if slugs == ['cvpr']:
                fetch = lambda query, **kwargs: search_cvpr_page(query, **kwargs, verified_main_only=run.get('verified_main_only', False))
            elif slugs:
                fetch = lambda query, **kwargs: search_venue_page(query, venue_slugs=slugs, **kwargs, verified_main_only=run.get('verified_main_only', False))
            else:
                fetch = search_arxiv_page
            graph = build_search_graph(llm, fetch=fetch,
                checkpoint=lambda value: write_checkpoint(value, task_id),
                progress=lambda **event: update_task(task_id, **event), method=settings['method'])
            result = graph.invoke({'run': run}, config={'recursion_limit': 5})
        write_checkpoint(result['run'], task_id, complete=True)
    except Exception as exc:
        diagnostic, retryable = None, True
        if isinstance(exc, CodexExecutionError):
            code, message, diagnostic, retryable = exc.code, str(exc), exc.diagnostic, exc.retryable
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, settings['timeout'])
            code, message = 'MODEL_TIMEOUT', diagnostic['message']
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度或模型配置。'
        elif isinstance(exc, requests.Timeout):
            code, message = 'SEARCH_TIMEOUT', '文献来源检索超时，已完成结果保留，可继续未完成部分。'
        elif isinstance(exc, requests.RequestException):
            code, message = 'SEARCH_CONNECTION_ERROR', '文献来源连接失败，请稍后继续未完成部分。'
        elif isinstance(exc, FulltextError):
            code, message = exc.code, str(exc)
        elif isinstance(exc, ScreeningError):
            code, message = 'SCREENING_VALIDATION_FAILED', str(exc)
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'SEARCH_STORAGE_ERROR', '无法保存检索检查点，请检查服务端存储。'
        else:
            code, message = 'RESEARCH_SEARCH_FAILED', '检索或初筛失败，已完成部分保留，可继续重试。'
        logger.warning('计划检索失败：task=%s code=%s error_type=%s', task_id, code, type(exc).__name__)
        try:
            update_task(task_id, status='failed', stage='文献检索或初筛中断，已完成部分保留',
                        error={'code': code, 'message': message, 'retryable': retryable,
                               **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存检索失败状态：task=%s', task_id)


@router.post('/{plan_id}/search', status_code=202)
def start_search(plan_id: str, body: SearchRequest, background_tasks: BackgroundTasks):
    try:
        settings = model_settings()
        fingerprint = hashlib.sha256(json.dumps([SEARCH_VERSION, SCREEN_PROMPT, ScreeningBatch.model_json_schema(),
                    model_name(), model_endpoint(), settings['method'], body.per_query], sort_keys=True).encode()).hexdigest()
        with connection() as db:
            initialize(db)
            db.execute('BEGIN IMMEDIATE')
            saved = read_plan(db, plan_id)
            if saved['revision'] != body.expected_revision:
                raise HTTPException(409, detail='计划版本已改变，请重新读取并确认后执行。')
            plan = saved['plan']
            actions = [step['action'] for step in plan['steps']]
            if 'search_papers' not in actions or 'screen_papers' not in actions or not plan['search_queries']:
                raise HTTPException(422, detail='本阶段需要计划包含检索词、检索论文与筛选步骤，请调整研究目标后重新制定计划。')
            venue = plan['scope'].get('venue_slug')
            slugs = selected_venues(plan['scope'])
            if slugs:
                validate_venues(slugs)
            if body.refresh and not slugs:
                raise HTTPException(422, detail='重新核验入口仅适用于会议/期刊检索。')
            if slugs == ['cvpr']:
                fingerprint = hashlib.sha256(json.dumps([fingerprint, CVPR_VERSION, plan['scope'].get('verified_main_only', False)]).encode()).hexdigest()
            if slugs and slugs != ['cvpr']:
                fingerprint = hashlib.sha256(json.dumps([fingerprint, VENUE_SEARCH_VERSION, slugs, plan['scope'].get('verified_main_only', False)]).encode()).hexdigest()
            if any(s in {'aaai','ijcai'} for s in slugs):
                fingerprint = hashlib.sha256(json.dumps([fingerprint,PROCEEDINGS_VERSION]).encode()).hexdigest()
            if EXTENDED_SLUGS.intersection(slugs):
                fingerprint = hashlib.sha256(json.dumps([fingerprint,EXTENDED_VERSION]).encode()).hexdigest()
            supported = supported_sources(slugs)
            if any(source.strip().casefold() not in supported for source in plan['scope']['requested_sources']):
                raise HTTPException(422, detail='当前支持 arXiv 或已接通会议/期刊的 OpenAlex/正式来源检索，请修改计划的会议与来源设置后保存。')
            # 不跳过需要先完成的其他工具动作；本阶段执行范围固定。
            if any(step['action'] not in {'search_papers', 'screen_papers'} for step in plan['steps'][:actions.index('screen_papers')]):
                raise HTTPException(422, detail='筛选步骤之前包含尚未接通的计划动作，本阶段无法按该依赖执行。')
            if actions.index('search_papers') > actions.index('screen_papers'):
                raise HTTPException(422, detail='计划中的筛选步骤必须安排在检索步骤之后。')
            rows = db.execute('SELECT payload FROM research_search_runs WHERE plan_id=? AND revision=?', (plan_id, body.expected_revision)).fetchall()
            runs = sorted((public_run(db, json.loads(row[0])) for row in rows), key=lambda r: r['created_at'], reverse=True)
            # 同一计划版本不能并行修改结果，即使更改模型或检索预算。
            active = next((run for run in runs if run['status'] in {'queued', 'running'}), None)
            if active:
                if active['fingerprint'] != fingerprint:
                    raise HTTPException(409, detail='该计划版本已有检索任务运行，请等待完成后调整配置。')
                return {'task_id': active['task_id'], 'run_id': active['id'], 'reused': True}
            run = None if body.refresh else next((run for run in runs if run['fingerprint'] == fingerprint), None)
            if run and run['status'] == 'succeeded':
                return {'task_id': run['task_id'], 'run_id': run['id'], 'reused': True}
            task_id, timestamp = uuid4().hex, now()
            if not run:
                run = {'id': 'search_' + uuid4().hex, 'plan_id': plan_id, 'revision': saved['revision'],
                       'fingerprint': fingerprint, 'goal': saved['goal'], 'plan': plan, 'per_query': body.per_query,
                       'created_at': timestamp, 'queries': [{'number': i, 'query': query, 'status': 'pending', 'items': [], 'total': None}
                                   for i, query in enumerate(plan['search_queries'], 1)],
                       'candidates': [], 'screenings': {}, 'duplicate_count': 0, 'skipped_count': 0, 'attempts': 0,
                       'model_name': model_name(), 'source': 'openalex' if slugs else 'arxiv',
                       'venue_slug': slugs[0] if len(slugs)==1 else None, 'venue_slugs': slugs, 'verified_main_only': plan['scope'].get('verified_main_only', False), 'screening_basis': 'title_abstract',
                       'notice': ('、'.join(s.upper() for s in slugs)+' 通过 OpenAlex 检索，逐篇用对应官方论文集核验发表身份，按正式发表年份过滤；主会筛选只作用于本轮候选。' if slugs else
                                 '本轮只检索每个检索词的前若干条 arXiv 结果，按首次提交年份过滤。') + '初筛仅依据标题和摘要，需人工确认，不代表全文核验或完整领域查新。'}
            run.update(task_id=task_id, status='queued', error=None, updated_at=timestamp, attempts=run['attempts']+1)
            task = {'id': task_id, 'type': 'research_search', 'paper_id': None, 'plan_id': plan_id,
                    'plan_revision': saved['revision'], 'search_run_id': run['id'], 'research_goal': saved['goal'],
                    'status': 'queued', 'stage': '等待执行计划检索与摘要初筛', 'progress': 0, 'result_id': None,
                    'error': None, 'created_at': timestamp, 'updated_at': timestamp}
            db.execute('INSERT INTO tasks VALUES (?,?)', (task_id, json.dumps(task, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO research_search_runs VALUES (?,?,?,?,?)',
                       (run['id'], plan_id, saved['revision'], fingerprint, json.dumps(run, ensure_ascii=False)))
        background_tasks.add_task(run_search, task_id, run, settings)
        return {'task_id': task_id, 'run_id': run['id'], 'reused': False}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建计划检索任务。') from None


@router.get('/{plan_id}/search')
def get_search(plan_id: str, run_id: str | None = None):
    try:
        with connection() as db:
            initialize(db)
            plan = read_plan(db, plan_id)
            rows = db.execute('SELECT payload FROM research_search_runs WHERE plan_id=?', (plan_id,)).fetchall()
            runs = sorted((public_run(db, json.loads(row[0])) for row in rows), key=lambda r: r['created_at'], reverse=True)
            result = next((run for run in runs if run['id'] == run_id), None) if run_id else next((run for run in runs if run['revision'] == plan['revision']), None)
            if run_id and result is None:
                raise HTTPException(404, detail='该计划下不存在此检索记录。')
            return {'run': result, 'history': [{key: run[key] for key in ('id', 'revision', 'status', 'created_at', 'per_query', 'model_name')} for run in runs]}
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取检索结果。') from None
