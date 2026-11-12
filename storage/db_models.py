
from datetime import datetime

from sqlalchemy import DateTime, Integer, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from storage.database import Base
from sqlalchemy import ForeignKey, UniqueConstraint
from storage.venue_models import VenueRecord, VenueIdentifierRecord, VenueRankingRecord, PaperPublicationRecord

class PaperRecord(Base):
    """对应 PostgreSQL 的 papers 表。"""

    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    title: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    authors: Mapped[list[str]] = mapped_column(
        JSONB, default=list, nullable=False
    )

    abstract: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )

    year: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    doi: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )

    url: Mapped[str] = mapped_column(
        Text, default="", nullable=False
    )

    citation_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )

    openalex_id: Mapped[str | None] = mapped_column(
        Text, unique=True, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class PaperDocumentRecord(Base):
    """论文全文表。"""

    __tablename__ = "paper_documents"

    __table_args__ = (
        UniqueConstraint(
            "paper_id",
            "source_url",
            name="uq_paper_document_source",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    paper_id: Mapped[int] = mapped_column(
        ForeignKey(
            "papers.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    source_url: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    source_type: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    parser: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    content: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    content_sha256: Mapped[str] = mapped_column(
        Text, nullable=False
    )

    page_count: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class DocumentChunkRecord(Base):
    """可追溯文本片段表。"""

    __tablename__ = "document_chunks"

    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "chunk_index",
            name="uq_document_chunk_index",
        ),
    )

    id: Mapped[int] = mapped_column(
        primary_key=True
    )

    document_id: Mapped[int] = mapped_column(
        ForeignKey(
            "paper_documents.id",
            ondelete="CASCADE",
        ),
        nullable=False,
        index=True,
    )

    chunk_index: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    page_number: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )

    section_hint: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )

    start_char: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    end_char: Mapped[int] = mapped_column(
        Integer, nullable=False
    )

    text: Mapped[str] = mapped_column(
        Text, nullable=False
    )
