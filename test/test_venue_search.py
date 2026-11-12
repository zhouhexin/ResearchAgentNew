"""ICCV 与同页多会议检索；受控来源响应，不代表真实联网通过。"""
import json
from copy import deepcopy
import pytest
import requests
from sqlalchemy import select

from tools.venue_search import cvpr
from tools.venue_search.catalog import selected_venues, VENUES
from tools.open_access_pdf import FulltextError
from storage.venue_models import VenueRecord, PaperPublicationRecord
from api import venues
from test.test_cvpr_search import sources, api_client, work, PAGE, TITLE, MAIN, candidate
from test.test_research_search import setup as plan_setup, start, edit
from test.test_api_documents import client as database_client

ICCV_MAIN = 'https://openaccess.thecvf.com/content/ICCV2025/html/Wang_Depth_ICCV_2025_paper.html'
ICCV_TITLE = 'International Depth Estimation'


def iccv_work(**kwargs):
    return work(2, url=ICCV_MAIN, title=ICCV_TITLE, doi='https://doi.org/10.1234/iccv-depth', publication_year=2025,
                primary_location={'source': {'id': 'https://openalex.org/S456'}, 'landing_page_url': ICCV_MAIN}, **kwargs)


@pytest.fixture
def multi_sources(sources, monkeypatch):
    monkeypatch.delenv('OPENALEX_ICCV_SOURCE_IDS', raising=False)
    state = {'calls': [], 'works': [work(), iccv_work()], 'fail_iccv': False, 'collide': False, 'index_override': None}
    def get(url, params=None, **kwargs):
        state['calls'].append((url, deepcopy(params)))
        if url.endswith('/sources'):
            is_iccv = 'international' in params['search']
            if is_iccv and state['fail_iccv']:
                raise requests.Timeout('private-api-key')
            slug, source_id = ('iccv', 'S123' if state['collide'] else 'S456') if is_iccv else ('cvpr', 'S123')
            return json.dumps({'results': [
                {'id': 'https://openalex.org/'+source_id, 'display_name': slug.upper(), 'type': 'conference'},
                {'id': 'https://openalex.org/S999', 'display_name': slug.upper()+' Workshops', 'type': 'conference'}]})
        if url.endswith('/works'):
            items = [w for w in state['works'] if w['primary_location']['source']['id'].rsplit('/',1)[-1] in params['filter']]
            if params.get('sort') == 'publication_date:desc':
                items = sorted(items, key=lambda w: w['publication_year'], reverse=True)
            offset = (params['page']-1)*params['per_page']
            return json.dumps({'meta': {'count': len(items)}, 'results': items[offset:offset+params['per_page']]})
        if '?day=all' in url:
            if state['index_override']:
                return state['index_override']
            href, title = (ICCV_MAIN, ICCV_TITLE) if 'ICCV' in url else (MAIN, TITLE)
            return f'<dt class="ptitle"><a href="{href}">{title}</a></dt>'
        return PAGE.replace(TITLE, ICCV_TITLE).replace('10.1234/depth', '10.1234/iccv-depth') if 'ICCV' in url else PAGE
    monkeypatch.setattr(cvpr, 'cached_get', get)
    return state


@pytest.mark.parametrize('url,track', [(ICCV_MAIN,'main'),
    (ICCV_MAIN.replace('ICCV2025/html', 'ICCV2025W/3DVision/html'),'workshop'),
    (ICCV_MAIN.replace('ICCV2025/html', 'ICCV2025W/Findings/html'),'findings'),
    ('https://openaccess.thecvf.com/content_iccv_2015/html/Wang_Depth_ICCV_2015_paper.html','main'),
    ('https://openaccess.thecvf.com/content_ICCV_2019_workshops/w1/html/Wang_Depth_paper.html','workshop')])
def test_iccv_paths_are_distinct_and_old_cvpr_helper_is_compatible(url, track):
    assert cvpr.cvf_publication_location(url)[0] == 'iccv'
    assert cvpr.cvf_publication_location(url)[2] == track
    assert cvpr.cvf_location(url) is None


def test_iccv_official_identity_and_pdf(multi_sources):
    paper = candidate(title=ICCV_TITLE, year=2025, doi='10.1234/iccv-depth', url=ICCV_MAIN)
    result = cvpr.verify_publication(paper, 'iccv')
    assert result['verification_status'] == 'official_verified' and result['track'] == 'main'
    assert result['verification_url'] == ICCV_MAIN
    assert cvpr.find_cvf_pdf_urls(ICCV_TITLE,paper['authors'],paper['doi'],ICCV_MAIN)[0].endswith('.pdf')
    wrong = cvpr.verify_publication(paper, 'cvpr')
    assert wrong['verification_status'] == 'indexed_unverified'


def test_iccv_cannot_be_verified_by_cvpr_directory(multi_sources):
    multi_sources['index_override'] = f'<a href="{MAIN}">{ICCV_TITLE}</a>'
    value = cvpr.verify_publication(candidate(title=ICCV_TITLE, year=2025, url='https://doi.org/10.1234/iccv-depth'), 'iccv')
    assert value['verification_status'] == 'indexed_unverified'
    assert not any(url == MAIN for url,_ in multi_sources['calls'])


def test_one_combined_query_has_global_pagination_and_correct_venue_labels(multi_sources):
    result = cvpr.search_venue_page('depth',venue_slugs=['iccv','cvpr'],page_size=1,sort='newest')
    assert result['total'] == 2 and result['items'][0]['venue_slug'] == 'iccv'
    assert result['items'][0]['publication']['track'] == 'main'
    calls = [params for url,params in multi_sources['calls'] if url.endswith('/works')]
    assert len(calls) == 1 and 'S123|S456' in calls[0]['filter']
    assert 'S999' not in calls[0]['filter']
    second = cvpr.search_venue_page('depth',venue_slugs=['cvpr','iccv'],page_size=1,page=2,sort='newest')
    assert second['items'][0]['venue_slug'] == 'cvpr'


def test_same_venue_duplicates_removed_without_combining_publication_venues(multi_sources):
    multi_sources['works'].append(work(3))
    result = cvpr.search_venue_page('depth',venue_slugs=['cvpr','iccv'])
    assert len(result['items']) == 2 and result['duplicate_count'] == 1
    assert {item['venue_slug'] for item in result['items']} == {'cvpr','iccv'}


def test_multi_plan_keeps_different_publication_records_even_for_same_identifier():
    from agent.research_search import merge_candidates
    first = {'identity_verified':True,'title':TITLE,'source':'openalex','openalex_id':'https://openalex.org/W1',
             'published_doi':'10.1234/shared','venue_slug':'cvpr'}
    second = dict(first,venue_slug='iccv')
    results,_ = merge_candidates([{'number':1,'items':[first,second]}],separate_publications=True)
    assert len(results)==2 and len({item['candidate_id'] for item in results})==2


def test_main_filter_removes_iccv_workshop_and_findings(multi_sources):
    for track in ('OtherWorkshop','Findings'):
        multi_sources['works'] = [iccv_work()]
        multi_sources['works'][0]['primary_location']['landing_page_url'] = ICCV_MAIN.replace('ICCV2025/html','ICCV2025W/'+track+'/html')
        result = cvpr.search_venue_page('depth',venue_slugs=['iccv'],verified_main_only=True)
        assert result['items'] == [] and result['total'] == 1
        assert result['publication_counts']['findings' if track=='Findings' else 'workshop'] == 1


def test_selected_source_failures_are_not_silently_ignored(multi_sources):
    multi_sources['fail_iccv'] = True
    with pytest.raises(requests.Timeout):
        cvpr.search_venue_page('depth',venue_slugs=['cvpr','iccv'])
    assert not any(url.endswith('/works') for url,_ in multi_sources['calls'])


def test_ambiguous_source_id_mapping_rejected(multi_sources):
    multi_sources['collide'] = True
    with pytest.raises(FulltextError,match='不同会议'):
        cvpr.search_venue_page('depth',venue_slugs=['cvpr','iccv'])


@pytest.mark.parametrize('selected', [[],['unknown'],['cvpr','cvpr'],['cvpr','iccv','unknown']])
def test_bad_selection_never_queries_sources(multi_sources, selected):
    with pytest.raises(ValueError):
        cvpr.search_venue_page('depth',venue_slugs=selected)
    assert multi_sources['calls'] == []


def test_catalog_combined_api_and_iccv_collection(api_client, multi_sources):
    client,Session = api_client
    assert {v['slug'] for v in client.get('/api/v1/venues').json()['items']} == set(VENUES)
    response = client.get('/api/v1/venues/papers',params={'venues':'cvpr,iccv','query':'depth'})
    assert response.status_code == 200,response.text
    items = response.json()['items'];assert len(items) == 2
    single = client.get('/api/v1/venues/iccv/papers',params={'query':'depth'})
    assert single.status_code == 200 and single.json()['items'][0]['venue_slug'] == 'iccv'
    assert {p['venue_slug'] for p in items} == {'cvpr','iccv'}
    saved = client.post('/api/v1/papers/batch',json={'papers':items})
    assert saved.status_code == 200,saved.text
    assert saved.json()['created'] == 2
    for item,row in zip(items,saved.json()['results']):
        publication = client.get(f"/api/v1/papers/{row['paper_id']}/publication").json()['items'][0]
        assert publication['venue']['slug'] == item['venue_slug']
        assert publication['verification_status'] == 'official_verified'
    with Session() as session:
        assert {v.slug for v in session.scalars(select(VenueRecord))} == {'cvpr','iccv'}
        assert len(session.scalars(select(PaperPublicationRecord)).all()) == 2


def test_api_validates_multi_selection_and_source_error(api_client, multi_sources):
    client,_ = api_client
    for selection in ('','unsupported','cvpr,cvpr','cvpr,unknown'):
        assert client.get('/api/v1/venues/papers',params={'venues':selection,'query':'depth'}).status_code == 422
    multi_sources['fail_iccv'] = True
    result = client.get('/api/v1/venues/papers',params={'venues':'cvpr,iccv','query':'depth'})
    assert result.status_code == 504 and 'private-api-key' not in result.text


@pytest.mark.parametrize('scope,expected', [({'venue_slug':'iccv'},['iccv']),
    ({'venue_slug':None,'venue_slugs':['cvpr','iccv']},['cvpr','iccv'])])
def test_plan_uses_the_selected_venues_and_isolates_checkpoints(plan_setup, multi_sources, scope, expected):
    client,state = plan_setup
    edit(client,lambda p:p['scope'].update(**scope,requested_sources=['openalex','cvf'],verified_main_only=True))
    _,task,run = start(client,revision=2)
    assert task['status'] == 'succeeded',task
    assert selected_venues(run) == expected
    assert {item['venue_slug'] for item in run['candidates']} == set(expected)
    assert all(v['decision']=='included' for v in run['screenings'].values())
    first_id = run['id'];edit(client,lambda p:p['scope'].update(venue_slug='cvpr',venue_slugs=[]))
    _,_,next_run = start(client,revision=3)
    assert next_run['id'] != first_id and {item['venue_slug'] for item in next_run['candidates']} == {'cvpr'}
