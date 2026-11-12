"""会议目录与发表记录；只有服务端核验结果可升级状态。"""
from datetime import datetime
from sqlalchemy import select
from storage.venue_models import VenueRecord, VenueIdentifierRecord, VenueRankingRecord, PaperPublicationRecord
from tools.venue_search.catalog import VENUES


def initialize_venues(engine):
    for cls in (VenueRecord, VenueIdentifierRecord, VenueRankingRecord, PaperPublicationRecord):
        cls.__table__.create(engine, checkfirst=True)


def seed_venue(session, slug, sources=()):
    definition = VENUES[slug]
    venue = session.scalar(select(VenueRecord).where(VenueRecord.slug == slug))
    if venue is None:
        venue = VenueRecord(**{key: definition[key] for key in ('slug', 'name', 'acronym', 'venue_type', 'field', 'official_url')})
        session.add(venue);session.flush()
    for source in sources:
        exists = session.scalar(select(VenueIdentifierRecord).where(
            VenueIdentifierRecord.venue_id == venue.id, VenueIdentifierRecord.provider == 'openalex',
            VenueIdentifierRecord.external_id == source['id']))
        if exists is None:
            session.add(VenueIdentifierRecord(venue_id=venue.id, provider='openalex', external_id=source['id'],
                                             source_url='https://openalex.org/'+source['id']))
    return venue


def seed_cvpr(session, sources=()):
    return seed_venue(session, 'cvpr', sources)


def save_publication(Session, paper_id, year, publication, venue_slug='cvpr'):
    if not isinstance(year, int):
        return
    with Session.begin() as session:
        venue = seed_venue(session, venue_slug)
        rows = session.scalars(select(PaperPublicationRecord).where(PaperPublicationRecord.paper_id == paper_id,
              PaperPublicationRecord.venue_id == venue.id, PaperPublicationRecord.publication_year == year)).all()
        if any(row.verification_status == 'official_verified' for row in rows) and publication['verification_status'] != 'official_verified':
            return  # 临时上游故障不能把已确认记录降为未知。
        row = next((row for row in rows if row.track == publication['track']), None)
        if row is None and publication['verification_status'] == 'official_verified':
            row = next((row for row in rows if row.track == 'unknown'), None)
        if row is None:
            row = PaperPublicationRecord(paper_id=paper_id, venue_id=venue.id, publication_year=year)
            session.add(row)
        for field in ('track', 'verification_status', 'verification_url', 'reason'):
            setattr(row, field, publication[field])
        row.verified_at = datetime.fromisoformat(publication['verified_at']) if publication.get('verified_at') else None
