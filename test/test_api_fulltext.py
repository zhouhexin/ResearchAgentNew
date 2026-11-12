import pymupdf
import pytest

from api import documents, fulltext, uploads
from storage.db_models import PaperRecord
from test.test_api_uploads import client as upload_client, pdf_bytes
from test.test_api_documents import client as database_client
from test.test_open_access_pdf import Response, public_dns
from tools import open_access_pdf as oa


@pytest.fixture
def client(upload_client, monkeypatch, public_dns, tmp_path):
    monkeypatch.setenv('RESEARCHPILOT_FULLTEXT_SOURCES_FILE', str(tmp_path / 'sources.json'))
    monkeypatch.setattr(fulltext, 'make_db', documents.make_db)
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        paper = session.get(PaperRecord, 2)
        paper.doi = '10.1234/example'
        paper.openalex_id = 'https://openalex.org/W123'
    upload_client.app.include_router(fulltext.router)
    return upload_client


def mock_source(monkeypatch, content, locations=None):
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/works/'):
            return Response(data={
                'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example',
                'best_oa_location': {'is_oa': True, 'pdf_url': 'https://example.org/paper.pdf'},
                'locations': locations or [],
            })
        return Response(content=content)
    monkeypatch.setattr(oa.requests, 'get', get)


def test_timeout_diagnostic_and_duplicate_download_url_not_retried(client,monkeypatch):
    import requests,json
    url='https://example.org/file.pdf?token=private-secret'
    monkeypatch.setattr(fulltext,'find_open_pdf_urls',lambda **k:[url])
    monkeypatch.setattr(fulltext,'find_doi_pdf_urls',lambda *a,**k:[])
    monkeypatch.setattr(fulltext,'find_arxiv_pdf_urls',lambda **k:[url])
    calls=[]
    class Stalled(Response):
        def iter_content(self,chunk_size):
            yield b'%PDF-data'
            raise requests.ReadTimeout('private-secret')
    def get(url,**kwargs):calls.append(url);return Stalled()
    monkeypatch.setattr(oa.requests,'get',get)
    response=client.post('/api/v1/papers/2/documents/fetch')
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='failed' and task['error']['code']=='PDF_READ_TIMEOUT',task
    assert task['error']['diagnostic']['received_bytes']==9
    assert task['error']['diagnostic']['pdf_header_verified'] is True
    assert task['download_progress']['received_bytes']==9
    assert task['download_progress']['host']=='example.org'
    assert len(calls)==1 and len(task['download_attempts'])==1
    assert task['source_results'][-1]['status']=='skipped'
    assert task['source_results'][0]['lookup_status']=='found' and task['source_results'][0]['candidate_count']==1
    assert 'private-secret' not in json.dumps(task)


def test_active_fulltext_request_is_reused(client,monkeypatch):
    monkeypatch.setattr(fulltext,'acquire_fulltext',lambda *a:None)
    first=client.post('/api/v1/papers/2/documents/fetch').json()
    second=client.post('/api/v1/papers/2/documents/fetch').json()
    assert first['task_id']==second['task_id'] and second['reused'] is True


def test_invalid_timeout_configuration_rejected_before_background_work(client,monkeypatch):
    monkeypatch.setenv('PDF_DOWNLOAD_TIMEOUT','bad')
    result=client.post('/api/v1/papers/2/documents/fetch')
    assert result.status_code==422 and 'PDF_DOWNLOAD_TIMEOUT' in result.text


def test_no_candidates_distinguished_from_failed_pdf_transfer(client,monkeypatch):
    monkeypatch.setattr(fulltext,'find_open_pdf_urls',lambda **k:[])
    monkeypatch.setattr(fulltext,'find_doi_pdf_urls',lambda *a,**k:[])
    monkeypatch.setattr(fulltext,'find_arxiv_pdf_urls',lambda **k:[])
    task_id=client.post('/api/v1/papers/2/documents/fetch').json()['task_id']
    task=client.get('/api/v1/tasks/'+task_id).json()
    assert task['error']['code']=='NO_OPEN_PDF'
    assert not task.get('download_progress')
    assert all(s['lookup_status']=='unavailable' and s['candidate_count']==0 for s in task['source_results'])


@pytest.mark.parametrize('cvf_available', [True, False])
@pytest.mark.parametrize('venue', ['CVPR', 'ICCV'])
def test_cvpr_official_pdf_priority_and_fallback(client, monkeypatch, pdf_bytes, cvf_available, venue):
    from tools.open_access_pdf import FulltextError
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        year = 2024 if venue == 'CVPR' else 2025
        session.get(PaperRecord, 2).url = f'https://openaccess.thecvf.com/content/{venue}{year}/html/Wang_Depth_{venue}_{year}_paper.html'
    def cvf(*args):
        if not cvf_available:
            raise FulltextError('NO_CVF_PDF', 'CVF 暂不可用')
        return ['https://openaccess.thecvf.com/content/CVPR2024/papers/Wang_Depth_paper.pdf']
    monkeypatch.setattr(fulltext, 'find_cvpr_pdf_urls' if venue=='CVPR' else 'find_cvf_pdf_urls', cvf)
    mock_source(monkeypatch, pdf_bytes)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['download_provider'] == ('CVF' if cvf_available else 'OpenAlex')
    assert task['source_results'][0]['provider'] == 'CVF'
    assert len(client.get('/api/v1/papers/2/documents').json()) == 1


@pytest.mark.parametrize('available',[True,False])
@pytest.mark.parametrize('venue,url',[('AAAI','https://ojs.aaai.org/index.php/AAAI/article/view/34950'),('IJCAI','https://ijcai.org/proceedings/2025/896')])
def test_publisher_pdf_priority_and_fallback(client,monkeypatch,pdf_bytes,available,venue,url):
    from tools.open_access_pdf import FulltextError
    _,Session=documents.make_db('sqlite://')
    with Session.begin() as session:session.get(PaperRecord,2).url=url
    calls=[]
    def lookup(*args):
        calls.append(args)
        if not available:raise FulltextError('NO_PROCEEDINGS_PDF','官方论文集暂不可用')
        return ['https://example.org/official.pdf']
    monkeypatch.setattr(fulltext,'find_proceedings_pdf_urls',lookup)
    mock_source(monkeypatch,pdf_bytes)
    response=client.post('/api/v1/papers/2/documents/fetch')
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='succeeded',task
    assert task['download_provider']==(venue+' 官方论文集' if available else 'OpenAlex')
    assert task['source_results'][0]['provider']==venue+' 官方论文集'
    assert len(calls)==1 and calls[0][3]==url
    assert len(client.get('/api/v1/papers/2/documents').json())==1


def test_fetch_download_parse_and_repeat(client, monkeypatch, pdf_bytes):
    mock_source(monkeypatch, pdf_bytes)
    response = client.post('/api/v1/papers/2/documents/fetch')
    assert response.status_code == 202
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded' and task['downloaded'] is True
    assert task['download_source_url'] == 'https://example.org/paper.pdf'
    document_id = task['result_id']
    docs = client.get('/api/v1/papers/2/documents').json()
    assert len(docs) == 1 and docs[0]['page_count'] == 2
    assert client.get(docs[0]['pdf_file_url']).content == pdf_bytes
    chunks = client.get(f'/api/v1/documents/{document_id}/chunks').json()['items']
    assert all(c['verification'] == 'location_verified' and c['paper_id'] == 2 for c in chunks)
    repeat = client.post('/api/v1/papers/2/documents/fetch')
    repeat_task = client.get('/api/v1/tasks/' + repeat.json()['task_id']).json()
    assert repeat_task['result_id'] == document_id
    assert len(client.get('/api/v1/papers/2/documents').json()) == 1
    assert client.get(f'/api/v1/documents/{document_id}/chunks').json()['items'] == chunks


def test_no_open_pdf_and_missing_paper(client, monkeypatch):
    monkeypatch.setattr(oa.requests, 'get', lambda *a, **k: Response(data={
        'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example', 'locations': [],
    }))
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'failed' and task['error']['code'] == 'NO_OPEN_PDF'
    assert [r['provider'] for r in task['source_results']] == ['OpenAlex', 'DOI 原站', 'arXiv']
    assert all(r['message'] for r in task['source_results'])
    assert client.get('/api/v1/papers/2/documents').json() == []
    assert client.post('/api/v1/papers/999/documents/fetch').status_code == 404
    assert client.post('/api/v1/papers/1/documents/fetch').status_code == 422


def test_non_pdf_falls_back_to_other_location(client, monkeypatch, pdf_bytes):
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/'):
            return Response(data={
                'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example',
                'locations': [
                    {'is_oa': True, 'pdf_url': 'https://example.org/login'},
                    {'is_oa': True, 'pdf_url': 'https://example.org/working.pdf'},
                ],
            })
        return Response(content=pdf_bytes if url.endswith('working.pdf') else b'<html>login</html>')
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['download_source_url'].endswith('/working.pdf')


def test_scanned_pdf_kept_locally_when_text_parsing_fails(client, monkeypatch):
    with pymupdf.open() as pdf:
        pdf.new_page()
        data = pdf.tobytes()
    mock_source(monkeypatch, data)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'failed' and task['downloaded'] is True
    assert task['error']['code'] == 'PDF_PARSE_ERROR'
    assert len(list((uploads.UPLOAD_ROOT / '2').glob('*.pdf'))) == 1
    assert client.get('/api/v1/papers/2/documents').json() == []


@pytest.mark.parametrize('openalex_state', ['empty', 'unreachable', 'broken_pdf'])
def test_arxiv_fallback_when_openalex_has_no_pdf(client, monkeypatch, pdf_bytes, openalex_state):
    from tools import arxiv_pdf as arxiv
    from test.test_arxiv_pdf import ATOM
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        paper = session.get(PaperRecord, 2)
        paper.title = 'Attention Is All You Need'
        paper.authors = ['Ashish Vaswani']
    monkeypatch.setattr(arxiv, '_last_query', 0)
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/'):
            if openalex_state == 'unreachable':
                import requests
                raise requests.ConnectionError('private network details')
            locations = [{'is_oa': True, 'pdf_url': 'https://example.org/broken.pdf'}] if openalex_state == 'broken_pdf' else []
            return Response(data={'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example', 'locations': locations})
        if url == 'https://example.org/broken.pdf':
            return Response(content=b'<html>not a PDF</html>')
        if url.startswith('https://doi.org/'):
            return Response(content=b'<html>No PDF link</html>')
        if url == arxiv.ARXIV_API:
            assert kwargs['params']['search_query'] == 'ti:"Attention Is All You Need"'
            return Response(content=ATOM.format(doi='').encode())
        assert url == 'https://arxiv.org/pdf/1706.03762v7'
        return Response(content=pdf_bytes)
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['download_provider'] == 'arXiv'
    assert task['download_source_url'] == 'https://arxiv.org/pdf/1706.03762v7'
    assert len(client.get('/api/v1/papers/2/documents').json()) == 1


def test_explicit_arxiv_without_openalex_or_doi(client, monkeypatch, pdf_bytes):
    from tools import arxiv_pdf as arxiv
    from test.test_arxiv_pdf import ATOM
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        paper = session.get(PaperRecord, 2)
        paper.doi = None
        paper.openalex_id = None
        paper.url = 'https://arxiv.org/abs/1706.03762v7'
    monkeypatch.setattr(arxiv, 'query_arxiv', lambda params: ATOM.format(doi='').encode())
    monkeypatch.setattr(oa.requests, 'get', lambda *a, **k: Response(content=pdf_bytes))
    response = client.post('/api/v1/papers/2/documents/fetch')
    assert response.status_code == 202
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded' and task['download_provider'] == 'arXiv'


def test_doi_repository_fallback(client, monkeypatch, pdf_bytes):
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/'):
            return Response(data={'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example', 'locations': []})
        if url.startswith('https://doi.org/'):
            return Response(status=302, headers={'Location': 'https://repository.example.org/item'})
        if url.endswith('/item'):
            return Response(content=b'<meta name="citation_pdf_url" content="https://repository.example.org/file.pdf">')
        return Response(content=pdf_bytes)
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded' and task['download_provider'] == 'DOI 原站'
    assert task['download_source_url'] == 'https://repository.example.org/file.pdf'


@pytest.mark.parametrize('page_mode', ['download_button', 'javascript_shell', 'saved_source'])
def test_eth_page_download_complete_flow(client, monkeypatch, pdf_bytes, page_mode):
    import json
    from test.test_eth_pdf import PAGE, DOI, ITEM, STREAM, ORIGIN, repository_response
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        paper = session.get(PaperRecord, 2)
        paper.doi = DOI
        if page_mode == 'saved_source':
            paper.url = PAGE
    seen = []
    def get(url, **kwargs):
        seen.append(url)
        if url.startswith('https://api.openalex.org/'):
            return Response(data={'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/' + DOI, 'locations': []})
        if url.startswith('https://doi.org/'):
            return Response(status=302, headers={'Location': PAGE})
        if url == PAGE:
            body = f'<a href="/bitstreams/{STREAM}/download"><span>Download PDF</span></a>' if page_mode == 'download_button' else '<html><app-root></app-root></html>'
            return Response(content=body.encode())
        if url.endswith('/content') or url.endswith('/download'):
            return Response(content=pdf_bytes)
        return Response(content=json.dumps(repository_response(url)).encode())
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded'
    assert task['download_provider'] == 'DOI 原站'
    record = client.get('/api/v1/papers/2/documents').json()[0]
    assert record['page_count'] == 2
    assert client.get(record['pdf_file_url']).content == pdf_bytes
    if page_mode == 'saved_source':
        assert not any(url.startswith('https://doi.org/') for url in seen)


def test_registered_repository_source_skips_doi_resolver(client, monkeypatch, pdf_bytes, tmp_path):
    import json
    from test.test_eth_pdf import PAGE, DOI, repository_response
    path = tmp_path / 'sources.json'
    path.write_text(json.dumps({DOI: PAGE}))
    _, Session = documents.make_db('sqlite://')
    with Session.begin() as session:
        paper = session.get(PaperRecord, 2)
        paper.doi = DOI
        paper.url = 'https://doi.org/' + DOI
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/'):
            return Response(data={'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/' + DOI, 'locations': []})
        assert not url.startswith('https://doi.org/')
        if url.endswith('/content'):
            return Response(content=pdf_bytes)
        return Response(content=json.dumps(repository_response(url)).encode())
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    task = client.get('/api/v1/tasks/' + response.json()['task_id']).json()
    assert task['status'] == 'succeeded' and task['download_provider'] == 'DOI 原站'


def test_provider_http_status_persisted_without_sensitive_url(client, monkeypatch):
    import requests
    def get(url, **kwargs):
        if url.startswith('https://api.openalex.org/'):
            return Response(data={'id': 'https://openalex.org/W123', 'doi': 'https://doi.org/10.1234/example', 'locations': []})
        response = requests.Response()
        response.status_code = 403
        response.url = 'https://doi.org/private?api_key=secret-value'
        response._content = b'Forbidden'
        response._content_consumed = True
        return response
    monkeypatch.setattr(oa.requests, 'get', get)
    response = client.post('/api/v1/papers/2/documents/fetch')
    result = client.get('/api/v1/tasks/' + response.json()['task_id'])
    source = result.json()['source_results'][1]
    assert source['diagnostic']['http_status'] == 403
    assert source['diagnostic']['host'] == 'doi.org'
    assert 'HTTP 403' in source['message']
    assert 'secret-value' not in result.text


@pytest.mark.parametrize('available',[True,False])
@pytest.mark.parametrize('url',['https://aclanthology.org/2024.acl-long.1/','https://proceedings.mlr.press/v235/han24b.html','https://openreview.net/forum?id=Abcdef1234'])
def test_extended_official_pdf_priority_and_fallback(client,monkeypatch,pdf_bytes,available,url):
    from tools.open_access_pdf import FulltextError
    _,Session=documents.make_db('sqlite://')
    with Session.begin() as session:session.get(PaperRecord,2).url=url
    calls=[]
    def lookup(*args):
        calls.append(args)
        if not available:raise FulltextError('NO_OFFICIAL_PDF','官方记录未确认')
        return ['https://example.org/official.pdf']
    monkeypatch.setattr(fulltext,'find_extended_pdf_urls',lookup)
    mock_source(monkeypatch,pdf_bytes)
    response=client.post('/api/v1/papers/2/documents/fetch')
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='succeeded',task
    assert task['download_provider']==('官方论文集' if available else 'OpenAlex')
    assert len(calls)==1 and calls[0][3]==url
