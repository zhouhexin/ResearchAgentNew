"""分批分析的进度记录。只把通过核验的批次保存为 completed。"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


def now():
    return datetime.now(timezone.utc).isoformat()


class AnalysisBatchStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute('''CREATE TABLE IF NOT EXISTS analysis_batch_runs (
                run_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL, plan TEXT NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS analysis_batches (
                run_key TEXT NOT NULL, batch_index INTEGER NOT NULL, status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, payload TEXT, error_code TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(run_key,batch_index))''')
            db.execute('''CREATE TABLE IF NOT EXISTS analysis_batch_feedback (
                run_key TEXT NOT NULL,batch_index INTEGER NOT NULL,feedback TEXT NOT NULL,
                PRIMARY KEY(run_key,batch_index))''')
            with db:
                yield db
        finally:
            db.close()

    def latest_run(self, fingerprint):
        with self.connection() as db:
            row = db.execute('SELECT run_key FROM analysis_batch_runs WHERE fingerprint=? ORDER BY created_at DESC LIMIT 1', (fingerprint,)).fetchone()
        return row[0] if row else None

    def prepare(self, run_key, fingerprint, batches):
        plan = [{'index': i + 1, 'evidence_ids': list(batch.sources), 'characters': sum(len(s.text) for s in batch.sources.values())} for i, batch in enumerate(batches)]
        with self.connection() as db:
            db.execute('INSERT OR IGNORE INTO analysis_batch_runs VALUES (?,?,?,?)', (run_key, fingerprint, now(), json.dumps(plan)))
            for i in range(len(batches)):
                db.execute('INSERT OR IGNORE INTO analysis_batches VALUES (?,?,?,0,NULL,NULL,?)', (run_key, i + 1, 'queued', now()))

    def get_completed(self, run_key, index):
        with self.connection() as db:
            row = db.execute('SELECT payload FROM analysis_batches WHERE run_key=? AND batch_index=? AND status=?', (run_key, index, 'completed')).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def begin_attempt(self, run_key, index):
        with self.connection() as db:
            db.execute('UPDATE analysis_batches SET status=?,attempts=attempts+1,error_code=NULL,updated_at=? WHERE run_key=? AND batch_index=?', ('running', now(), run_key, index))

    def save_completed(self, run_key, index, analysis):
        with self.connection() as db:
            db.execute('UPDATE analysis_batches SET status=?,payload=?,error_code=NULL,updated_at=? WHERE run_key=? AND batch_index=?', ('completed', analysis.model_dump_json(), now(), run_key, index))
            db.execute('DELETE FROM analysis_batch_feedback WHERE run_key=? AND batch_index=?',(run_key,index))

    def failed(self, run_key, index, code, *, feedback=None):
        with self.connection() as db:
            db.execute('UPDATE analysis_batches SET status=?,payload=NULL,error_code=?,updated_at=? WHERE run_key=? AND batch_index=?', ('failed', code, now(), run_key, index))
            if feedback is not None:
                db.execute('INSERT OR REPLACE INTO analysis_batch_feedback VALUES (?,?,?)',(run_key,index,feedback))

    def get_feedback(self,run_key,index):
        with self.connection() as db:
            row=db.execute('SELECT feedback FROM analysis_batch_feedback WHERE run_key=? AND batch_index=?',(run_key,index)).fetchone()
        return row[0] if row else None

    def steps(self, run_key):
        with self.connection() as db:
            rows = db.execute('SELECT batch_index,status,attempts,error_code,updated_at FROM analysis_batches WHERE run_key=? ORDER BY batch_index', (run_key,)).fetchall()
        return [dict(zip(('batch_index', 'status', 'attempts', 'error_code', 'updated_at'), row)) for row in rows]
