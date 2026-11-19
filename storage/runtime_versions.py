"""P0 不可变内容仓库。使用调用方事务，不自动建表或修改历史任务。"""
from datetime import datetime

from sqlalchemy import DateTime, JSON, String, func
from sqlalchemy.dialects.postgresql import JSONB, insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from agent.agent_version import AgentVersion
from agent.runtime_snapshot import RuntimeSnapshot, RuntimeUnavailable, safe_manifest
from storage.database import Base


class AgentVersionRecord(Base):
    __tablename__ = 'agent_versions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class RuntimeSnapshotRecord(Base):
    __tablename__ = 'runtime_snapshots'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


TABLES = [AgentVersionRecord.__table__, RuntimeSnapshotRecord.__table__]


def save_immutable(session, value):
    if isinstance(value, AgentVersion):
        record_type = AgentVersionRecord
    elif isinstance(value, RuntimeSnapshot):
        record_type = RuntimeSnapshotRecord
    else:
        raise TypeError('只能保存已校验的版本或运行快照。')
    payload = safe_manifest(value.model_dump(mode='json'))
    record = session.get(record_type, value.id)
    if record is None:
        # 数据库负责并发去重；不创建独立事务，不吞掉其他完整性错误。
        dialect = session.get_bind().dialect.name
        insert = {'postgresql': pg_insert, 'sqlite': sqlite_insert}[dialect]
        session.execute(insert(record_type).values(id=value.id, payload=payload)
                        .on_conflict_do_nothing(index_elements=['id']))
        record = session.get(record_type, value.id, populate_existing=True)
    if record.payload != payload:
        raise RuntimeUnavailable('版本仓库内容与编号不符，已拒绝覆盖。')
    return value.id


def load_immutable(session, identity):
    if identity.startswith('version_'):
        record_type, model_type = AgentVersionRecord, AgentVersion
    elif identity.startswith('runtime_'):
        record_type, model_type = RuntimeSnapshotRecord, RuntimeSnapshot
    else:
        raise RuntimeUnavailable('未知版本编号。')
    record = session.get(record_type, identity, populate_existing=True)
    if record is None:
        raise RuntimeUnavailable('原版本内容不存在，不能使用当前配置替代。')
    try:
        value = model_type.model_validate(record.payload)
    except ValueError:
        raise RuntimeUnavailable('原版本内容无效，不能恢复。') from None
    if value.id != identity:
        raise RuntimeUnavailable('版本仓库内容校验失败。')
    return value
