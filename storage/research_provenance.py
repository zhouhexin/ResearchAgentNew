"""新增长期交互与不可变计划修订；任务仍使用既有 SQLite。"""
from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint, safe_manifest
from storage.database import Base


class InteractionRecord(Base):
    __tablename__ = 'research_interactions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)


class PlanRevisionRecord(Base):
    __tablename__ = 'research_plan_revisions'
    id: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)


TABLES = [InteractionRecord.__table__, PlanRevisionRecord.__table__]


def save_once(session, record_type, identifier, payload):
    payload = safe_manifest(payload)
    insert = {'postgresql': pg_insert, 'sqlite': sqlite_insert}[session.get_bind().dialect.name]
    session.execute(insert(record_type).values(id=identifier, payload=payload).on_conflict_do_nothing(index_elements=['id']))
    existing = session.get(record_type, identifier, populate_existing=True)
    if existing.payload != payload:
        raise RuntimeUnavailable('历史溯源内容冲突，拒绝覆盖。')
    return identifier


def save_revision(session, plan):
    content = {key: value for key, value in plan.items() if key not in {'capabilities', 'plan_snapshot_id'}}
    payload = {'content': content, 'content_sha256': fingerprint(content)}
    identifier = f"{plan['id']}:{plan['revision']}:{payload['content_sha256']}"
    return save_once(session, PlanRevisionRecord, identifier, payload)


def read_revision(session, identifier):
    record = session.get(PlanRevisionRecord, identifier)
    if record is None:
        raise RuntimeUnavailable('原计划修订快照不存在。')
    if fingerprint(record.payload['content']) != record.payload['content_sha256']:
        raise RuntimeUnavailable('原计划修订内容校验失败。')
    return dict(record.payload['content'], plan_snapshot_id=identifier)
