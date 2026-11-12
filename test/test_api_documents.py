"""用本地数据库测试 HTTP 查询行为；不代表 PostgreSQL 联调已经通过。"""

from hashlib import sha256

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import JSON, MetaData, create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from api import papers, documents, tasks
from storage.database import Base
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord, PaperRecord


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path / 'tasks.sqlite3')
    engine = create_engine(
        "sqlite://", poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    # SQLite 只用于查询测试，给复制的表使用 JSON；不改生产 JSONB 模型。
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        copied = table.to_metadata(metadata)
        if copied.name == "papers":
            copied.c.authors.type = JSON()
    metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        session.add_all([
            PaperRecord(id=1, title="测试论文"),
            PaperRecord(id=2, title="没有全文的论文"),
            PaperRecord(id=3, title="另一篇论文"),
        ])
        session.flush()
        session.add_all([
            PaperDocumentRecord(
                id=10, paper_id=1, source_url="file:///private/example.pdf",
                source_type="pdf", parser="pymupdf", page_count=2,
                content="第一段第二段", content_sha256=sha256(b"pdf").hexdigest(),
            ),
            PaperDocumentRecord(
                id=11, paper_id=1, source_url="https://example.org/article",
                source_type="web", parser="jina_reader", page_count=None,
                content="网页正文", content_sha256=sha256(b"web").hexdigest(),
            ),
            PaperDocumentRecord(
                id=12, paper_id=3, source_url="https://example.org/other",
                source_type="web", parser="jina_reader",
                content="其他论文正文", content_sha256=sha256(b"other").hexdigest(),
            ),
        ])
        session.flush()
        session.add_all([
            DocumentChunkRecord(document_id=10, chunk_index=0, text="第一段", start_char=0, end_char=3, page_number=1),
            DocumentChunkRecord(document_id=10, chunk_index=1, text="第二段", start_char=3, end_char=6, page_number=2),
            DocumentChunkRecord(document_id=12, chunk_index=0, text="其他论文正文", start_char=0, end_char=6, page_number=None),
        ])

    # API 负责释放请求引擎；测试共享内存库须在整项测试结束后才释放。
    class RequestEngine:
        def dispose(self):
            pass

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setattr(papers, "make_db", lambda url: (RequestEngine(), Session))
    monkeypatch.setattr(documents, "make_db", lambda url: (RequestEngine(), Session))
    app = FastAPI()
    app.include_router(papers.router)
    app.include_router(documents.router)
    with TestClient(app) as value:
        yield value
    engine.dispose()


def test_documents_sorted_scoped_and_counted(client):
    response = client.get("/api/v1/papers/1/documents")
    assert response.status_code == 200
    records = response.json()
    assert [record["id"] for record in records] == [11, 10]
    assert [record["chunk_count"] for record in records] == [0, 2]
    assert records[0]["page_count"] is None
    assert records[0]["source_url"] == "https://example.org/article"
    assert records[1]["page_count"] == 2
    assert records[1]["source_url"] == "本地导入 PDF"
    assert "/private/" not in response.text
    assert all(record["pdf_file_url"] is None for record in records)
    assert all(record["paper_id"] == 1 for record in records)


def test_existing_paper_without_documents(client):
    response = client.get("/api/v1/papers/2/documents")
    assert response.status_code == 200
    assert response.json() == []


def test_missing_paper_is_404(client):
    response = client.get("/api/v1/papers/999/documents")
    assert response.status_code == 404
    assert response.json()["detail"] == "论文不存在：999"


def test_database_failure_is_safe_503(client, monkeypatch):
    def fail(url):
        raise OperationalError("private connection details", {}, Exception("private error"))

    monkeypatch.setattr(papers, "make_db", fail)
    response = client.get("/api/v1/papers/1/documents")
    assert response.status_code == 503
    assert "private" not in response.text


def test_chunks_paginated_ordered_and_scoped(client):
    first = client.get('/api/v1/documents/10/chunks?page=1&page_size=1')
    assert first.status_code == 200
    body = first.json()
    assert body['total'] == 2
    assert body['page'] == 1
    assert body['page_size'] == 1
    chunk = body['items'][0]
    assert chunk['chunk_index'] == 0
    assert chunk['paper_id'] == 1
    assert chunk['quote'] == chunk['text'] == '第一段'
    assert chunk['page_number'] == 1
    assert (chunk['start_char'], chunk['end_char']) == (0, 3)
    assert chunk['verification'] == 'location_verified'
    assert '/private/' not in first.text
    second = client.get('/api/v1/documents/10/chunks?page=2&page_size=1').json()
    assert second['items'][0]['chunk_index'] == 1
    assert second['items'][0]['page_number'] == 2
    assert client.get('/api/v1/documents/10/chunks?page=3&page_size=1').json()['items'] == []
    assert client.get('/api/v1/documents/11/chunks').json()['total'] == 0
    assert client.get('/api/v1/documents/999/chunks').status_code == 404


@pytest.mark.parametrize('query', ['page=0', 'page_size=0', 'page_size=101'])
def test_chunks_invalid_pagination(client, query):
    assert client.get('/api/v1/documents/10/chunks?' + query).status_code == 422


def test_evidence_matches_list_and_preserves_unknown_page(client):
    chunk = client.get('/api/v1/documents/10/chunks').json()['items'][0]
    response = client.get('/api/v1/evidence/' + chunk['evidence_id'])
    assert response.status_code == 200
    assert response.json() == chunk
    web_chunk = client.get('/api/v1/documents/12/chunks').json()['items'][0]
    assert web_chunk['paper_id'] == 3
    assert web_chunk['page_number'] is None
    assert client.get('/api/v1/evidence/E999').status_code == 404
    assert client.get('/api/v1/evidence/P1:E1').status_code == 422
    assert client.get('/api/v1/evidence/E0').status_code == 422


def test_corrupt_character_range_is_not_verified():
    paper = PaperRecord(id=1, title='测试')
    document = PaperDocumentRecord(id=10, paper_id=1, content='原始文本', source_url='file:///private/test.pdf', page_count=1)
    chunk = DocumentChunkRecord(id=1, document_id=10, chunk_index=0, text='改写文本', start_char=0, end_char=4, page_number=1)
    assert documents.evidence_payload(chunk, document, paper)['verification'] == 'unverified'
    chunk.text = '原始文本'
    chunk.page_number = 2
    assert documents.evidence_payload(chunk, document, paper)['verification'] == 'unverified'


@pytest.mark.parametrize('path', ['/api/v1/documents/10/chunks', '/api/v1/evidence/E1'])
def test_evidence_database_failure_is_safe_503(client, monkeypatch, path):
    def fail(url):
        raise OperationalError('private connection details', {}, Exception('private error'))
    monkeypatch.setattr(documents, 'make_db', fail)
    response = client.get(path)
    assert response.status_code == 503
    assert 'private' not in response.text


@pytest.fixture
def saved_pdf(client, tmp_path):
    import fitz
    path = tmp_path / '测试论文.pdf'
    with fitz.open() as pdf:
        pdf.new_page()
        pdf.new_page()
        pdf.save(path)
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).source_url = path.as_uri()
    return path


def test_pdf_preview_and_byte_range(client, saved_pdf):
    response = client.get('/api/v1/documents/10/file')
    assert response.status_code == 200
    assert response.headers['content-type'] == 'application/pdf'
    assert response.headers['content-disposition'].startswith('inline;')
    assert response.content == saved_pdf.read_bytes()
    assert '/private/' not in response.headers['content-disposition']
    partial = client.get('/api/v1/documents/10/file', headers={'Range': 'bytes=0-4'})
    assert partial.status_code == 206
    assert partial.content == b'%PDF-'
    records = client.get('/api/v1/papers/1/documents').json()
    assert records[1]['pdf_file_url'] == '/api/v1/documents/10/file'
    assert records[0]['pdf_file_url'] is None


def test_citation_opens_its_exact_document_and_pdf_page(client, saved_pdf):
    # 同篇论文最新的全文是文档 11（网页），引用必须指向原分析的 PDF 10。
    assert client.get('/api/v1/papers/1/documents').json()[0]['id'] == 11
    chunk = client.get('/api/v1/documents/10/chunks?page=2&page_size=1').json()['items'][0]
    response = client.get('/api/v1/evidence/' + chunk['evidence_id'])
    evidence = response.json()
    assert evidence['document_id'] == 10
    assert evidence['source_type'] == 'pdf'
    assert evidence['pdf_file_url'] == '/api/v1/documents/10/file'
    assert evidence['page_count'] == 2 and evidence['page_number'] == 2
    assert str(saved_pdf) not in response.text
    assert client.get(evidence['pdf_file_url']).content == saved_pdf.read_bytes()
    web_chunk = client.get('/api/v1/documents/12/chunks').json()['items'][0]
    assert web_chunk['pdf_file_url'] is None
    assert web_chunk['source_type'] == 'web' and web_chunk['page_number'] is None


def test_missing_or_deleted_pdf(client, saved_pdf):
    saved_pdf.unlink()
    response = client.get('/api/v1/documents/10/file')
    assert response.status_code == 404
    assert str(saved_pdf) not in response.text
    assert client.get('/api/v1/papers/1/documents').json()[1]['pdf_file_url'] is None
    assert client.get('/api/v1/documents/10/chunks').json()['total'] == 2
    chunk = client.get('/api/v1/documents/10/chunks').json()['items'][0]
    evidence = client.get('/api/v1/evidence/' + chunk['evidence_id']).json()
    assert evidence['pdf_file_url'] is None and evidence['quote'] == chunk['text']
    assert client.get('/api/v1/documents/11/file').status_code == 404
    assert client.get('/api/v1/documents/999/file').status_code == 404


def test_invalid_pdf_not_served(client, saved_pdf):
    saved_pdf.write_text('not a PDF')
    assert client.get('/api/v1/documents/10/file').status_code == 404


def test_file_database_failure_is_safe_503(client, monkeypatch):
    def fail(url):
        raise OperationalError('private connection', {}, Exception('private error'))
    monkeypatch.setattr(documents, 'make_db', fail)
    response = client.get('/api/v1/documents/10/file')
    assert response.status_code == 503
    assert 'private' not in response.text
