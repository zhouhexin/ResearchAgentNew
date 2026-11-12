"""后台翻译与原文绑定缓存，不写回 Paper.abstract，不访问论文数据库。"""
import hashlib
import json
import logging
import os
import sqlite3
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException
from openai import APIConnectionError, APIStatusError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.abstract_translation import VERSION, PROMPT, AbstractTranslationOutput, AbstractTranslationError, translate_abstract
from agent.codex_model import CodexExecutionError
from agent.llm_runtime import analysis_llm, model_name, model_endpoint, model_http_backend
from agent.model_diagnostics import model_connection_diagnostic, model_timeout_diagnostic
from api.analyses import model_settings
from api.tasks import connection, now, update_task

router = APIRouter(prefix='/api/v1/translations/abstract', tags=['摘要中文翻译'])
logger = logging.getLogger(__name__)


class TranslationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str = Field(default='', max_length=2000)
    abstract: str = Field(min_length=1, max_length=24000)

    @model_validator(mode='after')
    def nonblank(self):
        if not self.abstract.strip():
            raise ValueError('没有原文摘要，无法翻译。')
        return self


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS abstract_translations (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')


def translation_key(body):
    return 'abstract_' + hashlib.sha256(json.dumps([VERSION, PROMPT, AbstractTranslationOutput.model_json_schema(),
        body.title, body.abstract, model_name(), model_endpoint(), os.getenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')],
        ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_record(db, identifier):
    row = db.execute('SELECT payload FROM abstract_translations WHERE id=?', (identifier,)).fetchone()
    if row is None:
        return None
    record = json.loads(row[0])
    task_row = db.execute('SELECT payload FROM tasks WHERE id=?', (record['task_id'],)).fetchone()
    return {'record': record, 'task': json.loads(task_row[0]) if task_row else None}


def complete_translation(identifier, task_id, text):
    with connection() as db:
        initialize(db);db.execute('BEGIN IMMEDIATE')
        value = read_record(db, identifier)
        if not value or value['record']['task_id'] != task_id or not value['task'] or value['task']['status'] not in {'queued', 'running'}:
            raise RuntimeError('翻译任务已结束或被替代')
        record, task = value['record'], value['task']
        record.update(translation_zh=text, completed_at=now())
        task.update(status='succeeded', result_id=identifier, progress=100, stage='摘要中文翻译已保存', error=None, updated_at=now())
        db.execute('UPDATE abstract_translations SET payload=? WHERE id=?', (json.dumps(record, ensure_ascii=False), identifier))
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))


def run_translation(identifier, task_id, settings):
    try:
        update_task(task_id, status='running', progress=10, stage='正在翻译摘要，原文保留')
        with connection() as db:
            value = read_record(db, identifier)
        if not value or value['record']['task_id'] != task_id:
            raise RuntimeError('翻译任务已被替代')
        record = value['record']
        with analysis_llm(read_timeout=settings['timeout'], retries=settings['retries'], trust_env=True, max_output_tokens=8000) as llm:
            text = translate_abstract(llm, record['title'], record['original_abstract'], method=settings['method'])
        complete_translation(identifier, task_id, text)
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
        elif isinstance(exc, AbstractTranslationError):
            code, message = 'ABSTRACT_TRANSLATION_INVALID', str(exc)
        elif isinstance(exc, (sqlite3.Error, OSError)):
            code, message = 'TRANSLATION_STORAGE_ERROR', '无法保存译文，请检查服务端存储。'
        else:
            code, message = 'ABSTRACT_TRANSLATION_FAILED', '摘要中文翻译失败，可重试，原文摘要已保留。'
        logger.warning('摘要翻译失败：task=%s code=%s error_type=%s', task_id, code, type(exc).__name__)
        try:
            update_task(task_id, status='failed', stage='中文翻译失败，原文仍可阅读', error={
                'code': code, 'message': message, 'retryable': retryable, **({'diagnostic': diagnostic} if diagnostic else {})})
        except (sqlite3.Error, OSError):
            logger.error('无法保存翻译失败状态：task=%s', task_id)


@router.post('/lookup')
def lookup(body: TranslationRequest):
    try:
        with connection() as db:
            initialize(db)
            return read_record(db, translation_key(body))
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取中文翻译缓存。') from None


@router.get('/{identifier}')
def get_translation(identifier: str):
    try:
        with connection() as db:
            initialize(db);value = read_record(db, identifier)
        if value is None:
            raise HTTPException(404, detail='摘要翻译记录不存在。')
        return value
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取摘要翻译记录。') from None


@router.post('', status_code=202)
def start(body: TranslationRequest, background_tasks: BackgroundTasks):
    try:
        identifier = translation_key(body)
        with connection() as db:
            initialize(db);db.execute('BEGIN IMMEDIATE')
            existing = read_record(db, identifier)
            if existing and (existing['record'].get('translation_zh') or (existing['task'] and existing['task']['status'] in {'queued', 'running'})):
                return {'task_id': existing['record']['task_id'], 'translation_id': identifier, 'reused': True}
            settings = model_settings()
            task_id, timestamp = uuid4().hex, now()
            record = {'id': identifier, 'title': body.title, 'original_abstract': body.abstract,
                      'source_sha256': hashlib.sha256(body.abstract.encode()).hexdigest(), 'translation_zh': None,
                      'model_name': model_name(), 'task_id': task_id, 'created_at': timestamp, 'completed_at': None}
            task = {'id': task_id, 'type': 'abstract_translation', 'paper_id': None, 'translation_id': identifier,
                    'paper_title': body.title, 'model_name': model_name(), 'http_backend': model_http_backend(), 'read_timeout': settings['timeout'],
                    'status': 'queued', 'stage': '等待生成摘要中文翻译', 'progress': 0, 'result_id': None,
                    'error': None, 'created_at': timestamp, 'updated_at': timestamp}
            db.execute('INSERT OR REPLACE INTO abstract_translations VALUES (?,?)', (identifier, json.dumps(record, ensure_ascii=False)))
            db.execute('INSERT INTO tasks VALUES (?,?)', (task_id, json.dumps(task, ensure_ascii=False)))
        background_tasks.add_task(run_translation, identifier, task_id, settings)
        return {'task_id': task_id, 'translation_id': identifier, 'reused': False}
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法创建摘要翻译任务。') from None
