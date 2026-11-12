"""顶刊顶会候选检索；统一服务供手动页面和研究计划复用。"""
import os
import sqlite3
from fastapi import APIRouter, HTTPException, Query
import requests
from sqlalchemy import select, or_
from sqlalchemy.exc import SQLAlchemyError
from storage.database import make_db
from storage.db_models import PaperRecord
from storage.venue_models import PaperPublicationRecord, VenueRecord
from storage.venue_repository import initialize_venues, seed_venue
from tools.venue_search.cvpr import search_cvpr_page, search_venue_page
from tools.open_access_pdf import FulltextError
from tools.venue_search.catalog import VENUES, verification_provider, validate_venues

router = APIRouter(prefix='/api/v1', tags=['顶刊顶会文献检索'])


@router.get('/venues')
def venues():
    return {'items': list(VENUES.values())}


def _search_venues(slugs, query, start_year, end_year, page, page_size, sort, verified_main_only):
    engine = None
    try:
        fetch = search_cvpr_page if slugs == ['cvpr'] else lambda query, **kwargs: search_venue_page(query, venue_slugs=slugs, **kwargs)
        result = fetch(query, page=page, page_size=page_size, start_year=start_year,
            end_year=end_year, sort=sort, verified_main_only=verified_main_only)
        engine, Session = make_db(os.environ['DATABASE_URL'])
        initialize_venues(engine)
        with Session.begin() as session:
            for slug in slugs:
                seed_venue(session, slug, [source for source in result['source_ids'] if source.get('venue_slug', slugs[0]) == slug])
            items = result['items']
            ids = [item['openalex_id'] for item in items]
            dois = [item['doi'] for item in items if item.get('doi')]
            rows = session.scalars(select(PaperRecord).where(or_(PaperRecord.openalex_id.in_(ids), PaperRecord.doi.in_(dois)))).all()
            for item in items:
                found = {row.id for row in rows if row.openalex_id == item['openalex_id'] or (item.get('doi') and row.doi == item['doi'])}
                item['identity_verified'] = len(found) <= 1
                item['saved_paper_id'] = next(iter(found)) if len(found) == 1 else None
        return result
    except requests.Timeout:
        raise HTTPException(504, detail='OpenAlex 顶刊顶会候选检索超时，请稍后重试。') from None
    except requests.RequestException:
        raise HTTPException(502, detail='无法连接 OpenAlex，请检查网络、API Key 或请求额度后重试。') from None
    except FulltextError as exc:
        raise HTTPException(502, detail={'code': exc.code, 'message': str(exc)}) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    except (SQLAlchemyError, sqlite3.Error, OSError, KeyError):
        raise HTTPException(503, detail='会议检索或收藏状态暂时不可用，请检查数据库与缓存存储。') from None
    finally:
        if engine is not None:
            engine.dispose()


@router.get('/venues/papers')
def search_venues(venues: str = Query(min_length=1, max_length=500), query: str = Query(min_length=1, max_length=300),
                  start_year: int | None = Query(default=None, ge=1900, le=2100),
                  end_year: int | None = Query(default=None, ge=1900, le=2100),
                  page: int = Query(default=1, ge=1), page_size: int = Query(default=5, ge=1, le=20),
                  sort: str = 'relevance', verified_main_only: bool = False):
    try:
        slugs = validate_venues([v.strip() for v in venues.split(',')])
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    return _search_venues(slugs, query, start_year, end_year, page, page_size, sort, verified_main_only)


@router.get('/venues/{slug}/papers')
def venue_papers(slug: str, query: str = Query(min_length=1, max_length=300),
                 start_year: int | None = Query(default=None, ge=1900, le=2100),
                 end_year: int | None = Query(default=None, ge=1900, le=2100),
                 page: int = Query(default=1, ge=1), page_size: int = Query(default=5, ge=1, le=20),
                 sort: str = 'relevance', verified_main_only: bool = False):
    if slug not in VENUES:
        raise HTTPException(404, detail='当前支持 '+ '、'.join(v['acronym'] for v in VENUES.values())+'。')
    return _search_venues([slug], query, start_year, end_year, page, page_size, sort, verified_main_only)


@router.get('/papers/{paper_id:int}/publication')
def publication(paper_id: int):
    engine = None
    try:
        engine, Session = make_db(os.environ['DATABASE_URL'])
        initialize_venues(engine)
        with Session() as session:
            if session.get(PaperRecord, paper_id) is None:
                raise HTTPException(404, detail='论文不存在。')
            rows = session.scalars(select(PaperPublicationRecord).where(PaperPublicationRecord.paper_id == paper_id)).all()
            return {'items': [{'venue': {'slug': session.get(VenueRecord,row.venue_id).slug, 'name': session.get(VenueRecord,row.venue_id).name}, 'year': row.publication_year, 'track': row.track,
                    'verification_status': row.verification_status, 'verification_url': row.verification_url,
                    'verification_provider':verification_provider(session.get(VenueRecord,row.venue_id).slug,row.verification_url),
                    'reason': row.reason, 'verified_at': row.verified_at.isoformat() if row.verified_at else None} for row in rows]}
    except (SQLAlchemyError, sqlite3.Error, OSError, KeyError):
        raise HTTPException(503, detail='暂时无法读取论文发表记录。') from None
    finally:
        if engine is not None:
            engine.dispose()
