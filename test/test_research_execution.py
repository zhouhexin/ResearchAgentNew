"""计划闭环的版本、真实任务关联和恢复；下载/模型以受控子任务代替。"""
from copy import deepcopy
import json
import pytest

from api import research_execution as execution, tasks
from test.test_research_search import setup as search_setup, start, edit
from test.test_api_analyses import client as analysis_client, model
from test.test_api_documents import client as database_client
from test.test_api_uploads import pdf_bytes

BASE='/api/v1/agent/plans/plan_test/executions'


@pytest.fixture
def setup(search_setup,monkeypatch):
    client,_=search_setup
    client.app.include_router(execution.router)
    _,_,search=start(client)
    state={'search':search,'docs':{},'analyses':{},'downloads':[],'calls':[],'fail':set(),'samples':set(),'saved':[]}
    def save(body):
        p=body.papers[0];state['saved'].append(p.doi)
        pid=int(p.doi.rsplit('.',1)[-1])
        return {'results':[{'paper_id':pid,'status':'existing' if pid in state['docs'] else 'created'}]}
    monkeypatch.setattr(execution.papers,'save_papers_batch',save)
    monkeypatch.setattr(execution.papers,'list_paper_documents',lambda pid:deepcopy(state['docs'].get(pid,[])))
    monkeypatch.setattr(execution.analyses,'read_analyses',lambda load_paper_id:deepcopy(state['analyses'].get(load_paper_id,[])))
    def download(pid,jobs):
        t=tasks.create_task(pid,dedup_key='fetch_fulltext',deduplicate=True)
        def perform():
            state['downloads'].append(pid)
            state['docs'][pid]=[{'id':pid*10,'chunk_count':3}]
            tasks.update_task(t['id'],status='succeeded',result_id=str(pid*10))
        if not t.get('_reused'):jobs.add_task(perform)
        return {'task_id':t['id'],'reused':bool(t.get('_reused'))}
    def analyze(body,jobs):
        assert body.scope=='full' and not body.force_restart and not body.resume_task_id
        pid=body.paper_id;t=tasks.create_task(pid,task_type='paper_analysis',deduplicate=True)
        def perform():
            state['calls'].append(pid)
            if pid in state['fail']:
                tasks.update_task(t['id'],status='failed',error={'code':'MODEL_TIMEOUT','message':'模型超时，已完成批次保留','retryable':True});return
            result={'id':f'analysis_{pid}','paper':{'id':pid},'document':{'id':pid*10,'selected_chunks':3,'total_chunks':3},'is_current':True,'batch_summary':{'scope':'full'}}
            state['analyses'][pid]=[result]
            tasks.update_task(t['id'],status='succeeded',result_id=result['id'])
        if not t.get('_reused'):jobs.add_task(perform)
        return {'task_id':t['id'],'reused':bool(t.get('_reused'))}
    monkeypatch.setattr(execution.fulltext,'fetch_fulltext',download)
    monkeypatch.setattr(execution.analyses,'start_analysis',analyze)
    return client,state


def launch(client,state,ids=None,revision=1):
    body={'expected_revision':revision,'search_run_id':state['search']['id'],
          'candidate_ids':ids or [p['candidate_id'] for p in state['search']['candidates'][:2]]}
    return client.post(BASE,json=body)


def current(client):return client.get(BASE).json()['items'][0]


def test_two_papers_complete_and_link_actual_artifacts(setup):
    client,state=setup;r=launch(client,state)
    assert r.status_code==202,r.text
    run=current(client)
    assert run['status']=='succeeded' and run['revision']==1 and run['plan']==state['search']['plan']
    assert state['downloads']==state['calls']==[1,2]
    assert [p['paper_id'] for p in run['papers']]==[1,2]
    for p in run['papers']:
        assert p['fulltext']['document_id']==10*p['paper_id']
        assert p['analysis']['analysis_id']==f"analysis_{p['paper_id']}"
        assert p['analysis']['task']['status']=='succeeded' and p['fulltext']['task']['status']=='succeeded'
    parent=tasks.get_task(r.json()['task_id'])
    assert parent['type']=='research_execution' and parent['plan_revision']==1 and parent['progress']==100
    again=launch(client,state).json()
    assert again['reused'] and again['execution_id']==run['id'] and state['calls']==[1,2]


def test_partial_failure_retains_other_papers_and_retries_only_missing_analysis(setup):
    client,state=setup;state['fail']={1}
    launch(client,state);run=current(client)
    assert run['status']=='failed' and run['papers'][0]['analysis']['error']['code']=='MODEL_TIMEOUT'
    assert run['papers'][1]['analysis']['status']=='succeeded' and state['calls']==[1,2]
    old_child=run['papers'][0]['analysis']['task_id'];state['fail'].clear()
    response=client.post(BASE+'/'+run['id']+'/resume')
    assert response.status_code==202,response.text
    resumed=current(client)
    assert resumed['id']==run['id'] and resumed['status']=='succeeded'
    assert state['downloads']==[1,2] and state['calls']==[1,2,1]
    assert resumed['papers'][0]['analysis']['task_id']!=old_child
    assert resumed['papers'][1]['analysis']['analysis_id']==run['papers'][1]['analysis']['analysis_id']


def test_reuses_valid_full_analysis_but_rejects_sample_and_stale(setup):
    client,state=setup
    for pid in (1,2):
        state['docs'][pid]=[{'id':pid*10,'chunk_count':3}]
        state['analyses'][pid]=[{'id':f'old_{pid}','is_current':True,'paper':{'id':pid},'document':{'id':pid*10,'selected_chunks':3,'total_chunks':3},'batch_summary':{'scope':'full'}}]
    state['analyses'][1][0]['batch_summary']['scope']='sample'
    launch(client,state)
    run=current(client)
    assert state['calls']==[1] and not state['downloads']
    assert run['papers'][1]['analysis']['reused'] and run['papers'][1]['analysis']['analysis_id']=='old_2'


@pytest.mark.parametrize('ids',[[],['x','y'],['arxiv:2401.00001']*2,['arxiv:2401.00001']])
def test_invalid_selection_no_work(setup,ids):
    client,state=setup
    r=client.post(BASE,json={'expected_revision':1,'search_run_id':state['search']['id'],'candidate_ids':ids})
    assert r.status_code==422 and not state['saved'] and not state['calls']


def test_versions_history_and_ownership(setup):
    client,state=setup;state['fail']={1};launch(client,state);old=current(client)
    edit(client,lambda p:p.update(topic='新主题'))
    assert launch(client,state).status_code==409
    assert launch(client,state,revision=2).status_code==422
    state['fail'].clear()
    assert client.post(BASE+'/'+old['id']+'/resume').status_code==202
    assert current(client)['revision']==1 and current(client)['plan']['topic']!= '新主题'
    assert client.post('/api/v1/agent/plans/other/executions/'+old['id']+'/resume').status_code==404
    assert client.get(BASE,params={'search_run_id':'other'}).json()['items']==[]


def test_queued_execution_dedup_and_restart_recovery(setup,monkeypatch):
    client,state=setup;worker=execution.run_execution
    monkeypatch.setattr(execution,'run_execution',lambda run:None)
    first=launch(client,state).json();again=launch(client,state).json()
    assert first['task_id']==again['task_id'] and again['reused']
    ids=[p['candidate_id'] for p in state['search']['candidates'][1:]]
    assert launch(client,state,ids=ids).status_code==409
    tasks.recover_interrupted_tasks()
    assert current(client)['status']=='failed'
    monkeypatch.setattr(execution,'run_execution',worker)
    response=client.post(BASE+'/'+first['execution_id']+'/resume')
    assert response.json()['task_id']!=first['task_id'] and current(client)['status']=='succeeded'


def test_incomplete_plan_does_not_execute(setup):
    client,state=setup
    with tasks.connection() as db:
        row=db.execute('SELECT payload FROM research_search_runs WHERE id=?',(state['search']['id'],)).fetchone()
        search=json.loads(row[0]);search['plan']['steps']=[s for s in search['plan']['steps'] if s['action']!='acquire_fulltext']
        db.execute('UPDATE research_search_runs SET payload=? WHERE id=?',(json.dumps(search),search['id']))
    assert launch(client,state).status_code==422 and not state['calls']


@pytest.mark.parametrize('bad_quote',[False,True])
@pytest.mark.parametrize('download',[False,True])
@pytest.mark.parametrize('scope',['full','fast'])
def test_real_analysis_child_and_evidence_validation(analysis_client,model,monkeypatch,bad_quote,download,pdf_bytes,tmp_path,scope):
    from test.test_research_planning import proposal,GOAL
    from api import research_plans,research_search
    client=analysis_client;client.app.include_router(execution.router)
    plan=proposal()
    selected=(1,2) if download else (1,3)
    if download:
        from api import documents,uploads
        from storage import document_repository
        from storage.db_models import PaperRecord
        monkeypatch.setattr(execution.fulltext,'make_db',documents.make_db)
        monkeypatch.setattr(document_repository,'make_db',documents.make_db)
        monkeypatch.setattr(uploads,'UPLOAD_ROOT',tmp_path/'uploads')
        monkeypatch.setenv('RESEARCHPILOT_FULLTEXT_SOURCES_FILE',str(tmp_path/'sources.json'))
        _,Session=documents.make_db('sqlite://')
        with Session.begin() as session:session.get(PaperRecord,2).doi='10.1234/2'
        monkeypatch.setattr(execution.fulltext,'find_open_pdf_urls',lambda **k:['https://example.org/paper.pdf'])
        def fetch_pdf(url,destination,**kwargs):
            destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(pdf_bytes);return url
        monkeypatch.setattr(execution.fulltext,'download_open_pdf',fetch_pdf)
    candidates=[dict(candidate_id=str(i),title='测试论文'+str(i),authors=[],doi='10.1234/'+str(i),source='arxiv',identity_verified=True) for i in selected]
    search=dict(id='search_real',plan_id='plan_real',revision=1,plan=plan,goal=GOAL,task_id='search_task',status='succeeded',error=None,candidates=candidates,screenings={str(i):{'decision':'included'} for i in selected})
    with tasks.connection() as db:
        execution.initialize(db)
        db.execute('INSERT INTO research_plans VALUES (?,?)',('plan_real',json.dumps(dict(revision=1,plan=plan))))
        db.execute('INSERT INTO research_search_runs VALUES (?,?,?,?,?)',('search_real','plan_real',1,'key',json.dumps(search)))
    monkeypatch.setattr(execution.papers,'save_papers_batch',lambda body:{'results':[{'status':'existing','paper_id':int(body.papers[0].doi.rsplit('/',1)[-1])}]})
    model['mode']='bad_quote' if bad_quote else 'valid'
    response=client.post('/api/v1/agent/plans/plan_real/executions',json={'expected_revision':1,'search_run_id':'search_real','candidate_ids':[str(i) for i in selected],'analysis_scope':scope})
    assert response.status_code==202,response.text
    run=client.get('/api/v1/agent/plans/plan_real/executions').json()['items'][0]
    assert run['status']==('failed' if bad_quote else 'succeeded'),run
    if bad_quote:
        assert all(p['analysis']['error']['code']=='EVIDENCE_VALIDATION_FAILED' for p in run['papers'])
        assert not execution.analyses.read_analyses()
        model['mode']='valid'
        client.post('/api/v1/agent/plans/plan_real/executions/'+run['id']+'/resume')
        run=client.get('/api/v1/agent/plans/plan_real/executions').json()['items'][0]
        assert run['status']=='succeeded',run
    for p in run['papers']:
        result=execution.analyses.get_analysis(p['analysis']['analysis_id'])
        assert result['is_current'] and result['batch_summary']['scope']==scope
        assert result['paper']['id']==p['paper_id'] and result['document']['id']==p['fulltext']['document_id']


def test_active_child_is_not_executed_twice_and_can_be_linked_after_completion(setup):
    client,state=setup
    child=tasks.create_task(1,task_type='paper_analysis',deduplicate=True)
    launch(client,state)
    run=current(client)
    assert run['status']=='failed' and state['calls']==[2]
    assert run['papers'][0]['analysis']['task_id']==child['id']
    assert run['papers'][0]['analysis']['error']['code']=='CHILD_TASK_RUNNING'
    state['analyses'][1]=[{'id':'external-analysis','is_current':True,'paper':{'id':1},'document':{'id':10,'selected_chunks':3,'total_chunks':3},'batch_summary':{'scope':'full'}}]
    tasks.update_task(child['id'],status='succeeded',result_id='external-analysis')
    client.post(BASE+'/'+run['id']+'/resume')
    assert current(client)['status']=='succeeded' and state['calls']==[2]


def test_stale_analysis_is_not_reused_and_deleted_document_does_not_keep_success(setup,monkeypatch):
    from fastapi import HTTPException
    client,state=setup;state['fail']={2};launch(client,state);run=current(client)
    state['analyses'][1][0]['is_current']=False
    state['docs'].pop(1)
    def fail(pid,jobs):raise HTTPException(422,detail='请上传新的全文')
    monkeypatch.setattr(execution.fulltext,'fetch_fulltext',fail)
    state['fail'].clear();client.post(BASE+'/'+run['id']+'/resume')
    result=current(client)
    assert result['status']=='failed'
    assert result['papers'][0]['fulltext']['status']=='failed' and result['papers'][0]['analysis']['status']=='pending'
    assert result['papers'][1]['analysis']['status']=='succeeded'



def test_fast_analysis_never_satisfies_full_execution(setup):
    from agent.fast_reading import VERSION
    client,state=setup
    for pid in (1,2):
        state['docs'][pid]=[{'id':pid*10,'chunk_count':3}]
        state['analyses'][pid]=[{'id':f'fast_{pid}','is_current':True,'paper':{'id':pid},'document':{'id':pid*10,'selected_chunks':2,'total_chunks':3},'batch_summary':{'scope':'fast'},'reading_selection':{'policy_version':VERSION,'budget':18}}]
    body={'expected_revision':1,'search_run_id':state['search']['id'],'candidate_ids':[p['candidate_id'] for p in state['search']['candidates'][:2]],'analysis_scope':'fast'}
    assert client.post(BASE,json=body).status_code==202
    fast=current(client)
    assert fast['status']=='succeeded' and not state['calls']
    assert all(p['analysis']['scope']=='fast' for p in fast['papers'])
    response=launch(client,state)
    assert response.status_code==202 and current(client)['id']!=fast['id']
    assert state['calls']==[1,2]
