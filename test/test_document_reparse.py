import pymupdf
import pytest

from api import documents
import document_demo
from storage.db_models import PaperDocumentRecord
from storage.document_repository import PaperDocumentRepository
from tools.document_parser import PDF_PARSER, parse_pdf
from tools.document_verification import verify_stored_document
from test.test_api_documents import client as database_client
from test.test_api_analyses import client as analysis_client, model


def legacy_pdf_in_database(tmp_path):
    from tools.document_parser import build_document
    path = tmp_path / 'legacy-columns.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=800)
        page.insert_textbox((50, 100, 280, 300), 'Left column first sentence.\nLeft column second sentence.')
        page.insert_textbox((320, 100, 550, 300), 'Right column first sentence.\nRight column second sentence.')
        pdf.save(path)
    with pymupdf.open(path) as pdf:
        legacy = build_document([(1, pdf[0].get_text('text', sort=True))], source_url=path.as_uri(),
                                source_type='pdf', parser='pymupdf', page_count=1)
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).source_url = path.as_uri()
    with PaperDocumentRepository('sqlite://', db_factory=documents.make_db) as repo:
        assert repo.save(1, legacy) == 10
    return path, legacy, Session


def test_reparse_uses_linked_pdf_and_updates_layout_and_parser(database_client, monkeypatch, tmp_path):
    path = tmp_path / 'linked-paper.pdf'
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), 'Original left column.\nSecond left sentence.')
        page.insert_text((330, 72), 'Original right column.\nSecond right sentence.')
        pdf.save(path)
    factory = documents.make_db
    monkeypatch.setattr(document_demo, 'make_db', factory)
    monkeypatch.setattr('storage.document_repository.make_db', factory)
    _, Session = factory('sqlite://')
    with Session.begin() as session:
        row = session.get(PaperDocumentRecord, 10)
        row.source_url = path.as_uri()
        row.parser = 'pymupdf'
    linked = document_demo.stored_pdf_for_paper('sqlite://', 1)
    assert linked == path
    parsed = parse_pdf(linked)
    with PaperDocumentRepository('sqlite://') as repo:
        assert repo.save(1, parsed) == 10
        ids = verify_stored_document(repo, 10, parsed)
        assert ids
        assert repo.save(1, parsed) == 10
        assert verify_stored_document(repo, 10, parsed) == ids
    with Session() as session:
        assert session.get(PaperDocumentRecord, 10).parser == PDF_PARSER
    # 不会从另一篇论文或目录中猜测 PDF 归属。
    with pytest.raises(ValueError, match='没有可读取的本地 PDF'):
        document_demo.stored_pdf_for_paper('sqlite://', 2)


@pytest.mark.parametrize('resume', [False, True])
def test_analysis_automatically_reparses_old_text_and_uses_new_plan(analysis_client, model, tmp_path, resume):
    from api import tasks, analyses
    from agent.paper_context import load_paper_context
    from agent.batch_analysis import split_context, plan_fingerprint
    from agent.analysis_schema import PaperAnalysis
    from storage.analysis_batches import AnalysisBatchStore
    path, legacy, Session = legacy_pdf_in_database(tmp_path)
    old = None
    if resume:
        context = load_paper_context('sqlite://', 1, max_chunks=None)
        batches = split_context(context, 2, 4000)
        fingerprint = plan_fingerprint(context, batches, model='test-model', base_url='',
                                       method='json_schema', scope='full', batch_size=2, max_chars=4000)
        old = tasks.create_task(1, task_type='paper_analysis', metadata={
            'plan_fingerprint': fingerprint, 'batch_run_key': 'old-run'})
        tasks.update_task(old['id'], status='failed')
        store = AnalysisBatchStore(tasks.TASK_DB_PATH)
        store.prepare('old-run', fingerprint, batches)
        store.save_completed('old-run', 1, PaperAnalysis(research_questions=[], methodologies=[],
                                                       contributions=[], experiments=[], limitations=[]))
    response = analysis_client.post('/api/v1/analyses', json={
        'paper_id': 1, **({'resume_task_id': old['id']} if old else {})})
    assert response.status_code == 202, response.text
    assert response.json()['source_reparsed'] is True
    task = analysis_client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['source_reparsed']['previous_hash'] == legacy.content_sha256
    assert task['source_reparsed']['content_sha256'] != legacy.content_sha256
    if old:
        assert task['batch_run_key'] != old['batch_run_key']
        assert store.get_completed('old-run', 1) is not None
    current = parse_pdf(path)
    with PaperDocumentRepository('sqlite://', db_factory=documents.make_db) as repo:
        verify_stored_document(repo, 10, current)
    again = analysis_client.post('/api/v1/analyses', json={'paper_id': 1, 'force_restart': True})
    assert again.status_code == 202 and 'source_reparsed' not in again.json()


def test_active_analysis_is_reused_before_reparsing(analysis_client, model, tmp_path):
    from api import tasks
    _, _, Session = legacy_pdf_in_database(tmp_path)
    active = tasks.create_task(1, task_type='paper_analysis')
    response = analysis_client.post('/api/v1/analyses', json={'paper_id': 1})
    assert response.json() == {'task_id': active['id'], 'reused': True}
    with Session() as session:
        assert session.get(PaperDocumentRecord, 10).parser == 'pymupdf'
    assert model['calls'] == 0


def test_mismatched_local_pdf_is_not_used_to_replace_source(analysis_client, model, tmp_path):
    _, _, Session = legacy_pdf_in_database(tmp_path)
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).content_sha256 = 'different-source-hash'
    response = analysis_client.post('/api/v1/analyses', json={'paper_id': 1})
    assert response.status_code == 422 and '本地 PDF 与已保存原文不一致' in response.text
    with Session() as session:
        row = session.get(PaperDocumentRecord, 10)
        assert row.parser == 'pymupdf' and row.content_sha256 == 'different-source-hash'
    assert model['calls'] == 0


def test_native_v1_rechunking_keeps_text_hash_but_invalidates_old_batch_plan(analysis_client, model, tmp_path):
    from models import PaperDocument, DocumentChunk
    from api import tasks
    from agent.paper_context import load_paper_context
    from agent.batch_analysis import split_context, plan_fingerprint
    from agent.analysis_schema import PaperAnalysis
    from storage.analysis_batches import AnalysisBatchStore

    path = tmp_path / 'native-sentences.pdf'
    text = ' '.join(f'Sentence number {i} describes the experiment and its results.' for i in range(36))
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=600, height=900)
        assert page.insert_textbox((50, 50, 550, 850), text, fontsize=11) >= 0
        pdf.save(path)
    with pymupdf.open(path) as pdf:
        raw = pdf[0].get_text('text', sort=False).strip()
    legacy = PaperDocument(source_url=path.as_uri(), source_type='pdf', parser='pymupdf-native-v1',
                           text=raw, page_count=1, chunks=[
                               DocumentChunk(index=i, text=raw[start:start + 1200], start_char=start,
                                             end_char=min(start + 1200, len(raw)), page_number=1)
                               for i, start in enumerate(range(0, len(raw), 1200))])
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperDocumentRecord, 10).source_url = path.as_uri()
    with PaperDocumentRepository('sqlite://', db_factory=documents.make_db) as repo:
        repo.save(1, legacy)
    context = load_paper_context('sqlite://', 1, max_chunks=None)
    batches = split_context(context, 2, 4000)
    fingerprint = plan_fingerprint(context, batches, model='test-model', base_url='',
                                   method='json_schema', scope='full', batch_size=2, max_chars=4000)
    old = tasks.create_task(1, task_type='paper_analysis', metadata={
        'plan_fingerprint': fingerprint, 'batch_run_key': 'native-v1-checkpoint'})
    tasks.update_task(old['id'], status='failed')
    store = AnalysisBatchStore(tasks.TASK_DB_PATH)
    store.prepare(old['batch_run_key'], fingerprint, batches)
    store.save_completed(old['batch_run_key'], 1, PaperAnalysis(research_questions=[], methodologies=[],
                                                              contributions=[], experiments=[], limitations=[]))
    response = analysis_client.post('/api/v1/analyses', json={'paper_id': 1, 'resume_task_id': old['id']})
    assert response.status_code == 202, response.text
    assert response.json()['source_reparsed'] is True
    task = analysis_client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded' and task['batch_run_key'] != old['batch_run_key']
    assert task['source_reparsed']['previous_hash'] == task['source_reparsed']['content_sha256'] == legacy.content_sha256
    assert task['source_reparsed']['previous_parser'] == 'pymupdf-native-v1'
    assert task['source_reparsed']['previous_chunks'] == len(legacy.chunks)
    assert task['resumed_batches'] == 0 and store.get_completed(old['batch_run_key'], 1) is not None
    current = parse_pdf(path)
    assert any(a.end_char > b.start_char for a, b in zip(current.chunks, current.chunks[1:]))
    with PaperDocumentRepository('sqlite://', db_factory=documents.make_db) as repo:
        ids = verify_stored_document(repo, 10, current)
        assert repo.save(1, current) == 10
        assert verify_stored_document(repo, 10, current) == ids
