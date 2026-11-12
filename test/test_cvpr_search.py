"""受控 OpenAlex/CVF 协议响应验证；不将模拟数据称为联网验收。"""
from copy import deepcopy
import json
import sqlite3

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
import requests
from sqlalchemy import select

from tools.venue_search import cvpr
from tools.open_access_pdf import FulltextError
from api import venues, papers, tasks, research_search
from storage.repository import PaperRepository
from storage.venue_models import PaperPublicationRecord, VenueRecord, VenueIdentifierRecord
from test.test_api_documents import client as database_client
from test.test_research_search import setup as plan_setup, start, edit

TITLE = 'Depth Estimation with Geometric Vision'
MAIN = 'https://openaccess.thecvf.com/content/CVPR2024/html/Wang_Depth_CVPR_2024_paper.html'
WORKSHOP = 'https://openaccess.thecvf.com/content/CVPR2024W/3DVision/html/Wang_Depth_CVPRW_2024_paper.html'
PAGE = '''<html><head><meta name="citation_title" content="Depth Estimation with Geometric Vision">
<meta name="citation_author" content="Alice Wang"><meta name="citation_author" content="Bob Li">
<meta name="citation_doi" content="10.1234/depth"><meta name="citation_pdf_url" content="../papers/Wang_Depth_paper.pdf"></head>
<body><div id="papertitle">Depth Estimation with Geometric Vision</div><div id="authors"><b>Alice Wang, Bob Li</b></div>
<div id="abstract">We evaluate <b>depth estimation</b> for geometric vision.</div></body></html>'''


def candidate(**kwargs):
    return {'title': TITLE, 'authors': ['Alice Wang', 'Bob Li'], 'doi': '10.1234/depth', 'year': 2024, 'url': MAIN, **kwargs}


def work(number=1, url=MAIN, **kwargs):
    return {'id': f'https://openalex.org/W{number}', 'title': TITLE, 'doi': 'https://doi.org/10.1234/depth',
            'publication_year': 2024, 'authorships': [{'author': {'display_name': 'Alice Wang'}}, {'author': {'display_name': 'Bob Li'}}],
            'abstract_inverted_index': {}, 'cited_by_count': 42,
            'primary_location': {'source': {'id': 'https://openalex.org/S123'}, 'landing_page_url': url}, 'locations': [], **kwargs}


@pytest.fixture
def sources(monkeypatch, tmp_path):
    monkeypatch.delenv('OPENALEX_CVPR_SOURCE_IDS', raising=False)
    monkeypatch.setattr(cvpr, 'CACHE_PATH', tmp_path/'cache.sqlite3')
    state = {'calls': [], 'works': [work()], 'page': PAGE, 'index': f'<dt class="ptitle"><a href="{MAIN}">{TITLE}</a></dt>', 'failure': False}
    def get(url, params=None, **kwargs):
        state['calls'].append((url, params))
        if url.startswith('https://api.openalex.org/sources'):
            return json.dumps({'results': [
                {'id': 'https://openalex.org/S123', 'display_name': 'IEEE/CVF Conference on Computer Vision and Pattern Recognition', 'type': 'conference'},
                {'id': 'https://openalex.org/S999', 'display_name': 'CVPR Workshops', 'type': 'conference'}]})
        if url.endswith('/works'):
            return json.dumps({'meta': {'count': 100}, 'results': state['works']})
        if state['failure']:
            raise requests.ConnectionError('private-upstream-detail')
        return state['index'] if '?day=all' in url else state['page']
    monkeypatch.setattr(cvpr, 'cached_get', get)
    return state


@pytest.mark.parametrize('url,track', [(MAIN, 'main'), (WORKSHOP, 'workshop'),
    ('https://openaccess.thecvf.com/content_cvpr_2017/html/Wang_Depth_2017_CVPR_paper.html', 'main'),
    ('https://openaccess.thecvf.com/content_CVPR_2019_workshops/w1/html/Wang_Depth_paper.html', 'workshop')])
def test_official_paths_are_distinct(url, track):
    assert cvpr.cvf_location(url)[1] == track


@pytest.mark.parametrize('url', [MAIN.replace('openaccess.thecvf.com', 'evil.test'), MAIN+'?redirect=https://evil.test',
    MAIN.replace('/CVPR2024/', '/ICCV2024/'), MAIN.replace('/CVPR2024/', '/CVPR2024/Findings/'), 'http://'+MAIN[8:]])
def test_untrusted_or_other_track_urls_are_not_cvpr_main(url):
    assert cvpr.cvf_location(url) is None


def test_main_and_workshop_verify_with_exact_identity(sources):
    for url, track in [(MAIN, 'main'), (WORKSHOP, 'workshop')]:
        result = cvpr.verify_cvpr(candidate(url=url))
        assert result['verification_status'] == 'official_verified' and result['track'] == track
        assert result['verification_url'] == url and result['verified_at']
        assert result['abstract'] == 'We evaluate depth estimation for geometric vision.'
        assert result['pdf_url'].startswith('https://openaccess.thecvf.com/')
    assert cvpr.find_cvpr_pdf_urls(TITLE, ['Alice Wang', 'Bob Li'], '10.1234/depth', MAIN)[0].endswith('.pdf')
    with pytest.raises(FulltextError):
        cvpr.find_cvpr_pdf_urls('Wrong paper', ['Alice Wang'], '10.1234/depth', MAIN)


@pytest.mark.parametrize('change', [{'title': 'Another title'}, {'authors': ['Other Wang']}, {'authors': []},
                                 {'doi': '10.1234/wrong'}, {'year': None}, {'year': 2100}])
def test_identity_mismatch_does_not_upgrade(sources, change):
    result = cvpr.verify_cvpr(candidate(**change))
    assert result['verification_status'] == 'indexed_unverified' and result['track'] == 'unknown'


def test_missing_citation_meta_uses_visible_title_authors(sources):
    sources['page'] = '<div id="papertitle">'+TITLE+'</div><div id="authors"><b>Alice Wang, Bob Li</b></div><div id="abstract">Visible abstract.</div>'
    assert cvpr.verify_cvpr(candidate())['verification_status'] == 'official_verified'


def test_index_lookup_requires_unique_title_and_author_verification(sources):
    assert cvpr.verify_cvpr(candidate(url='https://doi.org/10.1234/depth'))['verification_url'] == MAIN
    other = MAIN.replace('Wang_Depth', 'Li_Depth')
    sources['index'] += f'<a href="{other}">{TITLE}</a>'
    assert cvpr.verify_cvpr(candidate(url='https://doi.org/10.1234/depth'))['verification_status'] == 'indexed_unverified'


def test_source_outage_keeps_candidates_and_main_filter_is_strict(sources):
    sources['failure'] = True
    result = cvpr.search_cvpr_page('depth', start_year=2020, end_year=2025)
    assert result['items'][0]['publication']['verification_status'] == 'indexed_unverified'
    assert 'private-upstream-detail' not in json.dumps(result)
    assert cvpr.search_cvpr_page('depth', verified_main_only=True)['items'] == []


def test_openalex_source_filter_and_metadata(sources):
    result = cvpr.search_cvpr_page('depth', start_year=2023, end_year=2025, page_size=5)
    item = result['items'][0]
    assert item['source'] == 'openalex' and item['venue_slug'] == 'cvpr'
    assert item['publication']['verification_status'] == 'official_verified'
    assert item['abstract_source'] == 'cvf' and item['abstract']
    params = next(params for url, params in sources['calls'] if url.endswith('/works'))
    assert 'primary_location.source.id:S123' in params['filter'] and 'S999' not in params['filter']
    assert 'from_publication_date:2023-01-01' in params['filter'] and params['per_page'] == 5
    assert result['total'] == 100  # 主会筛选不会把本页数量伪装成全局总数。


def test_workshop_excluded_by_verified_main_filter(sources):
    sources['works'] = [work(url=WORKSHOP)]
    assert cvpr.search_cvpr_page('depth')['items'][0]['publication']['track'] == 'workshop'
    assert cvpr.search_cvpr_page('depth', verified_main_only=True)['items'] == []


def test_wrong_source_and_missing_id_skipped(sources):
    sources['works'] = [work(id=None), work(primary_location={'source': {'id': 'https://openalex.org/S999'}})]
    result = cvpr.search_cvpr_page('depth')
    assert result['items'] == [] and result['skipped_count'] == 2


def test_source_mapping_never_uses_fabricated_id(sources, monkeypatch):
    monkeypatch.setenv('OPENALEX_CVPR_SOURCE_IDS', 'not-an-id')
    with pytest.raises(FulltextError, match='S 编号'):
        cvpr.resolve_sources()
    monkeypatch.delenv('OPENALEX_CVPR_SOURCE_IDS')
    monkeypatch.setattr(cvpr, 'get_json', lambda *a: {'results': []})
    with pytest.raises(FulltextError, match='未返回'):
        cvpr.resolve_sources()


def test_cache_omits_key_and_success_only(monkeypatch, tmp_path):
    monkeypatch.setattr(cvpr, 'CACHE_PATH', tmp_path/'cache.sqlite3')
    monkeypatch.setenv('OPENALEX_API_KEY', 'private-cvpr-key')
    monkeypatch.setattr(cvpr.time, 'sleep', lambda *a: None)
    calls = []
    class Response:
        is_redirect = False
        def __enter__(self):return self
        def __exit__(self, *a):pass
        def raise_for_status(self):pass
        def iter_content(self, *a):yield b'{"results":[]}'
    def get(*args, **kwargs):calls.append(kwargs);return Response()
    monkeypatch.setattr(cvpr.requests, 'get', get)
    for _ in range(2):cvpr.cached_get('https://api.openalex.org/sources', {'search': 'cvpr'})
    assert len(calls) == 1 and calls[0]['params']['api_key'] == 'private-cvpr-key'
    with sqlite3.connect(cvpr.CACHE_PATH) as db:
        assert 'private-cvpr-key' not in str(db.execute('SELECT * FROM responses').fetchall())
    with pytest.raises(FulltextError):cvpr.cached_get('https://localhost/secret')
    class Blocked(Response):
        def iter_content(self, *a):yield b'<html>Please verify you are human</html>'
    monkeypatch.setattr(cvpr.requests, 'get', lambda *a, **k: Blocked())
    with pytest.raises(FulltextError, match='未缓存'):
        cvpr.cached_get(MAIN)
    with sqlite3.connect(cvpr.CACHE_PATH) as db:
        assert len(db.execute('SELECT * FROM responses').fetchall()) == 1


@pytest.fixture
def api_client(database_client, monkeypatch, sources):
    engine, Session = papers.make_db('sqlite://')
    class Repository(PaperRepository):
        def __init__(self, url):self.engine, self.SessionLocal = engine, Session
    monkeypatch.setattr(papers, 'PaperRepository', Repository)
    monkeypatch.setattr(venues, 'make_db', lambda *a: (engine, Session))
    monkeypatch.setattr(venues, 'initialize_venues', lambda *a: None)
    app = FastAPI();app.include_router(venues.router);app.include_router(papers.router)
    with TestClient(app) as client:
        yield client, Session


def test_search_collect_and_publication_persisted(api_client, sources):
    client, Session = api_client
    assert client.get('/api/v1/venues').json()['items'][0]['slug'] == 'cvpr'
    response = client.get('/api/v1/venues/cvpr/papers', params={'query': 'depth'})
    assert response.status_code == 200, response.text
    item = response.json()['items'][0]
    item['publication']['track'] = 'workshop'  # 浏览器修改不会覆盖服务端核验。
    saved = client.post('/api/v1/papers/batch', json={'papers': [item]})
    assert saved.status_code == 200, saved.text
    paper_id = saved.json()['results'][0]['paper_id']
    publication = client.get(f'/api/v1/papers/{paper_id}/publication').json()['items'][0]
    assert publication['verification_status'] == 'official_verified' and publication['track'] == 'main'
    assert publication['verification_url'] == MAIN
    assert client.get('/api/v1/venues/cvpr/papers', params={'query': 'depth'}).json()['items'][0]['saved_paper_id'] == paper_id
    assert client.post('/api/v1/papers/batch', json={'papers': [item]}).json()['existing'] == 1
    with Session() as session:
        assert len(session.scalars(select(VenueRecord)).all()) == 1
        assert len(session.scalars(select(VenueIdentifierRecord)).all()) == 1
        assert len(session.scalars(select(PaperPublicationRecord)).all()) == 1
    sources['failure'] = True
    client.post('/api/v1/papers/batch', json={'papers': [item]})
    assert client.get(f'/api/v1/papers/{paper_id}/publication').json()['items'][0]['verification_status'] == 'official_verified'


def test_spoofed_official_status_is_ignored(api_client, sources):
    client, _ = api_client;sources['failure'] = True
    item = cvpr.search_cvpr_page('depth')['items'][0]
    item['publication'] = {'verification_status': 'official_verified', 'track': 'main', 'verification_url': MAIN}
    saved = client.post('/api/v1/papers/batch', json={'papers': [item]}).json()
    value = client.get(f"/api/v1/papers/{saved['results'][0]['paper_id']}/publication").json()
    assert value['items'][0]['verification_status'] == 'indexed_unverified'


def test_api_validation_and_upstream_errors(api_client, monkeypatch):
    client, _ = api_client
    assert client.get('/api/v1/venues/unsupported/papers', params={'query': 'depth'}).status_code == 404
    assert client.get('/api/v1/venues/cvpr/papers', params={'query': 'depth', 'page_size': 21}).status_code == 422
    monkeypatch.setattr(venues, 'search_cvpr_page', lambda *a, **k: (_ for _ in ()).throw(requests.Timeout('secret')))
    result = client.get('/api/v1/venues/cvpr/papers', params={'query': 'depth'})
    assert result.status_code == 504 and 'secret' not in result.text


def test_plan_cvpr_search_screen_resume_and_snapshot(plan_setup, sources):
    client, state = plan_setup
    edit(client, lambda p: p['scope'].update(venue_slug='cvpr', verified_main_only=True, requested_sources=['openalex', 'cvf']))
    response, task, result = start(client, revision=2)
    assert task['status'] == 'succeeded', task
    assert result['source'] == 'openalex' and result['venue_slug'] == 'cvpr'
    assert len(result['candidates']) == 1 and result['duplicate_count'] == 1
    candidate_id = result['candidates'][0]['candidate_id']
    assert candidate_id == 'https://openalex.org/W1'
    assert result['screenings'][candidate_id]['decision'] == 'included'
    assert result['candidates'][0]['publication']['track'] == 'main'
    assert '正式发表' in result['screenings'][candidate_id]['checks'][-1]['explanation']
    calls = len(sources['calls'])
    assert start(client, revision=2)[0]['reused']
    assert len(sources['calls']) == calls


def test_plan_can_refresh_unverified_results_without_overwriting_history(plan_setup, sources):
    client, state = plan_setup
    edit(client, lambda p: p['scope'].update(venue_slug='cvpr', requested_sources=['openalex']))
    sources['failure'] = True
    first, task, result = start(client, revision=2)
    assert task['status'] == 'succeeded' and result['candidates'][0]['publication']['verification_status'] == 'indexed_unverified'
    sources['failure'] = False
    response = client.post('/api/v1/agent/plans/plan_test/search', json={'expected_revision': 2, 'per_query': 10, 'refresh': True})
    assert response.status_code == 202
    second = response.json()
    assert first['run_id'] != second['run_id']
    result = client.get('/api/v1/agent/plans/plan_test/search').json()
    assert len(result['history']) == 2 and result['run']['candidates'][0]['publication']['verification_status'] == 'official_verified'
    old = client.get('/api/v1/agent/plans/plan_test/search', params={'run_id': first['run_id']}).json()
    assert old['run']['candidates'][0]['publication']['verification_status'] == 'indexed_unverified'


def test_diagnostic_cli_reads_configuration_and_redacts_error(monkeypatch, tmp_path, capsys):
    import diagnose_cvpr
    monkeypatch.setattr(diagnose_cvpr, 'ROOT', tmp_path)
    calls = []
    monkeypatch.setattr(diagnose_cvpr, 'load_dotenv', lambda path: calls.append(path))
    monkeypatch.setattr(diagnose_cvpr, 'search_cvpr_page', lambda *a, **k: (_ for _ in ()).throw(requests.ConnectionError('private-secret')))
    assert diagnose_cvpr.main(['--query', 'depth', '--limit', '1']) == 2
    assert calls == [tmp_path/'.env']
    assert 'private-secret' not in capsys.readouterr().out
    report = (tmp_path/'outputs'/'cvpr_search_diagnostic.json').read_text()
    assert 'private-secret' not in report and json.loads(report)['code'] == 'VENUE_CONNECTION_ERROR'
