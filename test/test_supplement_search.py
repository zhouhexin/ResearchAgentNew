import json
from copy import deepcopy
import pytest
from api import coverage, supplement_search as supplement, tasks
from storage import reread_budget as budget
from storage.db_models import PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_library_execution import library, BASE, launch


@pytest.fixture
def ready(library,monkeypatch):
    client,state,Session=library
    client.app.include_router(coverage.router);client.app.include_router(supplement.router)
    response,task=launch(client);assert task['status']=='succeeded'
    state['coverage_response']={'questions':[{'question_id':'Q1','status':'unanswered','answer':'','reason':'本轮缺少对照材料',
        'gaps':['缺少实验对照'],'finding_ids':[],'citations':[],'suggestions':[
            {'action':'search_later','paper_ids':[],'target':'机器人视觉能耗对照','rationale':'已有材料不足'}]}],'limitations':['仅本轮材料']}
    _,checked=post(client,BASE+'/executions/'+response['execution_id']+'/coverage',{})
    assert checked['status']=='succeeded',checked
    fetched=[]
    def fetch(query,**kwargs):
        fetched.append((query,kwargs))
        return {'total':1,'items':[{'source':'arxiv','arxiv_id':'2610.00001','title':'Robot vision comparison',
            'abstract':'Robot vision energy benchmark.','identity_verified':True,'year':2024,'authors':[],
            'doi':'10.48550/arxiv.2610.00001','url':'https://arxiv.org/abs/2610.00001'}]}
    monkeypatch.setattr(supplement,'search_arxiv_page',fetch)
    return client,state,Session,checked['result_id'],fetched


def launch_search(ready,**kwargs):
    client,_,_,identifier,_=ready
    return post(client,'/api/v1/coverage/'+identifier+'/supplement-search',{
        'question_id':'Q1','suggestion_index':0,'queries':['robot energy'],'max_calls':4,'total_seconds':60,**kwargs})


def test_confirmed_search_pins_actual_model_and_scope_and_keeps_original(ready,monkeypatch):
    client,state,_,identifier,fetched=ready
    original=deepcopy(coverage.read(identifier));defaults_b(monkeypatch)
    response,task=launch_search(ready)
    assert task['status']=='succeeded',task
    assert len(fetched)==1 and fetched[0][1]['page_size']==5 and fetched[0][1]['start_year']==2020
    actual,schema,payload=state['requests'][-1]
    assert schema=='ScreeningBatch' and json.loads(actual.content)['model']=='model-A' and actual.url.host=='a.example'
    assert payload['research_questions']==[original['report']['questions'][0]['question']]
    saved=client.get('/api/v1/supplement-search/'+response['request_id']).json()
    assert saved['result'] and len(saved['run']['candidates'])==1 and len(saved['run']['screenings'])==1
    assert saved['budget']['calls_used']==2 and saved['run']['stop_reason']=='confirmed_queries_screened'
    assert saved['request']['budget']['automatic_downloads']==0 and not state['fetches']
    assert coverage.read(identifier)==original
    before=len(state['requests']);again,_=launch_search(ready)
    assert again['task_id']==task['id'] and len(state['requests'])==before and len(fetched)==1
    # 不把派生的问题/查询伪装为原计划的普通检索版本。
    assert client.get(BASE+'/search').json()['run'] is None


def test_failure_resume_reuses_query_results_but_not_budget(ready,monkeypatch):
    client,state,_,_,fetched=ready
    state['fail_schema']='ScreeningBatch'
    response,task=launch_search(ready,max_calls=3)
    assert task['status']=='failed' and budget.summary(response['request_id'])['calls_used']==2
    state['fail_schema']=None;defaults_b(monkeypatch)
    _,resumed=post(client,'/api/v1/supplement-search/'+response['request_id']+'/resume',{})
    assert resumed['status']=='succeeded',resumed
    assert len(fetched)==1 and budget.summary(response['request_id'])['calls_used']==3
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'


def test_exhaustion_and_deadline_do_not_dispatch_more_calls(ready):
    client,state,_,_,fetched=ready
    state['fail_schema']='ScreeningBatch'
    response,task=launch_search(ready,max_calls=2)
    assert task['status']=='failed'
    before=len(state['requests'])
    _,resumed=post(client,'/api/v1/supplement-search/'+response['request_id']+'/resume',{})
    assert resumed['error']['code']=='SUPPLEMENT_BUDGET_EXHAUSTED' and len(state['requests'])==before
    with tasks.connection() as db:db.execute('UPDATE reread_budgets SET deadline=0 WHERE id=?',(response['request_id'],))
    _,resumed=post(client,'/api/v1/supplement-search/'+response['request_id']+'/resume',{})
    assert resumed['error']['code']=='SUPPLEMENT_DEADLINE_EXCEEDED' and len(fetched)==1


def test_changed_sources_or_invalid_scope_block_without_dispatch(ready):
    client,state,Session,identifier,fetched=ready
    url='/api/v1/coverage/'+identifier+'/supplement-search'
    body={'question_id':'Q1','suggestion_index':0,'queries':['robot']}
    for change in ({'question_id':'Q999'},{'queries':['robot','robot']},{'source':'openalex'}):
        assert client.post(url,json=body|change).status_code==422
    with Session.begin() as session:session.get(PaperDocumentRecord,10).content_sha256='changed'
    assert client.post(url,json=body).status_code==409
    assert fetched==[]


def test_saved_result_recovery_does_not_repeat_queries_or_models(ready,monkeypatch):
    client,state,_,_,fetched=ready
    original=tasks.update_task
    def interrupt(task_id,**changes):
        if changes.get('status')=='succeeded':raise OSError('controlled status write failure')
        return original(task_id,**changes)
    monkeypatch.setattr(tasks,'update_task',interrupt)
    response,first=launch_search(ready)
    assert first['status']=='failed'
    monkeypatch.setattr(tasks,'update_task',original)
    before=len(state['requests'])
    _,second=post(client,'/api/v1/supplement-search/'+response['request_id']+'/resume',{})
    assert second['status']=='succeeded',second
    assert len(state['requests'])==before and len(fetched)==1


def test_arxiv_deadline_checked_after_rate_limit_before_http(monkeypatch):
    import tools.arxiv_pdf as arxiv
    import requests
    monkeypatch.setattr(arxiv,'_last_query',0)
    monkeypatch.setattr(arxiv.requests,'get',lambda *a,**k:pytest.fail('expired request must not reach HTTP'))
    with pytest.raises(requests.Timeout):arxiv.query_arxiv({},deadline=0)


@pytest.mark.parametrize('constraint',[{'venue_slugs':['cvpr']},{'verified_main_only':True},{'requested_sources':['openalex']}])
def test_source_restrictions_are_not_silently_broadened(constraint):
    from fastapi import HTTPException
    scope={'requested_sources':[],'venue_slugs':[],'venue_slug':None,'verified_main_only':False}|constraint
    original=deepcopy(scope)
    with pytest.raises(HTTPException) as caught:supplement.check_scope(scope)
    assert caught.value.status_code==422 and scope==original


def test_late_tool_response_is_not_saved_as_completed_query(ready,monkeypatch):
    client,state,_,_,_=ready
    original=supplement.search_arxiv_page
    def late(*args,**kwargs):
        result=original(*args,**kwargs)
        with tasks.connection() as db:db.execute('UPDATE reread_budgets SET deadline=0')
        return result
    monkeypatch.setattr(supplement,'search_arxiv_page',late)
    before=len(state['requests'])
    response,task=launch_search(ready)
    assert task['status']=='failed' and task['error']['code']=='SUPPLEMENT_DEADLINE_EXCEEDED'
    saved=client.get('/api/v1/supplement-search/'+response['request_id']).json()
    assert saved['run']['queries'][0]['status']=='pending' and not saved['result']
    assert len(state['requests'])==before


def test_arxiv_actual_http_timeout_respects_remaining_deadline(monkeypatch):
    import tools.arxiv_pdf as arxiv
    monkeypatch.setattr(arxiv,'_last_query',0)
    monkeypatch.setattr(arxiv.time,'time',lambda:100)
    received=[]
    class Response:
        content=b'controlled'
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def raise_for_status(self):pass
    def get(url,**kwargs):received.append(kwargs);return Response()
    monkeypatch.setattr(arxiv.requests,'get',get)
    assert arxiv.query_arxiv({},deadline=102)==b'controlled'
    assert received[0]['timeout']==(2,2)
