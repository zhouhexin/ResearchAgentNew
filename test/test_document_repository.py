from dataclasses import replace
from pathlib import Path
import shutil

import pymupdf
import pytest
from sqlalchemy import event, func, select

from storage.db_models import DocumentChunkRecord, PaperDocumentRecord, PaperRecord
from storage.document_repository import PaperDocumentRepository
from tools.document_parser import build_document, parse_pdf
from tools.document_verification import verify_pdf, verify_stored_document


@pytest.fixture
def document_repo(isolated_database_url):
    with PaperDocumentRepository(isolated_database_url) as repo:
        with repo.SessionLocal.begin() as session:
            paper = PaperRecord(title="全文联调测试材料", authors=[])
            session.add(paper)
            session.flush()
            paper_id = paper.id
        yield repo, paper_id


def test_real_pdf_duplicate_update_and_restore(document_repo, tmp_path):
    repo, paper_id = document_repo
    source = next(iter(sorted((Path(__file__).resolve().parents[1] / "data/pdfs").glob("*.pdf"))), None)
    if source is None:
        pytest.skip("没有本地真实 PDF")
    path = tmp_path / "real-document.pdf"
    shutil.copyfile(source, path)
    document = parse_pdf(path)
    verify_pdf(document, path)
    document_id = repo.save(paper_id, document)
    old_ids = verify_stored_document(repo, document_id, document)

    # 相同 PDF 重复导入，文档与片段的主键都保持不变。
    assert repo.save(paper_id, parse_pdf(path)) == document_id
    assert verify_stored_document(repo, document_id, document) == old_ids
    with repo.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(PaperDocumentRecord)) == 1

    # 修改同一路径下的 PDF 副本，追加真实页面，重新解析并保存。
    with pymupdf.open(path) as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Updated PDF evidence: revision 2.")
        pdf.saveIncr()
    updated = parse_pdf(path)
    verify_pdf(updated, path)
    assert updated.content_sha256 != document.content_sha256
    assert updated.page_count == document.page_count + 1
    assert repo.save(paper_id, updated) == document_id
    new_ids = verify_stored_document(repo, document_id, updated)
    assert set(old_ids).isdisjoint(new_ids)
    assert repo.get_evidence(document_id, len(updated.chunks) - 1)["page_number"] == updated.page_count

    # 内容缩短回原版时也必须删除多余的末尾片段。
    assert repo.save(paper_id, document) == document_id
    verify_stored_document(repo, document_id, document)
    with repo.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(PaperDocumentRecord)) == 1


def test_web_evidence_and_missing_chunk(document_repo):
    repo, paper_id = document_repo
    document = build_document(
        [(None, "公开网页正文。" * 100)], source_url="https://example.org/article",
        source_type="web", parser="test", chunk_size=100,
    )
    document_id = repo.save(paper_id, document)
    verify_stored_document(repo, document_id, document)
    assert repo.get_evidence(document_id, 0)["page_number"] is None
    assert repo.get_evidence(document_id, -1) is None
    assert repo.get_evidence(document_id + 1000, 0) is None


def test_same_text_with_changed_pages_or_chunk_size(document_repo):
    repo, paper_id = document_repo
    original = build_document(
        [(1, "Same text " * 40)], source_url="file:///layout.pdf",
        source_type="pdf", parser="test", page_count=1, chunk_size=100,
    )
    document_id = repo.save(paper_id, original)
    # 插入空白首页后全文哈希不变，但原文实际位于第 2 页。
    updated = build_document(
        [(1, ""), (2, original.text)], source_url=original.source_url,
        source_type="pdf", parser="test", page_count=2, chunk_size=150,
    )
    assert original.content_sha256 == updated.content_sha256
    assert repo.save(paper_id, updated) == document_id
    verify_stored_document(repo, document_id, updated)


def test_update_failure_rolls_back_document_and_chunks(document_repo):
    repo, paper_id = document_repo
    original = build_document(
        [(1, "Original evidence " * 20)], source_url="file:///rollback.pdf",
        source_type="pdf", parser="test", page_count=1, chunk_size=100,
    )
    document_id = repo.save(paper_id, original)
    old_ids = verify_stored_document(repo, document_id, original)
    updated = build_document(
        [(1, "Updated evidence " * 40)], source_url=original.source_url,
        source_type="pdf", parser="test", page_count=1, chunk_size=100,
    )

    def fail_insert(mapper, connection, target):
        raise RuntimeError("模拟片段插入失败")

    # 插入新片段时全文已经更新、旧片段已经执行删除；此时失败必须整体回滚。
    event.listen(DocumentChunkRecord, "before_insert", fail_insert)
    try:
        with pytest.raises(RuntimeError, match="模拟片段插入失败"):
            repo.save(paper_id, updated)
    finally:
        event.remove(DocumentChunkRecord, "before_insert", fail_insert)
    assert verify_stored_document(repo, document_id, original) == old_ids


def test_invalid_document_does_not_change_database(document_repo):
    repo, paper_id = document_repo
    document = build_document(
        [(1, "Valid text")], source_url="file:///validation.pdf",
        source_type="pdf", parser="test", page_count=1,
    )
    document_id = repo.save(paper_id, document)
    old_ids = verify_stored_document(repo, document_id, document)
    invalid = replace(document, chunks=[replace(document.chunks[0], page_number=None)])
    with pytest.raises(ValueError, match="页码"):
        repo.save(paper_id, invalid)
    assert verify_stored_document(repo, document_id, document) == old_ids
