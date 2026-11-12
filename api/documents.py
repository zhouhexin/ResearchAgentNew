"""全文片段与引用查询。位置核验只针对数据库中保存的全文。"""

import os
import re

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from api.pdf_files import local_pdf_path
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from storage.database import make_db
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord, PaperRecord

router = APIRouter(prefix="/api/v1", tags=["原文证据"])


@router.get("/documents/{document_id:int}/file")
def get_document_file(document_id: int):
    engine = None
    try:
        engine, SessionLocal = make_db(os.environ["DATABASE_URL"])
        with SessionLocal() as session:
            document = session.get(PaperDocumentRecord, document_id)
            if document is None:
                raise HTTPException(404, detail=f"全文记录不存在：{document_id}")
            path = local_pdf_path(document)
            if path is None:
                raise HTTPException(404, detail="该全文没有可读取的本地 PDF，文件可能已移动或删除。")
            return FileResponse(
                path, media_type="application/pdf",
                filename=f"document-{document.id}.pdf",
                content_disposition_type="inline",
                headers={"X-Content-Type-Options": "nosniff"},
            )
    except SQLAlchemyError:
        raise HTTPException(503, detail="暂时无法读取 PDF，请检查数据库连接和数据表。") from None
    finally:
        if engine is not None:
            engine.dispose()


def evidence_payload(chunk, document, paper):
    location_valid = (
        0 <= chunk.start_char < chunk.end_char <= len(document.content)
        and document.content[chunk.start_char:chunk.end_char] == chunk.text
        and (
            chunk.page_number is None
            or (
                document.page_count is not None
                and 1 <= chunk.page_number <= document.page_count
            )
        )
    )
    return {
        "id": chunk.id,
        "evidence_id": f"E{chunk.id}",
        "paper_id": paper.id,
        "paper_title": paper.title,
        "document_id": document.id,
        "source_type": document.source_type,
        "page_count": document.page_count,
        "pdf_file_url": (
            f"/api/v1/documents/{document.id}/file"
            if local_pdf_path(document) is not None else None
        ),
        "chunk_index": chunk.chunk_index,
        "source_url": (
            "本地导入 PDF" if document.source_url.lower().startswith("file:")
            else document.source_url
        ),
        "page_number": chunk.page_number,
        "section_hint": chunk.section_hint,
        "text": chunk.text,
        "quote": chunk.text,
        "start_char": chunk.start_char,
        "end_char": chunk.end_char,
        "verification": "location_verified" if location_valid else "unverified",
    }


@router.get("/documents/{document_id:int}/chunks")
def list_document_chunks(
    document_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=100),
):
    engine = None
    try:
        engine, SessionLocal = make_db(os.environ["DATABASE_URL"])
        with SessionLocal() as session:
            document = session.get(PaperDocumentRecord, document_id)
            if document is None:
                raise HTTPException(404, detail=f"全文记录不存在：{document_id}")
            paper = session.get(PaperRecord, document.paper_id)
            if paper is None:
                raise HTTPException(404, detail="全文所属论文不存在。")
            condition = DocumentChunkRecord.document_id == document_id
            total = session.scalar(
                select(func.count()).select_from(DocumentChunkRecord).where(condition)
            ) or 0
            chunks = session.scalars(
                select(DocumentChunkRecord).where(condition)
                .order_by(DocumentChunkRecord.chunk_index, DocumentChunkRecord.id)
                .offset((page - 1) * page_size).limit(page_size)
            ).all()
            return {
                "items": [evidence_payload(chunk, document, paper) for chunk in chunks],
                "total": total, "page": page, "page_size": page_size,
            }
    except SQLAlchemyError:
        raise HTTPException(503, detail="暂时无法查询原文片段，请检查数据库连接和数据表。") from None
    finally:
        if engine is not None:
            engine.dispose()


@router.get("/evidence/{evidence_id}")
def get_evidence(evidence_id: str):
    if not re.fullmatch(r"E[1-9][0-9]*", evidence_id) or len(evidence_id) > 11:
        raise HTTPException(422, detail="证据编号格式应为 E 加正整数，例如 E21。")
    if int(evidence_id[1:]) > 2147483647:
        raise HTTPException(404, detail=f"证据不存在：{evidence_id}")
    engine = None
    try:
        engine, SessionLocal = make_db(os.environ["DATABASE_URL"])
        with SessionLocal() as session:
            row = session.execute(
                select(DocumentChunkRecord, PaperDocumentRecord, PaperRecord)
                .join(PaperDocumentRecord, DocumentChunkRecord.document_id == PaperDocumentRecord.id)
                .join(PaperRecord, PaperDocumentRecord.paper_id == PaperRecord.id)
                .where(DocumentChunkRecord.id == int(evidence_id[1:]))
            ).one_or_none()
            if row is None:
                raise HTTPException(404, detail=f"证据不存在：{evidence_id}")
            return evidence_payload(*row)
    except SQLAlchemyError:
        raise HTTPException(503, detail="暂时无法查询原文证据，请检查数据库连接和数据表。") from None
    finally:
        if engine is not None:
            engine.dispose()
