"""长期离线实验产物存PG，租约/预算/排程检查点沿用SQLite；不会随API启动调度。"""
import json
import time
from contextlib import contextmanager
from pathlib import Path
import sqlite3
from uuid import uuid4
from agent.runtime_snapshot import fingerprint, RuntimeUnavailable


class ExperimentStopped(RuntimeUnavailable):
    def __init__(self,code):
        super().__init__(code)
        self.code=code


def put(kind,content):
    from api import coverage
    identifier='experiment_'+fingerprint([kind,content])[:48]
    coverage.store({'id':identifier,'kind':'research_experiment','artifact_kind':kind,'content':content})
    return identifier


def get(identifier,kind=None):
    from api import coverage
    value=coverage.read(identifier)
    if value.get('kind')!='research_experiment' or kind and value['artifact_kind']!=kind:raise RuntimeUnavailable('实验产物类型不匹配。')
    if value['id']!='experiment_'+fingerprint([value['artifact_kind'],value['content']])[:48]:raise RuntimeUnavailable('实验产物指纹不匹配。')
    return value['content']


class Checkpoints:
    def __init__(self,path):self.path=Path(path)
    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db=sqlite3.connect(self.path,timeout=10)
        try:
            db.execute('CREATE TABLE IF NOT EXISTS experiment_jobs(id TEXT PRIMARY KEY,payload TEXT NOT NULL,lease TEXT,lease_until REAL NOT NULL DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS experiment_items(job_id TEXT,key TEXT,payload TEXT,PRIMARY KEY(job_id,key))')
            with db:yield db
        finally:db.close()

    def create(self,key,config):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT payload FROM experiment_jobs WHERE id=?',(key,)).fetchone()
            if row:
                value=json.loads(row[0])
                if value['config']!=config:raise RuntimeUnavailable('恢复配置不符，不能混用检查点。')
                return value
            value={'config':config,'state':'queued','calls_reserved':0,'deadline':None,'stop_reason':None}
            db.execute('INSERT INTO experiment_jobs VALUES (?,?,NULL,0)',(key,json.dumps(value)))
            return value

    def acquire(self,key,lease_seconds):
        token=uuid4().hex
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT lease_until FROM experiment_jobs WHERE id=?',(key,)).fetchone()
            if not row or row[0]>time.time():return None
            db.execute('UPDATE experiment_jobs SET lease=?,lease_until=? WHERE id=?',(token,time.time()+lease_seconds,key))
        return token

    def update(self,key,token,**changes):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            value=self._owned(db,key,token);value.update(changes)
            db.execute('UPDATE experiment_jobs SET payload=? WHERE id=?',(json.dumps(value),key))
            return value

    def _owned(self,db,key,token):
        row=db.execute('SELECT payload,lease,lease_until FROM experiment_jobs WHERE id=?',(key,)).fetchone()
        if not row or row[1]!=token or row[2]<=time.time():raise RuntimeUnavailable('实验执行租约已丢失。')
        return json.loads(row[0])

    def reserve(self,key,token,reservation=None):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');value=self._owned(db,key,token)
            if value['deadline'] is None:value['deadline']=time.time()+value['config']['total_seconds']
            if time.time()>=value['deadline']:raise ExperimentStopped('EXPERIMENT_DEADLINE_EXCEEDED')
            if value['calls_reserved']>=value['config']['max_calls']:raise ExperimentStopped('EXPERIMENT_BUDGET_EXHAUSTED')
            if reservation is not None:
                item_id,payload=reservation
                if db.execute('SELECT 1 FROM experiment_items WHERE job_id=? AND key=?',(key,item_id)).fetchone():
                    raise RuntimeUnavailable('实验尝试已经登记，不能重复预扣或调用。')
                db.execute('INSERT INTO experiment_items VALUES (?,?,?)',(key,item_id,json.dumps(payload)))
            value['calls_reserved']+=1
            db.execute('UPDATE experiment_jobs SET payload=? WHERE id=?',(json.dumps(value),key))
            return value

    def check_deadline(self,key,token,outer_deadline=None):
        with self.db() as db:
            value=self._owned(db,key,token)
            deadline=value['deadline']
            if deadline is None:raise RuntimeUnavailable('尚未固定执行期限。')
            if outer_deadline is not None:deadline=min(deadline,outer_deadline)
            if time.time()>=deadline:raise ExperimentStopped('EXPERIMENT_DEADLINE_EXCEEDED')
            return deadline

    def save_item(self,key,token,item_id,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,token)
            old=db.execute('SELECT payload FROM experiment_items WHERE job_id=? AND key=?',(key,item_id)).fetchone()
            if old and json.loads(old[0])!=payload:raise RuntimeUnavailable('实验样本历史冲突。')
            db.execute('INSERT OR IGNORE INTO experiment_items VALUES (?,?,?)',(key,item_id,json.dumps(payload)))

    def mark_started(self,key,token,item_id,runtime_snapshot_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,token)
            row=db.execute('SELECT payload FROM experiment_items WHERE job_id=? AND key=?',(key,item_id)).fetchone()
            if not row:raise RuntimeUnavailable('调用尝试未预先登记。')
            value=json.loads(row[0])
            if not value.get('pending') or value.get('invoke_started'):
                raise RuntimeUnavailable('该尝试已经开始或结束，不能重复发出请求。')
            value.update(runtime_snapshot_id=runtime_snapshot_id,invoke_started=True)
            db.execute('UPDATE experiment_items SET payload=? WHERE job_id=? AND key=?',(json.dumps(value),key,item_id))

    def finish_item(self,key,token,item_id,payload):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE');self._owned(db,key,token)
            row=db.execute('SELECT payload FROM experiment_items WHERE job_id=? AND key=?',(key,item_id)).fetchone()
            if not row:raise RuntimeUnavailable('调用前没有持久登记该尝试。')
            old=json.loads(row[0])
            if not old.get('pending'):
                if old!=payload:raise RuntimeUnavailable('已完成尝试不可覆盖。')
                return
            for field in ('case_id','repetition','input_hash'):
                if old['outcome'][field]!=payload['outcome'][field]:raise RuntimeUnavailable('尝试输入身份变化。')
            if old['attempt']!=payload['attempt']:raise RuntimeUnavailable('尝试编号变化。')
            db.execute('UPDATE experiment_items SET payload=? WHERE job_id=? AND key=?',(json.dumps(payload),key,item_id))

    def items(self,key):
        with self.db() as db:return {r[0]:json.loads(r[1]) for r in db.execute('SELECT key,payload FROM experiment_items WHERE job_id=?',(key,))}

    def release(self,key,token):
        with self.db() as db:db.execute('UPDATE experiment_jobs SET lease=NULL,lease_until=0 WHERE id=? AND lease=?',(key,token))
