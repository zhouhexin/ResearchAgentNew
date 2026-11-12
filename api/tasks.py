"""本地工作台任务记录。任务状态保存在 SQLite，正文仍保存在 PostgreSQL。"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query

TASK_DB_PATH = Path(__file__).resolve().parents[1] / 'outputs' / 'api_tasks.sqlite3'
router = APIRouter(prefix='/api/v1/tasks', tags=['任务'])


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection():
    TASK_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(TASK_DB_PATH, timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        with db:
            yield db
    finally:
        db.close()


def create_task(paper_id=None, *, task_type='document_parse', stage='PDF 已上传，等待解析', deduplicate=False, dedup_key=None, metadata=None):
    task = {
        'id': uuid4().hex, 'type': task_type, 'paper_id': paper_id,
        'status': 'queued', 'progress': 0, 'stage': stage,
        'result_id': None, 'error': None, 'created_at': now(), 'updated_at': now(),
    }
    if metadata:
        task.update(metadata)
    if dedup_key is not None:
        task['dedup_key'] = dedup_key
    with connection() as db:
        if deduplicate:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute('SELECT payload FROM tasks'):
                existing = json.loads(row[0])
                if existing.get('paper_id') == paper_id and existing['type'] == task_type and existing['status'] in {'queued', 'running'} and (dedup_key is None or existing.get('dedup_key') == dedup_key):
                    return dict(existing, _reused=True)
        db.execute('INSERT INTO tasks VALUES (?, ?)', (task['id'], json.dumps(task, ensure_ascii=False)))
    return task


def update_task(task_id, **changes):
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        task = json.loads(row[0])
        task.update(changes, updated_at=now())
        db.execute('UPDATE tasks SET payload = ? WHERE id = ?', (json.dumps(task, ensure_ascii=False), task_id))
    return task


def recover_interrupted_tasks():
    # 当前为单进程本地服务；重启后明确报告中断，不伪装成仍在运行。
    with connection() as db:
        db.execute('BEGIN IMMEDIATE')
        for task_id, payload in db.execute('SELECT id, payload FROM tasks').fetchall():
            task = json.loads(payload)
            if task['status'] in {'queued', 'running'}:
                task.update(status='failed', stage='服务重启，任务已中断', updated_at=now(), error={
                    'code': 'SERVICE_RESTARTED', 'message': '服务在处理期间重启，请重新发起该任务。', 'retryable': True,
                })
                db.execute('UPDATE tasks SET payload = ? WHERE id = ?', (json.dumps(task, ensure_ascii=False), task_id))


@router.get('')
def list_tasks(
    status: Literal['queued', 'running', 'succeeded', 'failed', 'cancelled'] | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    try:
        with connection() as db:
            tasks = [json.loads(row[0]) for row in db.execute('SELECT payload FROM tasks')]
        tasks = sorted((t for t in tasks if status is None or t['status'] == status),
                       key=lambda t: t['created_at'], reverse=True)
        start = (page - 1) * page_size
        return {'items': tasks[start:start + page_size], 'total': len(tasks), 'page': page, 'page_size': page_size}
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取任务记录。') from None


@router.get('/{task_id}')
def get_task(task_id: str):
    try:
        with connection() as db:
            row = db.execute('SELECT payload FROM tasks WHERE id = ?', (task_id,)).fetchone()
        if row is None:
            raise HTTPException(404, detail='任务不存在。')
        return json.loads(row[0])
    except (sqlite3.Error, OSError):
        raise HTTPException(503, detail='暂时无法读取任务记录。') from None
