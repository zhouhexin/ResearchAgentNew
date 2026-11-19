"""研究计划草稿、版本保存与后台规划任务；不访问论文数据库或执行文献工具。"""
from datetime import date
import hashlib
import json
import logging
import os
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

from agent.codex_model import CodexExecutionError
from agent.llm_runtime import analysis_llm, model_name, model_http_backend, model_endpoint
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from agent.research_planner import (PLAN_VERSION, PLANNING_PROMPT, ResearchPlan, ResearchPlanningError,
                                    build_planning_graph, planning_capabilities, validate_research_plan)
from api.analyses import model_settings
from api.tasks import connection, create_task, now, update_task
from api import tasks, runtime_bindings, provenance
from agent.runtime_snapshot import RuntimeUnavailable

router = APIRouter(prefix='/api/v1/agent/plans', tags=['研究目标与计划'])
logger = logging.getLogger(__name__)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    goal: str = Field(min_length=2, max_length=6000)
    resume_task_id: str | None = None


class PlanUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    expected_revision: int = Field(ge=1)
    plan: ResearchPlan


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS research_plans (
        id TEXT PRIMARY KEY, payload TEXT NOT NULL)''')
    # 只保存修订定位编号；正文仍在 PostgreSQL，与当前计划同一 SQLite 事务提交定位。
    db.execute('''CREATE TABLE IF NOT EXISTS research_plan_revision_links (
        plan_id TEXT NOT NULL, revision INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
        PRIMARY KEY(plan_id, revision))''')


def link_revision(db, plan):
    if plan.get('plan_snapshot_id'):
        db.execute('INSERT OR IGNORE INTO research_plan_revision_links VALUES (?,?,?)',
                   (plan['id'], plan['revision'], plan['plan_snapshot_id']))
        existing = db.execute('SELECT snapshot_id FROM research_plan_revision_links WHERE plan_id=? AND revision=?',
                              (plan['id'], plan['revision'])).fetchone()
        if existing[0] != plan['plan_snapshot_id']:
            raise RuntimeUnavailable('计划修订已经提交，不能改写历史定位。')


def complete_plan(task_id, goal, plan, today):
    result = {'id': 'plan_' + uuid4().hex, 'goal': goal, 'plan': plan.model_dump(mode='json'),
              'revision': 1, 'status': 'draft', 'created_at': now(), 'updated_at': now(),
              'planning_date': today.isoformat(), 'model_name': tasks.get_task(task_id).get('model_name'), 'source_task_id': task_id,
              'capabilities': planning_capabilities()}
    with connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('规划任务不存在')
        task = json.loads(row[0])
        result.update(provenance.fields(task))
        result['model_name'] = task.get('model_name', result['model_name'])
        if result.get('interaction_id'):
            result['plan_snapshot_id'] = provenance.freeze_plan(result)
            link_revision(db, result)
        if task['status'] not in {'queued', 'running'}:
            raise ValueError('规划任务已结束，不能再发布结果')
        db.execute('INSERT INTO research_plans VALUES (?,?)', (result['id'], json.dumps(result, ensure_ascii=False)))
        task.update(status='succeeded', progress=100, stage='研究目标与执行计划已生成，尚未执行',
                    result_id=result['id'], error=None, updated_at=now())
        if result.get('interaction_id'):
            task.update(plan_id=result['id'], plan_revision=1, plan_snapshot_id=result['plan_snapshot_id'],
                        research_questions=plan.research_questions, research_scope=plan.scope.model_dump(mode='json'))
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))
    return result


@runtime_bindings.task_worker
def run_planning(task_id, goal, settings, today):
    try:
        update_task(task_id, status='running', progress=5, stage='正在准备研究目标理解')
        binding = runtime_bindings.for_task(task_id, 'planning')
        if binding:
            settings = binding.settings
        with (binding.llm() if binding else analysis_llm(read_timeout=settings['timeout'], retries=settings['retries'], trust_env=True,
                          max_output_tokens=settings['output_tokens'])) as llm:
            graph = build_planning_graph(llm, method=settings['method'], today=today,
                                         progress=lambda **event: update_task(task_id, **event))
            result = graph.invoke({'goal': goal}, config={'recursion_limit': 5})
        complete_plan(task_id, goal, result['plan'], today)
    except Exception as exc:
        diagnostic, retryable = None, True
        if isinstance(exc, RuntimeUnavailable):
            code, message, retryable = exc.code, str(exc), False
        elif isinstance(exc, CodexExecutionError):
            code, message, diagnostic, retryable = exc.code, str(exc), exc.diagnostic, exc.retryable
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, settings['timeout'])
            code, message = 'MODEL_TIMEOUT', diagnostic['message']
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度或模型配置。'
        elif isinstance(exc, ResearchPlanningError):
            code = 'PLAN_STRUCTURE_FAILED' if exc.kind == 'structure' else 'PLAN_VALIDATION_FAILED'
            message, diagnostic = str(exc), exc.diagnostics
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'PLAN_STORAGE_ERROR', '无法保存研究计划，请检查服务端存储。'
        else:
            code, message = 'RESEARCH_PLANNING_FAILED', '研究计划生成失败，请检查模型配置后重试。'
        logger.warning('研究计划失败：task=%s code=%s error_type=%s diagnostic=%s', task_id, code, type(exc).__name__, diagnostic)
        try:
            update_task(task_id, status='failed', stage='研究计划生成失败，未执行文献研究', error={
                'code': code, 'message': message, 'retryable': retryable,
                **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存研究计划失败状态：task=%s', task_id)


@router.post('', status_code=202)
def start_planning(body: PlanRequest, background_tasks: BackgroundTasks):
    goal = body.goal.strip()
    if len(goal) < 2:
        raise HTTPException(422, detail='请描述希望研究的主题与目标。')
    try:
        previous = tasks.get_task(body.resume_task_id) if body.resume_task_id else None
        if previous and (previous['type'] != 'research_planning' or previous.get('research_goal') != goal):
            raise HTTPException(422, detail='只能继续同一研究目标的规划任务。')
        if previous and not previous.get('runtime_bindings') and runtime_bindings.enabled():
            raise RuntimeUnavailable('历史规划任务没有运行快照，请明确新建规划任务。')
        bundle = runtime_bindings.bundle_for(previous)
        binding = runtime_bindings.restore(bundle, 'planning') if bundle else None
        settings, today = dict(binding.settings) if binding else model_settings(), date.today()
        if previous and previous.get('planning_date'):
            today = date.fromisoformat(previous['planning_date'])
        try:
            if not binding:
                settings['output_tokens'] = int(os.getenv('RESEARCH_PLAN_MAX_OUTPUT_TOKENS', '6000'))
        except ValueError:
            raise ValueError('RESEARCH_PLAN_MAX_OUTPUT_TOKENS 必须为正整数') from None
        if settings['output_tokens'] < 1:
            raise ValueError('RESEARCH_PLAN_MAX_OUTPUT_TOKENS 必须为正整数')
        key = hashlib.sha256(json.dumps([goal, today.isoformat(), binding.metadata if binding else [model_name(), model_endpoint()], settings['method'],
                                        PLAN_VERSION, PLANNING_PROMPT, ResearchPlan.model_json_schema(), settings['output_tokens']], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        task = create_task(task_type='research_planning', stage='等待理解研究目标并制定计划', deduplicate=True, dedup_key=key,
                           metadata={**(provenance.fields(previous) if previous else {}),
                                     'research_goal': goal, 'planning_date': today.isoformat(),
                                     'model_name': binding.runtime.model if binding else model_name(),
                                     'http_backend': binding.runtime.backend if binding else model_http_backend(),
                                     'read_timeout': settings['timeout'], **runtime_bindings.metadata(bundle, 'planning'),
                                     **({'resumed_from_task_id': previous['id']} if previous else {})})
        if not task.get('_reused'):
            background_tasks.add_task(run_planning, task['id'], goal, settings, today)
        return {'task_id': task['id'], 'reused': bool(task.get('_reused'))}
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建研究计划任务。') from None


@router.get('/{plan_id}')
def get_plan(plan_id: str):
    try:
        with connection() as db:
            initialize(db)
            row = db.execute('SELECT payload FROM research_plans WHERE id=?', (plan_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail='研究计划不存在。')
        result = json.loads(row[0])
        result['capabilities'] = planning_capabilities()
        return result
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取研究计划。') from None


@router.put('/{plan_id}')
def save_plan(plan_id: str, body: PlanUpdate):
    try:
        with connection() as db:
            initialize(db)
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM research_plans WHERE id=?', (plan_id,)).fetchone()
            if row is None:
                raise HTTPException(404, detail='研究计划不存在。')
            result = json.loads(row[0])
            if result['revision'] != body.expected_revision:
                raise HTTPException(409, detail='计划已在其他页面修改，请重新读取后再保存。')
            plan = validate_research_plan(body.plan, result['goal'], today=date.fromisoformat(result['planning_date']))
            if result.get('interaction_id'):
                link_revision(db, result)
            result.update(plan=plan.model_dump(mode='json'), revision=result['revision'] + 1,
                          updated_at=now(), edited_by_user=True, capabilities=planning_capabilities())
            if result.get('interaction_id'):
                result['plan_snapshot_id'] = provenance.freeze_plan(result)
                link_revision(db, result)
            db.execute('UPDATE research_plans SET payload=? WHERE id=?', (json.dumps(result, ensure_ascii=False), plan_id))
        return result
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
    except ResearchPlanningError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法保存研究计划。') from None


@router.get('/{plan_id}/revisions/{revision}')
def get_plan_revision(plan_id: str, revision: int):
    get_plan(plan_id)
    try:
        with connection() as db:
            initialize(db)
            row = db.execute('SELECT snapshot_id FROM research_plan_revision_links WHERE plan_id=? AND revision=?',
                             (plan_id, revision)).fetchone()
        if not row:
            raise RuntimeUnavailable('这项历史修订未记录版本快照，不能用当前计划回填。')
        return provenance.load_plan_revision(row[0])
    except RuntimeUnavailable as exc:
        raise runtime_bindings.unavailable(exc) from None
