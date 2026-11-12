"""综述、比较矩阵及下载内容与任务完成状态原子保存。"""
import json
from uuid import uuid4

from api.analysis_results import load_result as load_analysis, with_current_state
from api.tasks import connection, now


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reviews (
        id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
        payload TEXT NOT NULL, markdown TEXT NOT NULL, csv TEXT NOT NULL)''')


def complete_review(task_id, payload, markdown, csv):
    result = dict(payload, id='review_' + uuid4().hex, created_at=now())
    with connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('任务不存在')
        task = json.loads(row[0])
        if task['status'] not in {'queued', 'running'}:
            raise ValueError('任务已结束，不能再发布综述')
        db.execute('INSERT INTO reviews VALUES (?,?,?,?,?)', (result['id'], result['created_at'], json.dumps(result, ensure_ascii=False), markdown, csv))
        task.update(status='succeeded', progress=100, stage='比较矩阵和综述已保存，引用核验通过', result_id=result['id'], error=None, updated_at=now())
        db.execute('UPDATE tasks SET payload=? WHERE id=?', (json.dumps(task, ensure_ascii=False), task_id))
    return result


def load_review(review_id):
    with connection() as db:
        initialize(db)
        row = db.execute('SELECT payload,markdown,csv FROM reviews WHERE id=?', (review_id,)).fetchone()
    return (json.loads(row[0]), row[1], row[2]) if row else None


def with_review_state(payload, session):
    snapshots = [load_analysis(key) for key in payload['analysis_ids']]
    current = all(snapshot is not None for snapshot in snapshots)
    if current:
        verified = with_current_state(snapshots, session)
        current = all(snapshot['is_current'] and snapshot['paper']['id'] == paper_id
                      for snapshot, paper_id in zip(verified, payload['paper_ids']))
    return dict(payload, is_current=bool(current))
