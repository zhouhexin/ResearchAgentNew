"""对照 PDF 原始页面和数据库，检查每个证据片段。"""

from collections import defaultdict
from pathlib import Path

import pymupdf
from sqlalchemy import select

from models import PaperDocument
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from tools.document_parser import NATIVE_PDF_PARSERS, normalize_extracted_text


def verify_pdf(document: PaperDocument, path: str | Path) -> dict:
    document.validate()
    if document.source_type != "pdf":
        raise ValueError("需要 PDF 文档")
    chunks_by_page = defaultdict(list)
    for chunk in document.chunks:
        chunks_by_page[chunk.page_number].append(chunk)
    with pymupdf.open(path) as pdf:
        if document.page_count != len(pdf):
            raise ValueError("PDF 总页数不一致")
        cursor, has_text = 0, False
        for page_number, page in enumerate(pdf, 1):
            # 保留对旧版按坐标排序文档的验收能力。
            expected = normalize_extracted_text(page.get_text("text", sort=document.parser not in NATIVE_PDF_PARSERS)).strip()
            page_chunks = chunks_by_page[page_number]
            if not expected:
                if page_chunks:
                    raise ValueError(f'第 {page_number} 页为空白页，却保存了原文片段')
                continue
            if has_text:
                cursor += 2  # build_document 用两个换行分隔非空页面。
            page_start, page_end = cursor, cursor + len(expected)
            if document.text[page_start:page_end] != expected:
                raise ValueError(f"第 {page_number} 页片段与 PDF 原文不一致")
            covered_end = page_start
            for chunk in page_chunks:
                if not page_start <= chunk.start_char <= covered_end < chunk.end_char <= page_end:
                    raise ValueError(f'第 {page_number} 页窗口越界、遗漏正文或未向前推进')
                if chunk.text != expected[chunk.start_char - page_start:chunk.end_char - page_start]:
                    raise ValueError(f'第 {page_number} 页窗口与 PDF 原文不一致')
                covered_end = chunk.end_char
            if covered_end != page_end:
                raise ValueError(f'第 {page_number} 页存在未覆盖的原文')
            cursor, has_text = page_end, True
        if cursor != len(document.text):
            raise ValueError('全文包含无法对应到 PDF 页面的文字')
    return {
        "pages": document.page_count,
        "characters": len(document.text),
        "chunks": len(document.chunks),
        "all_offsets_valid": True,
        "all_pages_match_pdf": True,
    }


def verify_stored_document(repo, document_id: int, document: PaperDocument) -> list[int]:
    """直接查询两张表，并逐一核验 get_evidence() 的完整返回值。"""
    document.validate()
    with repo.SessionLocal() as session:
        row = session.get(PaperDocumentRecord, document_id)
        if row is None or (
            row.content, row.content_sha256, row.source_url,
            row.source_type, row.parser, row.page_count,
        ) != (
            document.text, document.content_sha256, document.source_url,
            document.source_type, document.parser, document.page_count,
        ):
            raise ValueError("数据库全文或元数据不一致")
        rows = session.scalars(
            select(DocumentChunkRecord)
            .where(DocumentChunkRecord.document_id == document_id)
            .order_by(DocumentChunkRecord.chunk_index)
        ).all()
        if len(rows) != len(document.chunks):
            raise ValueError("数据库片段数量不一致")
        ids = []
        for saved, expected in zip(rows, document.chunks):
            if (
                saved.chunk_index, saved.text, saved.start_char,
                saved.end_char, saved.page_number, saved.section_hint,
            ) != (
                expected.index, expected.text, expected.start_char,
                expected.end_char, expected.page_number, expected.section_hint,
            ):
                raise ValueError("数据库片段内容或定位信息不一致")
            if row.content[saved.start_char:saved.end_char] != saved.text:
                raise ValueError("数据库正文切片不一致")
            evidence = repo.get_evidence(document_id, expected.index)
            if evidence != {
                "source_url": document.source_url,
                "page_number": expected.page_number,
                "section_hint": expected.section_hint,
                "start_char": expected.start_char,
                "end_char": expected.end_char,
                "quote": expected.text,
            }:
                raise ValueError("get_evidence() 与数据库原文不一致")
            ids.append(saved.id)
        if repo.get_evidence(document_id, len(document.chunks)) is not None:
            raise ValueError("数据库存在多余的旧片段")
        return ids
