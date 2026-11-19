"""覆盖检查输入和输出作为不可变长期产物保存。"""
from sqlalchemy import JSON, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from storage.database import Base


class CoverageArtifact(Base):
    __tablename__ = 'research_coverage_artifacts'
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON().with_variant(JSONB(), 'postgresql'), nullable=False)


TABLES = [CoverageArtifact.__table__]
