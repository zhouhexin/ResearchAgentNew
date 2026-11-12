"""新增九个场所的受控协议测试，不替代真实联网验收。"""
import json
from copy import deepcopy
import pytest
import requests

from tools.venue_search import cvpr, extended as ext
from tools.venue_search.catalog import VENUES, SOURCE_QUERIES, JOURNAL_ISSNS, VENUE_PLATFORMS, supported_sources
from test.test_cvpr_search import api_client, sources, work
from test.test_api_documents import client as database_client
from test.test_research_search import setup as plan_setup, start, edit

TITLE='Depth estimation with geometry'
URLS={
 'acl':'https://aclanthology.org/2024.acl-long.1/',
 'emnlp':'https://aclanthology.org/2024.emnlp-main.1/',
 'icml':'https://proceedings.mlr.press/v235/han24b.html',
 'eccv':'https://www.ecva.net/papers/eccv_2024/papers_ECCV/html/521_ECCV_2024_paper.php',
 'neurips':'https://papers.nips.cc/paper_files/paper/2024/hash/'+32*'a'+'-Abstract-Conference.html',
 'iclr':'https://openreview.net/forum?id=Abcdef1234',
 'sigir':'https://doi.org/10.1145/3626772.3657980',
 'tois':'https://doi.org/10.1145/1234567',
 'artificial-intelligence':'https://doi.org/10.1016/j.artint.2024.104000'}


def paper(slug,**changes):
    p=dict(title=TITLE,authors=['Alice Wang','Bob Li'],year=2024,doi=None,url=URLS[slug])
    if slug in {'sigir','tois','artificial-intelligence'}:p['doi']=p['url'].split('doi.org/')[1]
    p.update(changes);return p


def html(slug):
    return f'''<meta name="citation_title" content="{TITLE}"><meta name="citation_author" content="Alice Wang">
    <meta name="citation_author" content="Bob Li"><meta name="citation_publication_date" content="2024/07/01">
    <meta name="citation_conference_title" content="Proceedings of the International Conference on Machine Learning">
    <h2>Abstract</h2><p>We evaluate depth estimation using geometry.</p><h2>Related Material</h2>'''


def crossref(slug):
    r=dict(DOI=paper(slug)['doi'],title=[TITLE],author=[dict(given='Alice',family='Wang'),dict(given='Bob',family='Li')],published={'date-parts':[[2024]]})
    if slug in JOURNAL_ISSNS:r.update(type='journal-article',ISSN=list(JOURNAL_ISSNS[slug]),**{'container-title':[VENUES[slug]['name']]})
    else:r.update(type='proceedings-article',**{'container-title':['Proceedings of the 47th International ACM SIGIR Conference on Research and Development in Information Retrieval']})
    return {'status':'ok','message':r}


def openreview(venue='ICLR 2024 poster',venueid='ICLR.cc/2024/Conference'):
    content=dict(title=TITLE,authors=['Alice Wang','Bob Li'],abstract='We evaluate depth estimation using geometry.',venue=venue,venueid=venueid,pdf='/pdf/abc.pdf')
    return {'notes':[dict(id='Abcdef1234',forum='Abcdef1234',content={k:{'value':v} for k,v in content.items()})]}


def fetch(url,params=None,**kwargs):
    if 'crossref' in url:
        return json.dumps(crossref(next(s for s in ('sigir','tois','artificial-intelligence') if paper(s)['doi'].replace('/','%2F') in url)))
    if 'openreview' in url:return json.dumps(openreview())
    return html('icml')


@pytest.mark.parametrize('slug',sorted(ext.SLUGS))
def test_all_new_venues_verify_with_matching_formal_records(slug):
    r=ext.verify_extended(paper(slug),slug,fetch)
    assert r['verification_status']=='official_verified',r
    assert r['track']==('journal' if slug in JOURNAL_ISSNS else 'main')
    assert '参考' not in r.get('abstract','')


@pytest.mark.parametrize('slug',sorted(ext.SLUGS))
@pytest.mark.parametrize('change',[{'title':'Different paper'},{'authors':['Other Author']},{'year':2023},{'authors':[]}])
def test_wrong_identity_never_upgrades(slug,change):
    assert ext.verify_extended(paper(slug,**change),slug,fetch)['verification_status']=='indexed_unverified'


@pytest.mark.parametrize('url,track',[(URLS['acl'].replace('acl-long','findings-acl'),'findings'),
 (URLS['emnlp'].replace('emnlp-main','emnlp-demo'),'other'),
 (URLS['neurips'].replace('Conference','Datasets_and_Benchmarks_Track'),'other'),
 (URLS['eccv'].replace('papers_ECCV/','papers_ECCVW/'),'workshop')])
def test_official_secondary_tracks_stay_distinct(url,track):
    loc=ext.locate(url);slug=next(iter(loc['slugs']))
    assert ext.verify_extended(paper(slug,url=url),slug,fetch)['track']==track


def test_pmlr_requires_specific_icml_volume_not_generic_series():
    r=ext.verify_extended(paper('icml'),'icml',lambda *a,**k:html('icml').replace('International Conference on Machine Learning','International Conference on Artificial Intelligence and Statistics'))
    assert r['verification_status']=='indexed_unverified'


@pytest.mark.parametrize('venue,venueid',[('ICLR 2024 Submitted','ICLR.cc/2024/Conference/Submission'),
 ('ICLR 2024 Rejected','ICLR.cc/2024/Conference'),('ICLR 2024 Withdrawn','ICLR.cc/2024/Conference'),
 ('ICLR 2025 poster','ICLR.cc/2025/Conference'),('ICLR 2024 poster','ICLR.cc/2024/Workshop'),
 ('ICML 2024 poster','ICML.cc/2024/Conference')])
def test_openreview_submission_wrong_year_and_other_venues_do_not_pass(venue,venueid):
    r=ext.verify_extended(paper('iclr'),'iclr',lambda *a,**k:json.dumps(openreview(venue,venueid)))
    assert r['verification_status']=='indexed_unverified'


@pytest.mark.parametrize('slug',['artificial-intelligence','tois'])
@pytest.mark.parametrize('field,value',[('ISSN',['0000-0000']),('container-title',['Engineering Applications of Artificial Intelligence']),('type','posted-content'),('DOI','10.1234/wrong')])
def test_journal_requires_exact_name_issn_type_and_doi(slug,field,value):
    data=crossref(slug);data['message'][field]=value
    assert ext.verify_extended(paper(slug),slug,lambda *a,**k:json.dumps(data))['verification_status']=='indexed_unverified'


def test_pmlr_published_pdf_links_only_and_bibtex_identity():
    url='https://raw.githubusercontent.com/mlresearch/v235/main/assets/han24b/han24b.pdf'
    page=html('icml')+f'<a href="{url}">Download PDF</a>'
    r=ext.verify_extended(paper('icml'),'icml',lambda *a,**k:page)
    assert r['pdf_url']==url
    assert ext.find_extended_pdf_urls(TITLE,paper('icml')['authors'],None,URLS['icml'],lambda *a,**k:page)==[url]
    assert ext.verify_extended(paper('icml'),'icml',lambda *a,**k:page.replace('han24b.pdf','other.pdf'))['pdf_url'] is None
    assert ext.own_bib_fields('@inproceedings{pmlr-v235-other,title={Wrong}}','pmlr-v235-han24b')=={}


@pytest.mark.parametrize('slug',sorted(ext.SLUGS))
def test_outages_keep_pending_without_leaking_exception(slug):
    def fail(*a,**k):raise requests.ConnectionError('private-secret')
    r=ext.verify_extended(paper(slug),slug,fail)
    assert r['verification_status']=='indexed_unverified' and 'private-secret' not in str(r)


@pytest.fixture
def extended_sources(sources,monkeypatch):
    ids={s:'S'+str(200+i) for i,s in enumerate(sorted(ext.SLUGS))}
    for slug in ids:monkeypatch.delenv('OPENALEX_'+VENUES[slug]['acronym'].upper()+'_SOURCE_IDS',raising=False)
    state={'calls':[],'ids':ids,'works':[]}
    for i,slug in enumerate(ids):
        p=paper(slug);state['works'].append(work(30+i,title=TITLE,publication_year=2024,doi=p['doi'],
            primary_location={'source':{'id':'https://openalex.org/'+ids[slug]},'landing_page_url':p['url']}))
    def get(url,params=None,**kwargs):
        state['calls'].append((url,deepcopy(params)))
        if url.endswith('/sources'):
            slug=next(s for s in ids if SOURCE_QUERIES[s]==params['search'])
            return json.dumps({'results':[dict(id='https://openalex.org/'+ids[slug],display_name=VENUES[slug]['name'],type='journal' if slug in JOURNAL_ISSNS else 'conference',issn=list(JOURNAL_ISSNS.get(slug,[]))),
              dict(id='https://openalex.org/S999',display_name=VENUES[slug]['name']+' Workshops',type='conference')]})
        if url.endswith('/works'):return json.dumps({'meta':{'count':len(state['works'])},'results':state['works']})
        return fetch(url,params,**kwargs)
    monkeypatch.setattr(cvpr,'cached_get',get)
    return state


def test_combined_search_main_and_journal_filter_and_collection(api_client,extended_sources):
    client,_=api_client
    slugs=sorted(ext.SLUGS)
    response=client.get('/api/v1/venues/papers',params={'venues':','.join(slugs),'query':'depth','verified_main_only':True})
    assert response.status_code==200,response.text
    items=response.json()['items'];assert len(items)==9
    assert response.json()['publication_counts']['journal']==2
    queries=[p for u,p in extended_sources['calls'] if u.endswith('/works')]
    assert len(queries)==1 and all(v in queries[0]['filter'] for v in extended_sources['ids'].values())
    assert 'S999' not in queries[0]['filter']
    for item in items:
        saved=client.post('/api/v1/papers/batch',json={'papers':[item]})
        assert saved.status_code==200,saved.text
        pub=client.get('/api/v1/papers/'+str(saved.json()['results'][0]['paper_id'])+'/publication').json()['items']
        assert any(p['venue']['slug']==item['venue_slug'] and p['verification_status']=='official_verified' for p in pub)


def test_new_scope_can_execute_and_screen(plan_setup,extended_sources):
    client,_=plan_setup
    edit(client,lambda p:p['scope'].update(venue_slug=None,venue_slugs=sorted(ext.SLUGS),requested_sources=['openalex'],verified_main_only=True))
    _,task,run=start(client,revision=2)
    assert task['status']=='succeeded',task
    assert len(run['candidates'])==9


def test_catalog_and_schema_accept_all_13_and_full_platform_union():
    from agent.research_planner import ResearchScope
    from typing import get_args
    from tools.venue_search.catalog import VenueSlug
    from test.test_research_planning import proposal
    assert len(VENUES)==13 and set(get_args(VenueSlug))==set(VENUES)
    scope=proposal()['scope'];scope.update(venue_slug=None,venue_slugs=list(VENUES),requested_sources=list({p for s in VENUES for p in VENUE_PLATFORMS[s]}))
    assert len(ResearchScope.model_validate(scope).venue_slugs)==13


@pytest.mark.parametrize('slug',sorted(ext.SLUGS))
def test_source_mapping_rejects_similar_but_different_venue(extended_sources,monkeypatch,slug):
    if slug in JOURNAL_ISSNS:
        name=VENUES[slug]['name'];kind='journal';issn=['0000-0000']
    else:
        name=VENUES[slug]['name']+' Workshops';kind='conference';issn=[]
    monkeypatch.setattr(cvpr,'get_json',lambda *a,**k:{'results':[dict(id='https://openalex.org/S999',display_name=name,type=kind,issn=issn)]})
    from tools.open_access_pdf import FulltextError
    with pytest.raises(FulltextError):cvpr.resolve_sources(slug)
