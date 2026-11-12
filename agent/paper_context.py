
from dataclasses import replace

from sqlalchemy import case, select

from agent.analysis_validator import (
    EvidenceSource,
    PaperContext,
)
from storage.database import make_db
from storage.db_models import (
    PaperRecord,
    PaperDocumentRecord,
    DocumentChunkRecord,
)


def limit_context_chars(context: PaperContext, max_chars: int) -> PaperContext:
    """减少完整证据片段，不截断原文；优先从已有样本中均匀取样。"""
    if max_chars < 1:
        raise ValueError("原文字符预算必须为正整数")
    sources = [s for s in context.sources.values() if len(s.text) <= max_chars]
    if not sources:
        raise ValueError("没有能完整放入字符预算的证据片段，请增大 --max-chars")
    for count in range(len(sources), 0, -1):
        indices = (
            [0] if count == 1 else
            [round(i * (len(sources) - 1) / (count - 1)) for i in range(count)]
        )
        selected = [sources[i] for i in indices]
        if sum(len(s.text) for s in selected) <= max_chars:
            return replace(context, sources={s.evidence_id: s for s in selected})
    raise ValueError("原文字符预算不足")


def load_paper_context(
    database_url: str,
    paper_id: int,
    max_chunks: int | None = 12,
    *, fast: bool = False,
) -> PaperContext:
    """加载论文和部分原文片段。"""

    if max_chunks is not None and max_chunks < 2:
        raise ValueError("max_chunks 至少为 2")

    engine, SessionLocal = make_db(
        database_url
    )

    try:
        with SessionLocal() as session:

            # 1. 查找论文
            paper = session.get(
                PaperRecord, paper_id
            )

            if paper is None:
                raise ValueError(
                    f"论文不存在：{paper_id}"
                )

            # 2. 优先选择 PDF 文档
            document = session.scalar(
                select(PaperDocumentRecord)
                .where(
                    PaperDocumentRecord.paper_id
                    == paper_id
                )
                .order_by(
                    case(
                        (
                            PaperDocumentRecord.source_type
                            == "pdf",
                            0,
                        ),
                        else_=1,
                    ),
                    PaperDocumentRecord.id.desc(),
                )
                .limit(1)
            )

            if document is None:
                raise ValueError(
                    f"论文 {paper_id} 尚未关联全文。请先执行："
                    f'python document_demo.py "对应论文.pdf" --paper-id {paper_id}'
                )

            # 3. 读取有序文本片段
            chunks = session.scalars(
                select(DocumentChunkRecord)
                .where(
                    DocumentChunkRecord.document_id
                    == document.id
                )
                .order_by(
                    DocumentChunkRecord.chunk_index
                )
            ).all()

            if not chunks:
                raise ValueError(
                    "文档没有原文片段"
                )

            # 4. 从全文均匀采样
            reading_selection=None
            if fast:
                from agent.fast_reading import choose_chunks,DEFAULT_BUDGET
                selected,reading_selection=choose_chunks(chunks,document.content,max_chunks if max_chunks is not None else DEFAULT_BUDGET)
            elif max_chunks is None or len(chunks) <= max_chunks:
                selected = chunks
            else:
                # `{表达式 for ...}` → **集合推导式** 特点：元素不能重复，自动去重；无序
                indices = sorted({
                    round(
                        i * (len(chunks) - 1)
                        / (max_chunks - 1)
                    )
                    for i in range(max_chunks)
                })

                selected = [
                    chunks[i] for i in indices
                ]

            # 5. 建立本次允许使用的证据集合
            sources = {
                f"E{chunk.id}": EvidenceSource(
                    evidence_id=f"E{chunk.id}",
                    text=chunk.text,
                    source_url=document.source_url,
                    page_number=chunk.page_number,
                    section_hint=chunk.section_hint,
                )
                for chunk in selected
            }

            return PaperContext(
                paper_id=paper.id,
                paper_title=paper.title,
                doi=paper.doi,
                document_id=document.id,
                document_sha256=(
                    document.content_sha256
                ),
                source_url=document.source_url,
                total_chunks=len(chunks),
                sources=sources,
                reading_selection=reading_selection,
            )

    finally:
        engine.dispose()



def assert_document_unchanged(
    database_url: str,
    context: PaperContext,
) -> None:
    """检查分析期间全文是否被更新。"""

    engine, SessionLocal = make_db(
        database_url
    )

    try:
        with SessionLocal() as session:
            document = session.get(
                PaperDocumentRecord,
                context.document_id,
            )

            if (
                document is None
                or document.content_sha256
                != context.document_sha256
            ):
                raise RuntimeError(
                    "文档已更新，请重新分析"
                )
    finally:
        engine.dispose()


class DocumentChangedError(RuntimeError):
    pass


def verify_paper_context(session, context):
    document = session.scalar(select(PaperDocumentRecord).where(PaperDocumentRecord.id == context.document_id).with_for_update())
    if document is None or document.paper_id != context.paper_id or document.content_sha256 != context.document_sha256:
        raise DocumentChangedError()
    ids = [int(e[1:]) for e in context.sources]
    chunks = {f'E{c.id}': c for c in session.scalars(select(DocumentChunkRecord).where(DocumentChunkRecord.document_id == document.id, DocumentChunkRecord.id.in_(ids)))}
    for key, source in context.sources.items():
        row = chunks.get(key)
        if (row is None or not 0 <= row.start_char < row.end_char <= len(document.content)
                or document.content[row.start_char:row.end_char] != row.text
                or (document.source_type == 'pdf' and (row.page_number is None or document.page_count is None or not 1 <= row.page_number <= document.page_count))
                or row.text != source.text or row.page_number != source.page_number
                or row.section_hint != source.section_hint or document.source_url != source.source_url):
            raise DocumentChangedError()


