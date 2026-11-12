import os
import sqlite3

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import exists, func, or_, select
from sqlalchemy.exc import SQLAlchemyError

from storage.database import make_db
from storage.db_models import (
    PaperDocumentRecord,
    PaperRecord,
    DocumentChunkRecord,
)

import requests

from tools.arxiv_search import search_arxiv_page
from tools.open_access_pdf import FulltextError

from storage.repository import (
    IdentityConflictError,
    PaperRepository,
)

import logging

from sqlalchemy.exc import IntegrityError

from api.schemas import PaperBatchRequest
from api.pdf_files import local_pdf_path
from api.analysis_results import current_analysis_paper_ids
from models import Paper

logger = logging.getLogger(__name__)

router = APIRouter(
      prefix="/api/v1/papers",
      tags=["文献库"],
)


@router.get("/{paper_id:int}/documents")
def list_paper_documents(paper_id: int):
    """查看一篇论文已有的全文记录，不修改数据库或读取 PDF 文件。"""
    engine = None
    try:
        engine, SessionLocal = make_db(os.environ["DATABASE_URL"])

        with SessionLocal() as session:
            if session.get(PaperRecord, paper_id) is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"论文不存在：{paper_id}",
                )

            chunk_count = (
                select(func.count())
                .select_from(DocumentChunkRecord)
                .where(
                    DocumentChunkRecord.document_id
                    == PaperDocumentRecord.id
                )
                .correlate(PaperDocumentRecord)
                .scalar_subquery()
            )

            rows = session.execute(
                select(
                    PaperDocumentRecord,
                    chunk_count.label("chunk_count"),
                )
                .where(PaperDocumentRecord.paper_id == paper_id)
                .order_by(PaperDocumentRecord.id.desc())
            ).all()

            return [
                {
                    "id": document.id,
                    "paper_id": document.paper_id,
                    "source_url": (
                        "本地导入 PDF"
                        if document.source_url.lower().startswith("file:")
                        else document.source_url
                    ),
                    "source_type": document.source_type,
                    "parser": document.parser,
                    "page_count": document.page_count,
                    "content_sha256": document.content_sha256,
                    "chunk_count": count,
                    "pdf_file_url": (
                        f"/api/v1/documents/{document.id}/file"
                        if local_pdf_path(document) is not None else None
                    ),
                }
                for document, count in rows
            ]

    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(
            status_code=503,
            detail="暂时无法查询全文记录，请检查数据库连接和数据表。",
        ) from None
    finally:
        if engine is not None:
            engine.dispose()

@router.get("")
def list_papers(
  q: str = "",
  year: int | None = Query(default=None, ge=1900, le=2100),
  has_fulltext: bool | None = None,
  has_analysis: bool | None = None,
  page: int = Query(default=1, ge=1),
  page_size: int = Query(default=20, ge=1, le=100),
):
    engine, SessionLocal = make_db(os.environ["DATABASE_URL"])

    # 判断每篇论文是否存在关联全文。
    fulltext_exists = exists().where(
        PaperDocumentRecord.paper_id == PaperRecord.id
    )

    conditions = []
    if q.strip():
        # 把 % 和 _ 当成普通字符搜索。
        keyword = (
            q.strip()
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_")
        )

        conditions.append(
            or_(
                PaperRecord.title.ilike(
                    f"%{keyword}%", escape="\\"
                ),
                PaperRecord.doi.ilike(
                    f"%{keyword}%", escape="\\"
                ),
            )
        )

    if year is not None:
        conditions.append(PaperRecord.year == year)

    if has_fulltext is not None:
        conditions.append(
            fulltext_exists if has_fulltext else ~fulltext_exists
        )

    try:
        with SessionLocal() as session:
            analysis_ids = current_analysis_paper_ids(session)
            if has_analysis is not None:
                analyzed = PaperRecord.id.in_(analysis_ids)
                conditions.append(analyzed if has_analysis else ~analyzed)
            total = session.scalar(
                select(func.count())
                .select_from(PaperRecord)
                .where(*conditions)
            )

            rows = session.execute(
                select(
                    PaperRecord,
                    fulltext_exists.label("has_fulltext"),
                )
                .where(*conditions)
                .order_by(PaperRecord.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()

            items = [
                {
                    "id": paper.id,
                    "title": paper.title,
                    "authors": paper.authors,
                    "year": paper.year,
                    "doi": paper.doi,
                    "openalex_id": paper.openalex_id,
                    "citation_count": paper.citation_count,
                    "url": paper.url,
                    "abstract": paper.abstract,
                    "has_fulltext": bool(has_text),
                    "has_analysis": paper.id in analysis_ids,
                }
                for paper, has_text in rows
            ]

        return {
            "items": items,
            "total": total or 0,
            "page": page,
            "page_size": page_size,
        }

    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(
            status_code=503,
            detail="暂时无法查询文献库，请检查数据库连接和数据表。",
        ) from None

    finally:
        engine.dispose()


@router.get("/{paper_id:int}")
def get_paper(paper_id: int):
    engine, SessionLocal = make_db(
      os.environ["DATABASE_URL"]
    )

    fulltext_exists = exists().where(
        PaperDocumentRecord.paper_id == PaperRecord.id
    )

    try:
        with SessionLocal() as session:
            row = session.execute(
                select(
                    PaperRecord,
                    fulltext_exists.label("has_fulltext"),
                )
                .where(PaperRecord.id == paper_id)
            ).one_or_none()

            if row is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"论文不存在：{paper_id}",
                )

            paper, has_text = row
            analysis_ids = current_analysis_paper_ids(session, paper_id)

            return {
                "id": paper.id,
                "title": paper.title,
                "authors": paper.authors,
                "year": paper.year,
                "doi": paper.doi,
                "openalex_id": paper.openalex_id,
                "citation_count": paper.citation_count,
                "url": paper.url,
                "abstract": paper.abstract,
                "has_fulltext": bool(has_text),
                "has_analysis": paper.id in analysis_ids,
            }

    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(
            status_code=503,
            detail="暂时无法查询论文，请检查数据库连接和数据表。",
        ) from None

    finally:
        engine.dispose()


@router.get("/search")
def search_papers(
    q: str = Query(min_length=1, max_length=300),
    source: str = "arxiv",
    start_year: int | None = Query(default=None, ge=1900, le=2100),
    end_year: int | None = Query(default=None, ge=1900, le=2100),
    sort: str = "relevance",
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if source != "arxiv":
        raise HTTPException(422, detail="当前论文检索仅支持 arXiv，请使用 source=arxiv。")
    try:
        result = search_arxiv_page(q, page=page, page_size=page_size,
                                   start_year=start_year, end_year=end_year, sort=sort)
    except requests.exceptions.Timeout:
        raise HTTPException(504, detail="arXiv 检索超时，请稍后重试。") from None
    except requests.exceptions.RequestException:
        raise HTTPException(502, detail="暂时无法连接 arXiv，请检查后端网络后重试。") from None
    except FulltextError as exc:
        raise HTTPException(502, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from None
    items = result['items']
    if not items:
        return result
    keys = {key for item in items for key in (item['doi'], item.get('published_doi')) if key}
    urls = {item['url'] for item in items}
    engine = None
    try:
        engine, SessionLocal = make_db(os.environ['DATABASE_URL'])
        with SessionLocal() as session:
            rows = session.scalars(select(PaperRecord).where(or_(PaperRecord.doi.in_(keys), PaperRecord.url.in_(urls)))).all()
            for item in items:
                matched_ids = {row.id for row in rows if row.doi in {item['doi'], item.get('published_doi')} or row.url == item['url']}
                item['identity_verified'] = item['identity_verified'] and len(matched_ids) <= 1
                item['saved_paper_id'] = next(iter(matched_ids)) if len(matched_ids) == 1 else None
    except (SQLAlchemyError, sqlite3.Error, OSError):
        raise HTTPException(503, detail="arXiv 检索已完成，但暂时无法检查收藏状态。") from None
    finally:
        if engine is not None:
            engine.dispose()
    return result

@router.post("/batch")
def save_papers_batch(body: PaperBatchRequest):
    created = 0
    existing = 0
    rejected = 0
    results = []
    try:
        with PaperRepository(
                os.environ["DATABASE_URL"]
        ) as repo:
            for index, item in enumerate(body.papers):
                paper = Paper(
                    title=item.title,
                    authors=item.authors,
                    abstract=item.abstract,
                    year=item.year,
                    doi=item.doi,
                    openalex_id=item.openalex_id or "",
                    url=item.url or "",
                    citation_count=item.citation_count or 0,
                )

                try:
                    paper_id, is_created = (
                        repo.save_with_status(paper)
                    )
                except (IdentityConflictError, ValueError,):
                    rejected += 1

                    results.append({
                        "index": index,
                        "status": "rejected",
                        "paper_id": None,
                        "reason": "论文标识无效或存在身份冲突。",
                    })

                except IntegrityError:
                    rejected += 1

                    results.append({
                        "index": index,
                        "status": "rejected",
                        "paper_id": None,
                        "reason": "数据库约束冲突，请刷新后重试。",
                    })

                else:
                    if item.source == 'openalex' and item.venue_slug is not None:
                        from tools.venue_search.cvpr import verify_publication
                        from storage.venue_repository import save_publication
                        # 忽略浏览器提交的核验状态，在服务端重新核对（成功页面有缓存）。
                        publication = verify_publication(item.model_dump(), item.venue_slug)
                        save_publication(repo.SessionLocal, paper_id, item.year, publication, item.venue_slug)
                    if is_created:
                        created += 1
                        status = "created"
                    else:
                        existing += 1
                        status = "existing"

                    results.append({
                        "index": index,
                        "status": status,
                        "paper_id": paper_id,
                        "reason": None,
                    })
    except (SQLAlchemyError, sqlite3.Error, OSError):
        # 不打印数据库地址、密码或提交的论文内容。
        logger.warning(
            "批量收藏因数据库错误中断。"
            "已完成：created=%s, existing=%s, rejected=%s",
            created,
            existing,
            rejected,
        )

        raise HTTPException(
            status_code=503,
            detail=(
                "数据库操作中断。部分论文可能已经保存，"
                "请重新搜索检查收藏状态后重试。"
            ),
        ) from None

    return {
        "created": created,
        "existing": existing,
        "rejected": rejected,
        "results": results,
    }








