"""实验调度和回放发布目录；PG长期状态，显式初始化，不随API worker运行。"""
from sqlalchemy import JSON,String,Float
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped,mapped_column
from storage.database import Base


class EvaluationSchedule(Base):
    __tablename__='evaluation_schedules'
    id:Mapped[str]=mapped_column(String(100),primary_key=True)
    payload:Mapped[dict]=mapped_column(JSON().with_variant(JSONB(),'postgresql'))
    next_at:Mapped[float]=mapped_column(Float)
    lease_token:Mapped[str|None]=mapped_column(String(100),nullable=True)
    lease_until:Mapped[float]=mapped_column(Float,default=0)


class ReplayReleaseChannel(Base):
    __tablename__='replay_release_channels'
    id:Mapped[str]=mapped_column(String(100),primary_key=True)
    payload:Mapped[dict]=mapped_column(JSON().with_variant(JSONB(),'postgresql'))

TABLES=[EvaluationSchedule.__table__,ReplayReleaseChannel.__table__]
