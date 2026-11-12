"""分析前迁移已关联的旧版本地 PDF 文本，不猜测文件归属。"""
import pymupdf
from sqlalchemy import select, func

from api.pdf_files import local_pdf_path
from storage.database import make_db
from storage.db_models import PaperDocumentRecord, DocumentChunkRecord
from storage.document_repository import PaperDocumentRepository
from tools.document_parser import build_document, parse_pdf


def ensure_pdf_reading_order(database_url, paper_id, *, db_factory=None):
    with PaperDocumentRepository(database_url, db_factory=db_factory or make_db) as repo:
        with repo.SessionLocal() as session:
            row = session.scalar(select(PaperDocumentRecord).where(
                PaperDocumentRecord.paper_id == paper_id,
                PaperDocumentRecord.source_type == 'pdf',
            ).order_by(PaperDocumentRecord.id.desc()).limit(1))
            if row is None or row.parser not in {'pymupdf', 'pymupdf-native-v1'}:
                return None
            path = local_pdf_path(row)
            if path is None:
                return None  # 远程来源或文件已移动时，不替换既有原文。
            document_id, source_url, old_hash, old_parser = row.id, row.source_url, row.content_sha256, row.parser
            old_count = session.scalar(select(func.count()).select_from(DocumentChunkRecord).where(
                DocumentChunkRecord.document_id == document_id))

        # 必须能从这个文件逐页还原出旧文本，才自动迁移；避免覆盖不对应的原文。
        with pymupdf.open(path) as pdf:
            legacy = build_document([(n + 1, page.get_text('text', sort=old_parser == 'pymupdf'))
                                     for n, page in enumerate(pdf)],
                                    source_url=source_url, source_type='pdf', parser=old_parser, page_count=len(pdf), chunk_overlap=0)
        if legacy.content_sha256 != old_hash:
            raise ValueError('本地 PDF 与已保存原文不一致，未自动更新。请核对该论文的 PDF 并重新导入。')
        updated = parse_pdf(path)
        updated.source_url = source_url
        saved_id = repo.save(paper_id, updated, expected_hash=old_hash)
        if saved_id != document_id:
            raise ValueError('原文记录在重新解析期间已改变，请刷新后重试。')
        return {'document_id': document_id, 'previous_hash': old_hash,
                'content_sha256': updated.content_sha256, 'parser': updated.parser,
                'previous_parser': old_parser, 'previous_chunks': old_count, 'total_chunks': len(updated.chunks)}
