"""任务检查点预算，与既有任务/批次共用 SQLite；调用预扣，失败与恢复均不归零。"""
import json
import time
from api import tasks


class ReadingStopped(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS reread_budgets (
        id TEXT PRIMARY KEY, deadline REAL NOT NULL, calls_used INTEGER NOT NULL,
        active_task_id TEXT, max_calls INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS reread_calls (
        request_id TEXT NOT NULL, call_index INTEGER NOT NULL, payload TEXT NOT NULL,
        PRIMARY KEY(request_id,call_index))''')


def acquire(record, task_id):
    with tasks.connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        own = db.execute('SELECT payload FROM tasks WHERE id=?', (task_id,)).fetchone()
        if not own or json.loads(own[0])['status'] not in {'queued', 'running'}:
            return False
        db.execute('INSERT OR IGNORE INTO reread_budgets VALUES (?,?,0,NULL,?)',
                   (record['id'], time.time()+record['budget']['total_seconds'], record['budget']['max_calls']))
        active = db.execute('SELECT active_task_id FROM reread_budgets WHERE id=?', (record['id'],)).fetchone()[0]
        if active:
            row = db.execute('SELECT payload FROM tasks WHERE id=?', (active,)).fetchone()
            if row and json.loads(row[0])['status'] in {'queued', 'running'}:
                return False
        db.execute('UPDATE reread_budgets SET active_task_id=? WHERE id=?', (task_id, record['id']))
    return True


def release(request_id, task_id):
    with tasks.connection() as db:
        initialize(db)
        db.execute('UPDATE reread_budgets SET active_task_id=NULL WHERE id=? AND active_task_id=?', (request_id, task_id))


def reserve(request_id, task_id):
    with tasks.connection() as db:
        initialize(db)
        db.execute('BEGIN IMMEDIATE')
        deadline, used, active, maximum = db.execute('SELECT deadline,calls_used,active_task_id,max_calls FROM reread_budgets WHERE id=?', (request_id,)).fetchone()
        if active != task_id:
            raise ReadingStopped('REREAD_LEASE_LOST', '补读任务执行权已变化，请刷新任务状态。')
        remaining = deadline - time.time()
        if remaining <= 0:
            raise ReadingStopped('REREAD_DEADLINE_EXCEEDED', '补读执行期限已到，已核验批次保留；继续原任务不会重置期限。')
        if used >= maximum:
            raise ReadingStopped('REREAD_BUDGET_EXHAUSTED', '补读模型调用预算已用完，失败与引用修复调用也计入预算。')
        index = used+1
        db.execute('UPDATE reread_budgets SET calls_used=? WHERE id=?', (index, request_id))
        db.execute('INSERT INTO reread_calls VALUES (?,?,?)', (request_id, index, json.dumps({'status': 'reserved', 'reserved_at': tasks.now()})))
    return index, remaining


def finish(request_id, index, **payload):
    with tasks.connection() as db:
        initialize(db)
        row = db.execute('SELECT payload FROM reread_calls WHERE request_id=? AND call_index=?', (request_id,index)).fetchone()
        value = json.loads(row[0]) | payload
        db.execute('UPDATE reread_calls SET payload=? WHERE request_id=? AND call_index=?', (json.dumps(value),request_id,index))


def summary(request_id):
    with tasks.connection() as db:
        initialize(db)
        row = db.execute('SELECT deadline,calls_used,max_calls FROM reread_budgets WHERE id=?', (request_id,)).fetchone()
        calls = [dict(call_index=r[0], **json.loads(r[1])) for r in db.execute('SELECT call_index,payload FROM reread_calls WHERE request_id=? ORDER BY call_index', (request_id,))]
    return {'deadline_epoch': row[0], 'calls_used': row[1], 'max_calls': row[2], 'calls': calls,
            'tokens': None, 'cost': None} if row else None
