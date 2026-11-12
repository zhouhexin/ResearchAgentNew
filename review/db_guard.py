
from storage.database import make_db
from storage.db_models import (
    PaperRecord,
    PaperDocumentRecord,
    DocumentChunkRecord,
)
from review.loader import ReviewPaper


def verify_sources_in_session(session, papers: list[ReviewPaper]) -> None:
    """在调用方持有的事务内核验，便于 API 发布前锁定原文。"""
    for paper in papers:
        record = session.get(
            PaperRecord,
            paper.paper_id,
        )

        doc = session.get(
            PaperDocumentRecord,
            paper.document_id,
        )

        if record is None or doc is None:
            raise ValueError(
                "论文或文档已不存在："
                f"P{paper.paper_id}"
            )

        if (
            doc.paper_id != paper.paper_id
            or doc.content_sha256
                != paper.document_hash
            or doc.source_url
                != paper.source_url
            or record.title != paper.title
            or record.doi != paper.doi
        ):
            raise ValueError(
                "论文身份或全文版本不匹配："
                f"P{paper.paper_id}"
            )

        for evidence_id, source in (
            paper.evidence_index.items()
        ):
            chunk = session.get(
                DocumentChunkRecord,
                int(evidence_id[1:]),
            )

            if (
                chunk is None
                or chunk.document_id != doc.id
                or chunk.text != source["text"]
                or chunk.page_number
                    != source.get("page_number")
                or chunk.section_hint != source.get("section_hint")
                or not 0 <= chunk.start_char < chunk.end_char <= len(doc.content)
                or doc.content[chunk.start_char:chunk.end_char] != chunk.text
            ):
                raise ValueError(
                    "原文片段已发生变化："
                    f"P{paper.paper_id}:"
                    f"{evidence_id}"
                )


def verify_sources(database_url: str, papers: list[ReviewPaper]) -> None:
    """核验论文身份、全文版本和证据。"""
    engine, SessionLocal = make_db(database_url)
    try:
        with SessionLocal() as session:
            verify_sources_in_session(session, papers)
    finally:
        engine.dispose()
