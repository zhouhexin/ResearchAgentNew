"""新增独立发表信息表，不改变既有论文/分析记录。"""
from datetime import datetime
from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from storage.database import Base


class VenueRecord(Base):
    __tablename__ = 'venues'
    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(Text)
    acronym: Mapped[str] = mapped_column(String(50))
    venue_type: Mapped[str] = mapped_column(String(30))
    field: Mapped[str] = mapped_column(String(50))
    official_url: Mapped[str] = mapped_column(Text)


class VenueIdentifierRecord(Base):
    __tablename__ = 'venue_identifiers'
    __table_args__ = (UniqueConstraint('venue_id', 'provider', 'external_id'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey('venues.id', ondelete='CASCADE'))
    provider: Mapped[str] = mapped_column(String(30))
    external_id: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str] = mapped_column(Text)


class VenueRankingRecord(Base):
    __tablename__ = 'venue_rankings'
    __table_args__ = (UniqueConstraint('venue_id', 'system', 'edition_year'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey('venues.id', ondelete='CASCADE'))
    system: Mapped[str] = mapped_column(String(30))
    edition_year: Mapped[int]
    tier: Mapped[str] = mapped_column(String(10))
    source_url: Mapped[str] = mapped_column(Text)


class PaperPublicationRecord(Base):
    __tablename__ = 'paper_publications'
    __table_args__ = (UniqueConstraint('paper_id', 'venue_id', 'publication_year', 'track'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    paper_id: Mapped[int] = mapped_column(ForeignKey('papers.id', ondelete='CASCADE'), index=True)
    venue_id: Mapped[int] = mapped_column(ForeignKey('venues.id'))
    publication_year: Mapped[int]
    track: Mapped[str] = mapped_column(String(30), default='unknown')
    verification_status: Mapped[str] = mapped_column(String(30), default='indexed_unverified')
    verification_url: Mapped[str | None] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text, default='')
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
