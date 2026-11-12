import pytest

from api import papers
from storage.db_models import PaperRecord
from storage.repository import PaperRepository
from test.test_api_documents import client as database_client
from tools import arxiv_search

FEED = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/" xmlns:arxiv="http://arxiv.org/schemas/atom">
<opensearch:totalResults>42</opensearch:totalResults>
<entry><id>http://arxiv.org/abs/1706.03762v7</id><title>Attention Is All You Need</title>
<summary>Research abstract.</summary><published>2017-06-12T00:00:00Z</published>
<author><name>Ashish Vaswani</name></author><arxiv:doi>10.1234/journal</arxiv:doi></entry></feed>'''


@pytest.fixture
def client(database_client, monkeypatch):
    engine, Session = papers.make_db('sqlite://')
    class Repository(PaperRepository):
        def __init__(self, url):
            self.engine, self.SessionLocal = engine, Session
    monkeypatch.setattr(papers, 'PaperRepository', Repository)
    monkeypatch.setattr(arxiv_search, 'query_arxiv', lambda params: FEED.encode())
    return database_client


def test_arxiv_pagination_years_and_stable_identifier(monkeypatch):
    seen = []
    def query(params):
        seen.append(params)
        return FEED.encode()
    monkeypatch.setattr(arxiv_search, 'query_arxiv', query)
    result = arxiv_search.search_arxiv_page('monocular depth', page=2, page_size=20, start_year=2017, end_year=2026, sort='newest')
    assert result['total'] == 42 and result['page'] == 2
    assert seen[0]['start'] == 20
    assert seen[0]['sortBy'] == 'submittedDate'
    assert 'submittedDate:[201701010000 TO 202612312359]' in seen[0]['search_query']
    item = result['items'][0]
    assert item['doi'] == '10.48550/arxiv.1706.03762'
    assert item['arxiv_id'] == '1706.03762'
    assert item['pdf_url'].endswith('1706.03762v7')
    assert item['published_doi'] == '10.1234/journal'
    assert item['citation_count'] is None
    assert item['authors'] == ['Ashish Vaswani']


def test_search_save_repeat_and_new_version(client, monkeypatch):
    response = client.get('/api/v1/papers/search?q=Attention&page=2')
    assert response.status_code == 200
    assert response.json()['page'] == 2 and response.json()['total'] == 42
    item = response.json()['items'][0]
    assert item['source'] == 'arxiv' and item['saved_paper_id'] is None
    saved = client.post('/api/v1/papers/batch', json={'papers': [item]})
    assert saved.status_code == 200 and saved.json()['created'] == 1
    paper_id = saved.json()['results'][0]['paper_id']
    assert client.post('/api/v1/papers/batch', json={'papers': [item]}).json()['existing'] == 1
    monkeypatch.setattr(arxiv_search, 'query_arxiv', lambda p: FEED.replace('03762v7', '03762v8').encode())
    updated = client.get('/api/v1/papers/search?q=Attention').json()['items'][0]
    assert updated['saved_paper_id'] == paper_id
    detail = client.get(f'/api/v1/papers/{paper_id}').json()
    assert detail['doi'] == '10.48550/arxiv.1706.03762'
    assert detail['openalex_id'] is None
    assert detail['url'] == 'https://arxiv.org/abs/1706.03762'


def test_legacy_journal_doi_is_recognized_as_saved(client):
    _, Session = papers.make_db('sqlite://')
    with Session.begin() as session:
        session.get(PaperRecord, 1).doi = '10.1234/journal'
    result = client.get('/api/v1/papers/search?q=Attention').json()
    assert result['items'][0]['saved_paper_id'] == 1


@pytest.mark.parametrize('query', ['q=Attention&source=openalex', 'q=Attention&sort=cited', 'q=Attention&start_year=2026&end_year=2025', 'q=Attention&page=0', 'q=%20'])
def test_unsupported_source_and_filters_return_422(client, query):
    assert client.get('/api/v1/papers/search?' + query).status_code == 422


def test_cli_search_returns_repository_compatible_papers(monkeypatch):
    monkeypatch.setattr(arxiv_search, 'query_arxiv', lambda params: FEED.encode())
    result = arxiv_search.search_arxiv('attention', limit=5)
    assert result[0].doi == '10.48550/arxiv.1706.03762'
    assert result[0].openalex_id == ''


def test_search_rejects_non_atom_response(monkeypatch):
    from tools.open_access_pdf import FulltextError
    monkeypatch.setattr(arxiv_search, 'query_arxiv', lambda params: b'<html>Blocked</html>')
    with pytest.raises(FulltextError):
        arxiv_search.search_arxiv_page('depth')


def test_arxiv_search_to_fulltext_uses_arxiv_directly(client, monkeypatch, tmp_path):
    import pymupdf
    from api import documents, fulltext, tasks, uploads
    from storage import document_repository
    from tools import arxiv_pdf, open_access_pdf
    from test.test_open_access_pdf import Response
    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), 'Evidence from arXiv PDF.')
        content = pdf.tobytes()
    factory = papers.make_db
    monkeypatch.setattr(fulltext, 'make_db', factory)
    monkeypatch.setattr(document_repository, 'make_db', factory)
    monkeypatch.setattr(tasks, 'TASK_DB_PATH', tmp_path / 'tasks.sqlite3')
    monkeypatch.setattr(uploads, 'UPLOAD_ROOT', tmp_path / 'pdfs')
    monkeypatch.setattr(arxiv_pdf, 'query_arxiv', lambda params: FEED.encode())
    monkeypatch.setattr(open_access_pdf.socket, 'getaddrinfo', lambda *a, **k: [(2, 1, 6, '', ('93.184.216.34', 443))])
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        assert url == 'https://arxiv.org/pdf/1706.03762v7'
        return Response(content=content)
    monkeypatch.setattr(open_access_pdf.requests, 'get', get)
    client.app.include_router(fulltext.router)
    client.app.include_router(tasks.router)
    item = client.get('/api/v1/papers/search?q=Attention').json()['items'][0]
    saved = client.post('/api/v1/papers/batch', json={'papers': [item]}).json()
    paper_id = saved['results'][0]['paper_id']
    started = client.post(f'/api/v1/papers/{paper_id}/documents/fetch')
    assert started.status_code == 202
    task = client.get('/api/v1/tasks/' + started.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['download_provider'] == 'arXiv'
    assert seen == ['https://arxiv.org/pdf/1706.03762v7']
    chunks = client.get('/api/v1/documents/' + task['result_id'] + '/chunks').json()['items']
    assert chunks[0]['verification'] == 'location_verified'
