# 全文持久化


from sqlalchemy import delete, select

from models import PaperDocument
from storage.database import make_db
from storage.db_models import (
    PaperRecord,
    PaperDocumentRecord,
    DocumentChunkRecord,
)


class PaperDocumentRepository:
    """管理论文全文与证据片段。"""

    def __init__(self, database_url: str, *, db_factory=None):
        self.engine, self.SessionLocal = (db_factory or make_db)(
            database_url
        )


    def save(
        self,
        paper_id: int,
        document: PaperDocument,
        *, expected_hash: str | None = None,
    ) -> int:
        """保存全文及其片段，返回文档 ID。"""

        document.validate()

        with self.SessionLocal.begin() as session:

            # 1. 确认对应论文存在
            if session.get(PaperRecord, paper_id) is None:
                raise ValueError(
                    "paper_id 不存在，请先保存论文元数据"
                )

            # 2. 查询该来源是否已经保存
            row = session.scalar(
                select(PaperDocumentRecord).where(
                    PaperDocumentRecord.paper_id
                        == paper_id,
                    PaperDocumentRecord.source_url
                        == document.source_url,
                ).with_for_update()
            )

            # 3. 同一内容且定位信息未变才复用；空白页变化或重新分块也需更新。
            if (
                row is not None
                and row.content_sha256
                    == document.content_sha256
                and row.source_type == document.source_type
                and row.parser == document.parser
                and row.page_count == document.page_count
            ):
                existing = session.scalars(
                    select(DocumentChunkRecord)
                    .where(DocumentChunkRecord.document_id == row.id)
                    .order_by(DocumentChunkRecord.chunk_index)
                ).all()
                saved_layout = [
                    (c.chunk_index, c.text, c.start_char, c.end_char,
                     c.page_number, c.section_hint)
                    for c in existing
                ]
                incoming_layout = [
                    (c.index, c.text, c.start_char, c.end_char,
                     c.page_number, c.section_hint)
                    for c in document.chunks
                ]
                if saved_layout == incoming_layout:
                    return row.id

            if expected_hash is not None and (row is None or row.content_sha256 != expected_hash):
                raise ValueError('原文在重新解析期间已改变，请刷新后重新发起分析。')

            # 4. 没有记录则插入
            if row is None:
                row = PaperDocumentRecord(
                    paper_id=paper_id,
                    source_url=document.source_url,
                    source_type=document.source_type,
                    parser=document.parser,
                    content=document.text,
                    content_sha256=(
                        document.content_sha256
                    ),
                    page_count=document.page_count,
                )

                session.add(row)
                session.flush()

            # 5. 来源相同但内容变化：更新记录
            else:
                row.source_type = document.source_type
                row.parser = document.parser
                row.content = document.text
                row.content_sha256 = (
                    document.content_sha256
                )
                row.page_count = document.page_count

                # 旧片段的位置可能已经失效
                session.execute(
                    delete(DocumentChunkRecord).where(
                        DocumentChunkRecord.document_id
                            == row.id
                    )
                )

            # 6. 保存新的文本片段
            records = [
                DocumentChunkRecord(
                    document_id=row.id,
                    chunk_index=chunk.index,
                    text=chunk.text,
                    page_number=chunk.page_number,
                    section_hint=chunk.section_hint,
                    start_char=chunk.start_char,
                    end_char=chunk.end_char,
                )
                for chunk in document.chunks
            ]

            session.add_all(records)

            session.flush()

            return row.id


    def get_evidence(
        self,
        document_id: int,
        chunk_index: int,
    ) -> dict | None:
        """根据文档和片段编号获取原文证据。"""

        with self.SessionLocal() as session:
            row = session.scalar(
                select(DocumentChunkRecord).where(
                    DocumentChunkRecord.document_id
                        == document_id,
                    DocumentChunkRecord.chunk_index
                        == chunk_index,
                )
            )

            if row is None:
                return None

            source = session.get(
                PaperDocumentRecord,
                document_id,
            )

            return {
                "source_url": source.source_url,
                "page_number": row.page_number,
                "section_hint": row.section_hint,
                "start_char": row.start_char,
                "end_char": row.end_char,
                "quote": row.text,
            }

    def close(self):
        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        self.close()
        return False
