
import json
import re

from langchain_core.tools import tool
from sqlalchemy import or_, select

from storage.database import Base, make_db
from storage.db_models import (
    PaperRecord,
    PaperDocumentRecord,
    DocumentChunkRecord,
)
from tools.arxiv_search import search_arxiv


def make_research_tools(database_url: str):
    """创建科研工具，并返回数据库 Engine。"""

    engine, SessionLocal = make_db(database_url)
    try:
        Base.metadata.create_all(engine)
    except Exception:
        engine.dispose()
        raise

    @tool
    def search_academic_papers(
        query: str,
        limit: int = 5,
    ) -> str:
        """用 arXiv 搜索学术论文。结果是元数据，不是全文证据。"""

        limit = max(1, min(limit, 10))

        papers = search_arxiv(
            query=query,
            limit=limit,
        )

        results = [
            {
                "title": p.title,
                "year": p.year,
                "doi": p.doi,
                "source": "arxiv",
                "url": p.url,
                "abstract_preview": p.abstract[:500],
            }
            for p in papers
        ]

        return json.dumps(
            {
                "kind": "metadata",
                "results": results,
            },
            ensure_ascii=False,
        )

    @tool
    def search_local_evidence(
        query: str,
        limit: int = 5,
    ) -> str:
        """搜索 PostgreSQL 中的论文原文片段，返回 E 编号。"""

        terms = [
            w for w in re.split(
                r"[\s,，;；]+", query.strip()
            )
            if len(w) >= 2
        ][:5]

        if not terms:
            return json.dumps(
                {"results": [], "note": "请提供检索词"},
                ensure_ascii=False,
            )

        limit = max(1, min(limit, 8))

        # 转义 SQL LIKE 特殊字符
        def escape_like(s: str) -> str:
            return (
                s.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )

        conditions = [
            DocumentChunkRecord.text.ilike(
                f"%{escape_like(term)}%",
                escape="\\",
            )
            for term in terms
        ]

        stmt = (
            select(
                DocumentChunkRecord,
                PaperDocumentRecord,
                PaperRecord,
            )
            .join(
                PaperDocumentRecord,
                DocumentChunkRecord.document_id
                == PaperDocumentRecord.id,
            )
            .join(
                PaperRecord,
                PaperDocumentRecord.paper_id
                == PaperRecord.id,
            )
            .where(or_(*conditions))
            .order_by(
                DocumentChunkRecord.id.desc()
            )
            .limit(limit)
        )

        with SessionLocal() as session:
            rows = session.execute(stmt).all()

            results = [
                {
                    "evidence_id": f"E{chunk.id}",
                    "paper_title": paper.title,
                    "doi": paper.doi,
                    "source_url": doc.source_url,
                    "page_number": chunk.page_number,
                    "section_hint": chunk.section_hint,
                    "quote": chunk.text[:900],
                }
                for chunk, doc, paper in rows
            ]

        return json.dumps(
            {
                "kind": "evidence",
                "results": results,
            },
            ensure_ascii=False,
        )

    @tool
    def read_evidence(evidence_id: str) -> str:
        """按 E 编号读取完整证据片段与论文来源。"""

        if not re.fullmatch(
            r"E[1-9]\d*", evidence_id
        ):
            return json.dumps({
                "error": "证据编号格式应为 E123"
            })

        with SessionLocal() as session:
            chunk = session.get(
                DocumentChunkRecord,
                int(evidence_id[1:]),
            )

            if chunk is None:
                return json.dumps({
                    "error": "证据不存在"
                })

            document = session.get(
                PaperDocumentRecord,
                chunk.document_id,
            )

            paper = session.get(
                PaperRecord,
                document.paper_id,
            )

            result = {
                "kind": "evidence",
                "evidence_id": evidence_id,
                "paper_title": paper.title,
                "doi": paper.doi,
                "source_url": document.source_url,
                "page_number": chunk.page_number,
                "section_hint": chunk.section_hint,
                "start_char": chunk.start_char,
                "end_char": chunk.end_char,
                "quote": chunk.text,
            }

            return json.dumps(
                result,
                ensure_ascii=False,
            )

    tools = [
        search_academic_papers,
        search_local_evidence,
        read_evidence,
    ]

    return tools, engine
