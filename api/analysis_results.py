"""保存通过核验的分析历史；与任务状态在同一 SQLite 事务提交。"""
import json
import re
from uuid import uuid4

from sqlalchemy import select

from api.tasks import connection, now
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS analyses (
        id TEXT PRIMARY KEY, paper_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, payload TEXT NOT NULL)''')
    db.execute('CREATE INDEX IF NOT EXISTS analyses_paper ON analyses(paper_id, created_at)')


def load_results(paper_id=None):
    with connection() as db:
        initialize(db)
        if paper_id is None:
            rows = db.execute('SELECT payload FROM analyses ORDER BY created_at DESC, id DESC').fetchall()
        else:
            rows = db.execute('SELECT payload FROM analyses WHERE paper_id=? ORDER BY created_at DESC, id DESC', (paper_id,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def load_result(analysis_id):
    with connection() as db:
        initialize(db)
        row = db.execute('SELECT payload FROM analyses WHERE id=?', (analysis_id,)).fetchone()
    return json.loads(row[0]) if row else None


def complete_analysis(task_id, payload):
    payload = dict(payload, id='analysis_' + uuid4().hex, created_at=now())
    with connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        task = json.loads(row[0])
        if task['status'] not in {'queued', 'running'}:
            raise ValueError('任务已结束，不能再发布分析结果')
        db.execute('INSERT INTO analyses VALUES (?,?,?,?)',
                   (payload['id'], payload['paper']['id'], payload['created_at'], json.dumps(payload, ensure_ascii=False)))
        task.update(status='succeeded', progress=100, stage='分析和原文证据核验已完成', result_id=payload['id'], error=None, updated_at=now())
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))
    return payload


def with_current_state(payloads, session):
    """原文哈希和所读片段编号均未改变时，报告才算当前可用。"""
    if not payloads:
        return []
    document_ids = {p['document']['id'] for p in payloads}
    documents = {d.id: d for d in session.scalars(select(PaperDocumentRecord).where(PaperDocumentRecord.id.in_(document_ids)))}
    evidence_ids = {int(e[1:]) for p in payloads for e in p.get('selected_evidence_ids', p['evidence_index']) if re.fullmatch(r'E[1-9][0-9]*', e)}
    chunks = dict(session.execute(select(DocumentChunkRecord.id, DocumentChunkRecord.document_id).where(DocumentChunkRecord.id.in_(evidence_ids))).all()) if evidence_ids else {}
    results = []
    for payload in payloads:
        document = documents.get(payload['document']['id'])
        selected = payload.get('selected_evidence_ids', list(payload['evidence_index']))
        current = bool(document and document.paper_id == payload['paper']['id']
                       and document.content_sha256 == payload['document']['content_sha256']
                       and all(re.fullmatch(r'E[1-9][0-9]*', e) and chunks.get(int(e[1:])) == document.id for e in selected))
        results.append(dict(payload, is_current=current))
    return results


def current_analysis_paper_ids(session, paper_id=None):
    return {p['paper']['id'] for p in with_current_state(load_results(paper_id), session) if p['is_current']}
