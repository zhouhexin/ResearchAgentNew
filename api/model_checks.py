"""显式发起模型检查，持久化状态；不依赖论文 PostgreSQL。"""
import hashlib
import json
import logging
import os
import sqlite3
from time import monotonic
from typing import Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

from agent.analysis_validator import EvidenceValidationError
from agent.llm_runtime import analysis_llm, model_http_backend, model_request_options, model_name, model_endpoint, model_provider
from agent.codex_model import CodexExecutionError
from agent.model_check import check_structured_analysis
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from agent.paper_analyzer import StructuredAnalysisError
from api.analyses import model_settings
from api.tasks import connection, create_task, update_task

router = APIRouter(prefix='/api/v1/system', tags=['模型诊断'])
logger = logging.getLogger(__name__)
CheckMode = Literal['chat', 'structured']
HttpBackend = Literal['sdk', 'requests', 'codex']


class ModelCheckRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    mode: CheckMode = 'structured'
    http_backend: HttpBackend | None = None
    timeout: float = Field(default=60, ge=1, le=120, allow_inf_nan=False)


def configuration_fingerprint(backend):
    options = model_request_options()
    # 密钥仅参与不可逆摘要，原值不进入任务记录、日志或浏览器。
    values = ([model_provider(), model_name(), model_endpoint(), os.getenv('CODEX_CLI_PATH', ''), os.getenv('CODEX_USE_SYSTEM_PROXY', 'true')]
              if model_provider() == 'codex' else
              [os.getenv(name, '') for name in ('LLM_MODEL', 'LLM_BASE_URL', 'LLM_API_KEY', 'STRUCTURED_OUTPUT_METHOD')])
    return hashlib.sha256(json.dumps([values, backend, options], sort_keys=True).encode()).hexdigest()


@router.get('/model-check')
def latest_model_check(mode: CheckMode = 'structured', http_backend: HttpBackend | None = None):
    try:
        fingerprint = configuration_fingerprint(model_http_backend(http_backend))
        with connection() as db:
            candidates = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
        candidates = [task for task in candidates if task.get('type') == 'model_diagnostic'
                      and task.get('check_mode') == mode and task.get('configuration_fingerprint') == fingerprint]
        return max(candidates, key=lambda task: task['created_at']) if candidates else None
    except ValueError:
        return None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取模型检测记录。') from None


def run_model_check(task_id, *, mode, backend, timeout, method):
    started = monotonic()
    try:
        update_task(task_id, status='running', progress=10,
                    stage='正在检查结构化分析与原文引用' if mode == 'structured' else '正在发送普通对话请求')
        with analysis_llm(read_timeout=timeout, retries=0, trust_env=True, http_backend=backend) as llm:
            details = {}
            if mode == 'structured':
                details = check_structured_analysis(llm, method)
            else:
                response = llm.invoke([('human', '请只回复 OK。')])
                if not response.content:
                    raise ValueError('模型返回空响应')
        update_task(task_id, status='succeeded', progress=100, stage='样例结构和原文引用核验通过' if mode == 'structured' else '普通对话请求成功',
                    model_check_result={'response_received': True, 'structure_validated': mode == 'structured',
                                        'evidence_validated': mode == 'structured', 'seconds': round(monotonic() - started, 2), **details})
    except Exception as exc:
        diagnostic = None
        if isinstance(exc, CodexExecutionError):
            code, message, diagnostic = exc.code, str(exc), exc.diagnostic
        elif isinstance(exc, APITimeoutError):
            diagnostic = model_timeout_diagnostic(exc, timeout)
            code, message = 'MODEL_TIMEOUT', diagnostic['message']
        elif isinstance(exc, APIConnectionError):
            diagnostic = model_connection_diagnostic(exc)
            code, message = 'MODEL_CONNECTION_ERROR', diagnostic['message']
        elif isinstance(exc, APIStatusError):
            code, message = 'MODEL_HTTP_ERROR', f'模型服务返回 HTTP {exc.status_code}，请检查权限、额度与服务状态。'
            diagnostic = {'reason': 'http', 'http_status': exc.status_code}
        elif isinstance(exc, EvidenceValidationError):
            code, message = 'EVIDENCE_VALIDATION_FAILED', '模型返回的样例引文未通过原文核验。'
            diagnostic = {'reason': 'evidence', 'issue_count': len(exc.diagnostics.get('issues', []))}
        elif isinstance(exc, StructuredAnalysisError):
            code, message = 'STRUCTURED_ANALYSIS_FAILED', '模型未返回符合要求的样例分析结构。'
            diagnostic = {'reason': 'structure', 'finish_reason': exc.diagnostics.get('finish_reason')}
        else:
            code, message = 'MODEL_CHECK_FAILED', '模型检测未完成，请检查模型配置与服务日志。'
        logger.warning('模型检测失败：task=%s code=%s error_type=%s diagnostic=%s', task_id, code, type(exc).__name__, diagnostic)
        try:
            update_task(task_id, status='failed', stage='模型检测失败',
                        model_check_result={'seconds': round(monotonic() - started, 2)},
                        error={'code': code, 'message': message, 'retryable': exc.retryable if isinstance(exc, CodexExecutionError) else True,
                               **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存模型检测状态：task=%s', task_id)


@router.post('/model-check', status_code=202)
def start_model_check(body: ModelCheckRequest, background_tasks: BackgroundTasks):
    try:
        settings = model_settings()
        backend = model_http_backend(body.http_backend)
        fingerprint = configuration_fingerprint(backend)
        task = create_task(task_type='model_diagnostic', stage='等待模型检测', deduplicate=True,
                           dedup_key=f'{fingerprint}:{body.mode}:{body.timeout:g}', metadata={
                               'model_name': model_name(), 'http_backend': backend,
                               'check_mode': body.mode, 'read_timeout': body.timeout,
                               'structured_output_method': settings['method'], 'configuration_fingerprint': fingerprint})
        if not task.get('_reused'):
            background_tasks.add_task(run_model_check, task['id'], mode=body.mode, backend=backend,
                                      timeout=body.timeout, method=settings['method'])
        return {'task_id': task['id'], 'reused': bool(task.get('_reused'))}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建模型检测任务。') from None
