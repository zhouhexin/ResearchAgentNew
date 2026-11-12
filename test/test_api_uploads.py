"""HTTP 上传 → 真实 PDF 解析 → 全文入库。数据库使用隔离 SQLite。"""
import io

import pymupdf
import pytest
from sqlalchemy.exc import OperationalError

from api import documents, tasks, uploads
from storage import document_repository
from test.test_api_documents import client as database_client


@pytest.fixture
def client(database_client, monkeypatch, tmp_path):
    factory = documents.make_db
    monkeypatch.setattr(uploads, 'make_db', factory)
    monkeypatch.setattr(document_repository, 'make_db', factory)
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr(uploads, 'UPLOAD_ROOT', tmp_path / 'uploads')
    database_client.app.include_router(uploads.router)
    database_client.app.include_router(tasks.router)
    return database_client


@pytest.fixture
def pdf_bytes():
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'First page: real evidence text.')
        pdf.new_page().insert_text((72, 72), 'Second page: another evidence paragraph.')
        return pdf.tobytes()


def upload(client, content, paper_id=2, name='paper.pdf', confirmed='true'):
    return client.post(f'/api/v1/papers/{paper_id}/documents',
                       files={'file': (name, io.BytesIO(content), 'application/pdf')},
                       data={'confirmed': confirmed})


def test_upload_parse_save_preview_and_evidence(client, pdf_bytes):
    response = upload(client, pdf_bytes)
    assert response.status_code == 202
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['progress'] == 100
    assert task['paper_id'] == 2
    document_id = int(task['result_id'])
    records = client.get('/api/v1/papers/2/documents').json()
    assert len(records) == 1 and records[0]['id'] == document_id
    assert records[0]['page_count'] == 2
    assert records[0]['chunk_count'] == 2
    assert client.get(records[0]['pdf_file_url']).content == pdf_bytes
    chunks = client.get(f'/api/v1/documents/{document_id}/chunks').json()['items']
    assert [c['page_number'] for c in chunks] == [1, 2]
    assert all(c['paper_id'] == 2 and c['verification'] == 'location_verified' for c in chunks)
    assert client.get('/api/v1/evidence/' + chunks[0]['evidence_id']).json() == chunks[0]
    assert not list((uploads.UPLOAD_ROOT / '.staging').glob('*.pdf'))


def test_repeat_upload_keeps_document_and_evidence_ids(client, pdf_bytes):
    first = upload(client, pdf_bytes)
    task = client.get('/api/v1/tasks/' + first.json()['task_id']).json()
    document_id = task['result_id']
    before = client.get(f'/api/v1/documents/{document_id}/chunks').json()['items']
    second = upload(client, pdf_bytes, name='renamed.pdf')
    repeated = client.get('/api/v1/tasks/' + second.json()['task_id']).json()
    assert repeated['result_id'] == document_id
    assert repeated['status'] == 'succeeded'
    assert len(client.get('/api/v1/papers/2/documents').json()) == 1
    assert client.get(f'/api/v1/documents/{document_id}/chunks').json()['items'] == before
    # 同一 PDF 绑定到另一篇论文时，保持归属隔离。
    other = upload(client, pdf_bytes, paper_id=3)
    other_task = client.get('/api/v1/tasks/' + other.json()['task_id']).json()
    assert other_task['result_id'] != document_id


def test_changed_content_creates_separate_source(client, pdf_bytes):
    upload(client, pdf_bytes)
    with pymupdf.open(stream=pdf_bytes, filetype='pdf') as pdf:
        pdf[0].insert_text((72, 100), 'Updated content.')
        changed = pdf.tobytes()
    upload(client, changed)
    records = client.get('/api/v1/papers/2/documents').json()
    assert len(records) == 2
    assert len({r['content_sha256'] for r in records}) == 2


@pytest.mark.parametrize('content,name,confirmed,status', [
    (b'%PDF-fake', 'paper.pdf', 'false', 422),
    (b'%PDF-fake', 'paper.txt', 'true', 422),
    (b'not a pdf', 'paper.pdf', 'true', 422),
    (b'', 'paper.pdf', 'true', 422),
    (b'%PDF-corrupt', 'paper.pdf', 'true', 422),
])
def test_invalid_upload_rejected_without_task(client, content, name, confirmed, status):
    response = upload(client, content, name=name, confirmed=confirmed)
    assert response.status_code == status
    assert client.get('/api/v1/tasks').json()['total'] == 0
    assert not list(uploads.UPLOAD_ROOT.rglob('*.pdf'))


def test_missing_paper_and_size_limit(client, pdf_bytes, monkeypatch):
    assert upload(client, pdf_bytes, paper_id=999).status_code == 404
    monkeypatch.setattr(uploads, 'MAX_PDF_BYTES', 32)
    assert upload(client, pdf_bytes).status_code == 413
    assert client.get('/api/v1/tasks').json()['total'] == 0


def test_scanned_pdf_failure_is_visible_and_not_saved(client):
    with pymupdf.open() as pdf:
        pdf.new_page()
        data = pdf.tobytes()
    response = upload(client, data)
    assert response.status_code == 202
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'failed'
    assert task['error']['code'] == 'PDF_PARSE_ERROR'
    assert 'OCR' in task['error']['message']
    assert client.get('/api/v1/papers/2/documents').json() == []
    assert not list(uploads.UPLOAD_ROOT.rglob('*.pdf'))


def test_encrypted_pdf_rejected(client):
    with pymupdf.open() as pdf:
        pdf.new_page()
        data = pdf.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                           owner_pw='owner', user_pw='secret')
    response = upload(client, data)
    assert response.status_code == 422
    assert '密码' in response.json()['detail']
    assert client.get('/api/v1/tasks').json()['total'] == 0


def test_database_write_failure_is_safe_and_task_is_failed(client, pdf_bytes, monkeypatch):
    def fail(*args):
        raise OperationalError('private secret', {}, Exception('private details'))
    monkeypatch.setattr(document_repository.PaperDocumentRepository, 'save', fail)
    response = upload(client, pdf_bytes)
    assert response.status_code == 202
    task_response = client.get('/api/v1/tasks/' + response.json()['task_id'])
    assert task_response.json()['status'] == 'failed'
    assert task_response.json()['error']['code'] == 'DATABASE_ERROR'
    assert task_response.json()['error']['diagnostic']['error_type']=='OperationalError'
    assert 'private' not in task_response.text
    assert client.get('/api/v1/papers/2/documents').json() == []


def test_pdf_nul_text_is_postgres_compatible_before_database_write(client,pdf_bytes,monkeypatch):
    from psycopg.adapt import Transformer,PyFormat
    extract=pymupdf.Page.get_text
    monkeypatch.setattr(pymupdf.Page,'get_text',lambda page,*a,**kw:extract(page,*a,**kw)+'\x00')
    save=document_repository.PaperDocumentRepository.save
    def guarded_save(repo,paper_id,document):
        assert document.text.count('\ufffd')==2
        for value in [document.text,*(c.text for c in document.chunks)]:
            Transformer().get_dumper(value,PyFormat.TEXT).dump(value)
        return save(repo,paper_id,document)
    monkeypatch.setattr(document_repository.PaperDocumentRepository,'save',guarded_save)
    response=upload(client,pdf_bytes)
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='succeeded',task
    chunks=client.get('/api/v1/documents/'+task['result_id']+'/chunks').json()['items']
    assert all(c['verification']=='location_verified' and '\x00' not in c['quote'] for c in chunks)


def test_persistent_task_list_filter_and_restart_recovery(client, pdf_bytes):
    response = upload(client, pdf_bytes)
    success_id = response.json()['task_id']
    pending = tasks.create_task(2)
    tasks.update_task(pending['id'], status='running', progress=15)
    # 每次查询重开 SQLite 连接，验证状态来自磁盘。
    assert client.get('/api/v1/tasks?status=running').json()['total'] == 1
    tasks.recover_interrupted_tasks()
    recovered = client.get('/api/v1/tasks/' + pending['id']).json()
    assert recovered['status'] == 'failed'
    assert recovered['error']['code'] == 'SERVICE_RESTARTED'
    assert client.get('/api/v1/tasks/' + success_id).json()['status'] == 'succeeded'
    listing = client.get('/api/v1/tasks?page_size=1').json()
    assert listing['total'] == 2 and len(listing['items']) == 1
    assert client.get('/api/v1/tasks/nonexistent').status_code == 404
    assert client.get('/api/v1/tasks?page=0').status_code == 422
