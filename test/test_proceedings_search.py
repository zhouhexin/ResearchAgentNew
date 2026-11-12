"""受控 AAAI/IJCAI 页面和索引响应；不代表真实网站联网验收。"""
from copy import deepcopy
import json

import pytest
import requests
from sqlalchemy import select

from tools.venue_search import cvpr
from tools.venue_search.proceedings import official_location, verify_proceedings, find_proceedings_pdf_urls
from tools.open_access_pdf import FulltextError
from storage.venue_models import VenueRecord
from test.test_cvpr_search import sources, api_client, work, candidate, PAGE
from test.test_api_documents import client as database_client
from test.test_research_search import setup as plan_setup, start, edit

AAAI = 'https://ojs.aaai.org/index.php/AAAI/article/view/34950'
IJCAI = 'https://ijcai.org/proceedings/2025/896'
DOIS = {'aaai':'10.1609/aaai.v39i26.34950','ijcai':'10.24963/ijcai.2025/896'}
URLS = {'aaai':AAAI,'ijcai':IJCAI}


def aaai_page(issue='AAAI-25 Technical Tracks 1'):
    return f'''<meta name="citation_title" content="Depth Estimation with Geometric Vision">
<meta name="citation_author" content="Alice Wang"><meta name="citation_author" content="Bob Li">
<meta name="citation_doi" content="{DOIS['aaai']}"><meta name="citation_publication_date" content="2025/04/11">
<meta name="citation_pdf_url" content="{AAAI}/32710">
<a href="/index.php/AAAI/issue/view/624">{issue}</a><h2>Section</h2><p>AAAI Technical Track on AI Alignment</p>
<div class="abstract">We evaluate <b>depth estimation</b> using geometry.</div>'''


def ijcai_page(track='Main Track', key='ijcai2025p896'):
    return '''<h1>Depth Estimation with Geometric Vision</h1><a href="0896.pdf">PDF</a>
<h2>Abstract</h2><p>We evaluate <b>depth estimation</b> using geometry.</p><h2>References</h2>
<p>A reference must not become the abstract.</p><h2>BibTeX</h2><pre>'''+f'''@inproceedings{{{key},
 author = {{Wang, Alice and Li, Bob}}, title = {{Depth Estimation with Geometric Vision}},
 year = {{2025}}, note = {{{track}}}, doi = {{{DOIS['ijcai']}}},
 booktitle = {{Proceedings of the Thirty-Fourth International Joint Conference on Artificial Intelligence}}
}}</pre>'''


def paper(slug, **changes):
    return candidate(year=2025,url=URLS[slug],doi=DOIS[slug],**changes)


@pytest.mark.parametrize('slug',['aaai','ijcai'])
def test_official_identity_abstract_and_published_pdf(slug):
    html=aaai_page() if slug=='aaai' else ijcai_page()
    fetch=lambda *a,**k:html
    result=verify_proceedings(paper(slug),slug,fetch)
    assert result['verification_status']=='official_verified' and result['track']=='main'
    assert result['abstract']=='We evaluate depth estimation using geometry.'
    assert result['verification_provider']==slug.upper()+' Proceedings'
    expected=AAAI.replace('/view/','/download/')+'/32710' if slug=='aaai' else 'https://ijcai.org/proceedings/2025/0896.pdf'
    assert result['pdf_url']==expected
    p=paper(slug)
    assert find_proceedings_pdf_urls(p['title'],p['authors'],p['doi'],p['url'],fetch)==[expected]


@pytest.mark.parametrize('issue,track', [('AAAI-25 Special Track on AI Alignment','other'),
    ('AAAI-25 Demonstration Track','other'),('AAAI-25 Doctoral Consortium','other'),
    ('AAAI Workshops','workshop'),('AAAI-25 Unclassified Papers','unknown')])
def test_aaai_issue_has_priority_over_technical_section(issue,track):
    assert verify_proceedings(paper('aaai'),'aaai',lambda *a,**k:aaai_page(issue))['track']==track


@pytest.mark.parametrize('label', ['Survey Track','Demonstrations','Sister Conferences Best Papers','AI and Social Good'])
def test_ijcai_other_track_never_becomes_main(label):
    result=verify_proceedings(paper('ijcai'),'ijcai',lambda *a,**k:ijcai_page(label))
    assert result['verification_status']=='official_verified' and result['track']=='other'


@pytest.mark.parametrize('slug',['aaai','ijcai'])
@pytest.mark.parametrize('change',[{'title':'Wrong paper'},{'authors':['Other Author']},{'year':2024},{'doi':'10.1234/wrong'}])
def test_identity_mismatch_stays_unverified(slug,change):
    p=paper(slug);p.update(change)
    result=verify_proceedings(p,slug,lambda *a,**k:aaai_page() if slug=='aaai' else ijcai_page())
    assert result['verification_status']=='indexed_unverified'


def test_ijcai_references_and_unknown_index_cannot_supply_identity_or_main_track():
    assert verify_proceedings(paper('ijcai'),'ijcai',lambda *a,**k:ijcai_page(key='ijcai2025p123'))['verification_status']=='indexed_unverified'
    def get(url,**k):
        if url==IJCAI:return ijcai_page('Unclassified')
        return '<h2>Survey Track</h2><a href="0896">Depth Estimation with Geometric Vision</a>'
    assert verify_proceedings(paper('ijcai'),'ijcai',get)['track']=='other'


@pytest.mark.parametrize('url',[AAAI.replace('/AAAI/','/AIES/'),AAAI.replace('ojs.aaai.org','evil.test'),
    'https://ojs.aaai.org:bad/index.php/AAAI/article/view/34950',IJCAI.replace('/896','/bibtex/896')])
def test_untrusted_or_other_proceedings_paths_rejected(url):
    assert official_location(url) is None


def test_no_guessed_pdf_and_no_unverified_download():
    fetch=lambda *a,**k:ijcai_page().replace('<a href="0896.pdf">PDF</a>','')
    assert verify_proceedings(paper('ijcai'),'ijcai',fetch)['pdf_url'] is None
    with pytest.raises(FulltextError,match='PDF'):
        find_proceedings_pdf_urls('Wrong paper',['Alice Wang'],DOIS['ijcai'],IJCAI,fetch)


@pytest.fixture
def publisher_sources(sources,monkeypatch):
    for slug in ('aaai','ijcai'):monkeypatch.delenv(f'OPENALEX_{slug.upper()}_SOURCE_IDS',raising=False)
    state={'calls':[],'blocked':False,'works':[
        work(3,doi=DOIS['aaai'],publication_year=2025,primary_location={'source':{'id':'https://openalex.org/S789'},'landing_page_url':AAAI}),
        work(4,doi=DOIS['ijcai'],publication_year=2025,primary_location={'source':{'id':'https://openalex.org/S987'},'landing_page_url':IJCAI})]}
    def get(url,params=None,**kwargs):
        state['calls'].append((url,deepcopy(params)))
        if url.endswith('/sources'):
            aaai='aaai' in params['search'];name='Proceedings of the AAAI Conference on Artificial Intelligence' if aaai else 'International Joint Conference on Artificial Intelligence'
            return json.dumps({'results':[{'id':'https://openalex.org/'+('S789' if aaai else 'S987'),'display_name':name,'type':'journal' if aaai else 'conference'},
                {'id':'https://openalex.org/S999','display_name':name+' Workshops','type':'conference'},
                {'id':'https://openalex.org/S998','display_name':'AAAI Spring Symposium','type':'journal'},
                {'id':'https://openalex.org/S997','display_name':'Proceedings of the AAAI Conference on Artificial Intelligence and Interactive Digital Entertainment','type':'journal'}]})
        if url.endswith('/works'):return json.dumps({'meta':{'count':len(state['works'])},'results':state['works']})
        if state['blocked']:raise requests.ConnectionError('private-upstream-detail')
        return aaai_page() if url==AAAI else ijcai_page()
    monkeypatch.setattr(cvpr,'cached_get',get)
    return state


def test_combined_publisher_query_uses_verified_sources_and_keeps_official_locations(publisher_sources):
    value=publisher_sources['works'][0]
    value['locations']=[{'landing_page_url':AAAI}]
    value['primary_location']['landing_page_url']='https://doi.org/'+DOIS['aaai']
    result=cvpr.search_venue_page('depth',venue_slugs=['aaai','ijcai'])
    assert {p['venue_slug'] for p in result['items']}=={'aaai','ijcai'}
    assert result['items'][0]['official_urls']==[AAAI]
    assert all(p['publication']['track']=='main' for p in result['items'])
    assert {p['abstract_source'] for p in result['items']}=={'aaai_proceedings','ijcai_proceedings'}
    params=next(params for url,params in publisher_sources['calls'] if url.endswith('/works'))
    assert 'S789|S987' in params['filter'] and all(s not in params['filter'] for s in ('S999','S998','S997'))


def test_official_outage_retains_candidates_and_main_only_filter_is_strict(publisher_sources):
    publisher_sources['blocked']=True
    result=cvpr.search_venue_page('depth',venue_slugs=['aaai','ijcai'])
    assert len(result['items'])==2 and all(p['publication']['verification_status']=='indexed_unverified' for p in result['items'])
    assert 'private-upstream-detail' not in json.dumps(result)
    assert cvpr.search_venue_page('depth',venue_slugs=['aaai','ijcai'],verified_main_only=True)['items']==[]


def test_four_venues_share_one_global_candidate_query(publisher_sources,monkeypatch):
    from test.test_venue_search import ICCV_MAIN
    ids={'aaai':'S789','ijcai':'S987','cvpr':'S123','iccv':'S456'}
    monkeypatch.setattr(cvpr,'resolve_sources',lambda slug='cvpr':[{'id':ids[slug],'name':slug.upper()}])
    publisher_sources['works'].extend([work(),work(2,publication_year=2025,
        primary_location={'source':{'id':'https://openalex.org/S456'},'landing_page_url':ICCV_MAIN})])
    get=cvpr.cached_get
    monkeypatch.setattr(cvpr,'cached_get',lambda url,*a,**k:PAGE if 'openaccess.thecvf.com' in url else get(url,*a,**k))
    result=cvpr.search_venue_page('depth',venue_slugs=list(ids))
    assert {p['venue_slug'] for p in result['items']}==set(ids)
    assert all(p['publication']['verification_status']=='official_verified' for p in result['items'])
    calls=[params for url,params in publisher_sources['calls'] if url.endswith('/works')]
    assert len(calls)==1 and all(v in calls[0]['filter'] for v in ids.values())
    assert result['total']==4


@pytest.mark.parametrize('url',[AAAI,IJCAI])
def test_official_access_block_pages_are_not_cached(monkeypatch,tmp_path,url):
    import sqlite3
    monkeypatch.setattr(cvpr,'CACHE_PATH',tmp_path/'cache.sqlite3')
    monkeypatch.setattr(cvpr.time,'sleep',lambda *a:None)
    class Blocked:
        is_redirect=False
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def raise_for_status(self):pass
        def iter_content(self,*a):yield b'<html><h1>Access Denied</h1>BotStopper verification required</html>'
    monkeypatch.setattr(cvpr.requests,'get',lambda *a,**k:Blocked())
    with pytest.raises(FulltextError,match='未缓存'):cvpr.cached_get(url)
    with sqlite3.connect(cvpr.CACHE_PATH) as db:assert db.execute('SELECT count(*) FROM responses').fetchone()[0]==0


def test_collection_stores_actual_publisher_and_provider(api_client,publisher_sources):
    client,Session=api_client
    result=client.get('/api/v1/venues/papers',params={'query':'depth','venues':'aaai,ijcai'})
    assert result.status_code==200,result.text
    items=result.json()['items']
    saved=client.post('/api/v1/papers/batch',json={'papers':items})
    assert saved.status_code==200 and saved.json()['created']==2,saved.text
    for item,row in zip(items,saved.json()['results']):
        pub=client.get(f"/api/v1/papers/{row['paper_id']}/publication").json()['items'][0]
        assert pub['venue']['slug']==item['venue_slug']
        assert pub['verification_status']=='official_verified'
        assert pub['verification_provider']==item['venue_slug'].upper()+' Proceedings'
    with Session() as session:assert {v.slug for v in session.scalars(select(VenueRecord))}=={'aaai','ijcai'}


def test_plan_executes_publisher_search_and_screens_original_abstract(plan_setup,publisher_sources):
    client,state=plan_setup
    edit(client,lambda p:p['scope'].update(venue_slug=None,venue_slugs=['aaai','ijcai'],requested_sources=['openalex','aaai_proceedings','ijcai_proceedings'],verified_main_only=True))
    _,task,run=start(client,revision=2)
    assert task['status']=='succeeded',task
    assert {p['venue_slug'] for p in run['candidates']}=={'aaai','ijcai'}
    assert all(s['decision']=='included' for s in run['screenings'].values())
