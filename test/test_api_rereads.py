import json
from hashlib import sha256
import pytest
from api import coverage, rereads, tasks
from api.analysis_results import load_result
from storage import reread_budget as budget
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_library_execution import library, BASE, launch, current


@pytest.fixture
def ready(library):
    client,state,Session=library
    client.app.include_router(coverage.router)
    client.app.include_router(rereads.router)
    # 基线分析只读首尾；E101 是真实未读片段，覆盖检查后应能选择补读。
    with Session.begin() as session:
        doc=session.get(PaperDocumentRecord,10)
        content='First method.\nRuntime experiments.\nFinal limitation.'
        doc.content=content;doc.content_sha256=sha256(content.encode()).hexdigest()
        session.query(DocumentChunkRecord).filter(DocumentChunkRecord.document_id==10).delete()
        offset=0
        for i,text in enumerate(content.splitlines()):
            session.add(DocumentChunkRecord(id=i+100,document_id=10,chunk_index=i,text=text,
                start_char=offset,end_char=offset+len(text),page_number=1))
            offset+=len(text)+1
    _,one=post(client,'/api/v1/analyses',{'paper_id':1,'scope':'sample','max_chunks':2})
    _,two=post(client,'/api/v1/analyses',{'paper_id':2})
    assert one['status']==two['status']=='succeeded'
    run,task=launch(client,papers=[{'paper_id':1,'analysis_id':one['result_id']},{'paper_id':2,'analysis_id':two['result_id']}])
    assert task['status']=='succeeded',task
    _,checked=post(client,BASE+'/executions/'+run['execution_id']+'/coverage',{})
    assert checked['status']=='succeeded',checked
    return client,state,Session,checked['result_id'],one['result_id'],run['execution_id']


def preview(client,result_id):
    response=client.post('/api/v1/coverage/'+result_id+'/reread-preview',json={
        'question_id':'Q1','suggestion_index':0,'paper_id':1,'locator':'runtime'})
    assert response.status_code==200,response.text
    return response.json()


def start(client,value,**kwargs):
    return post(client,'/api/v1/rereads',{'preview_id':value['id'],'evidence_ids':['E101'],
                                          'max_calls':4,'total_seconds':60,**kwargs})


def test_preview_confirm_fixed_model_new_analysis_and_explicit_recheck(ready,monkeypatch):
    client,state,_,result_id,base_id,execution_id=ready
    original=load_result(base_id)
    before=len(state['requests'])
    value=preview(client,result_id)
    assert len(state['requests'])==before
    e2=next(c for c in value['candidates'] if c['evidence_id']=='E101')
    assert e2['already_read'] is False and e2['text']=='Runtime experiments.'
    defaults_b(monkeypatch)
    request,task=start(client,value,total_seconds=10)
    assert task['status']=='succeeded',task
    actual,schema,payload=state['requests'][-1]
    assert schema=='PaperAnalysis'
    body=json.loads(actual.content)
    assert body['model']=='model-A' and actual.url.host=='a.example'
    assert 0<actual.extensions['timeout']['read']<=10
    assert body['max_completion_tokens']==4321
    assert payload['research_focus']['question_id']=='Q1' and [e['evidence_id'] for e in payload['evidence']]==['E101']
    new=load_result(task['result_id'])
    assert new['id']!=base_id and new['base_analysis_id']==base_id
    assert new['selected_evidence_ids']==['E100','E101','E102']
    assert new['batch_summary']['scope']=='supplemental'
    assert len(new['analysis']['research_questions'])==2
    assert new['budget']['calls_used']==1 and new['budget']['tokens'] is None
    call=new['budget']['calls'][0]
    from storage.runtime_versions import load_immutable
    from api import provenance
    runtime=provenance.transaction(lambda session:load_immutable(session,call['runtime_snapshot_id']))
    assert runtime.read_timeout==call['timeout'] and runtime.retries==0
    assert load_result(base_id)==original
    assert coverage.read(result_id)['analysis_ids'][0]==base_id
    before=len(state['requests'])
    duplicate,_=start(client,value,total_seconds=10)
    assert duplicate['task_id']==task['id'] and len(state['requests'])==before
    response,rechecked=post(client,'/api/v1/rereads/'+task['id']+'/recheck',{})
    assert rechecked['status']=='succeeded',rechecked
    assert response['execution_id']!=execution_id
    saved=client.get('/api/v1/coverage/'+rechecked['result_id']).json()
    assert saved['analysis_ids'][0]==new['id'] and saved['is_current']
    original_run=coverage.source_run('library-plan',execution_id)
    assert original_run['papers'][0]['analysis']['analysis_id']==base_id
    assert client.post('/api/v1/coverage/'+saved['id']+'/reread-preview',json={
        'question_id':'Q1','suggestion_index':0,'paper_id':1}).status_code==422
    trace=client.get('/api/v1/tasks/'+task['id']+'/trace').json()
    assert any(t['id']==task['id'] and t['parent_task_id'] for t in trace['tasks'])


def test_foreign_duplicate_evidence_and_unapproved_paper_blocked(ready):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id);before=len(state['requests'])
    for ids in (['E999'],['E101','E101'],['E4']):
        response=client.post('/api/v1/rereads',json={'preview_id':value['id'],'evidence_ids':ids})
        assert response.status_code==422,response.text
    assert client.post('/api/v1/coverage/'+result_id+'/reread-preview',json={
        'question_id':'Q1','suggestion_index':0,'paper_id':2}).status_code==422
    assert len(state['requests'])==before


def test_failed_request_resume_keeps_budget_and_original_parameters(ready,monkeypatch):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id)
    state['fail_schema']='PaperAnalysis'
    request,first=start(client,value,max_calls=1)
    assert first['status']=='failed' and budget.summary(request['request_id'])['calls_used']==1
    state['fail_schema']=None;defaults_b(monkeypatch)
    before=len(state['requests'])
    _,second=post(client,'/api/v1/rereads/'+request['request_id']+'/resume',{})
    assert second['status']=='failed' and second['error']['code']=='REREAD_BUDGET_EXHAUSTED'
    assert len(state['requests'])==before and second['resumed_from_task_id']==first['id']


def test_source_changes_between_preview_and_confirm_are_rejected(ready):
    client,state,Session,result_id,*_=ready
    value=preview(client,result_id);before=len(state['requests'])
    with Session.begin() as session: session.get(DocumentChunkRecord,101).text='tampered'
    response=client.post('/api/v1/rereads',json={'preview_id':value['id'],'evidence_ids':['E101']})
    assert response.status_code==409,response.text
    assert len(state['requests'])==before


def test_cached_completed_batches_recover_without_new_calls(ready,monkeypatch):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id)
    original=rereads.complete_analysis
    monkeypatch.setattr(rereads,'complete_analysis',lambda *a,**k: (_ for _ in ()).throw(OSError('controlled save interruption')))
    request,first=start(client,value,max_calls=1)
    assert first['status']=='failed'
    before=len(state['requests'])
    monkeypatch.setattr(rereads,'complete_analysis',original)
    _,second=post(client,'/api/v1/rereads/'+request['request_id']+'/resume',{})
    assert second['status']=='succeeded',second
    assert len(state['requests'])==before and budget.summary(request['request_id'])['calls_used']==1


def test_resume_uses_original_model_and_does_not_repeat_successful_batches(ready,monkeypatch):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id)
    state['fail_schema']='PaperAnalysis'
    request,first=start(client,value,max_calls=2)
    assert first['status']=='failed'
    defaults_b(monkeypatch);state['fail_schema']=None
    _,second=post(client,'/api/v1/rereads/'+request['request_id']+'/resume',{})
    assert second['status']=='succeeded',second
    actual,_,payload=state['requests'][-1]
    assert json.loads(actual.content)['model']=='model-A' and actual.url.host=='a.example'
    assert [e['evidence_id'] for e in payload['evidence']]==['E101']
    assert budget.summary(request['request_id'])['calls_used']==2


def test_late_response_is_rejected_and_expired_resume_never_calls_model(ready):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id)
    def expire(schema):
        if schema=='PaperAnalysis':
            with tasks.connection() as db: db.execute('UPDATE reread_budgets SET deadline=0')
    state['hook']=expire
    request,failed=start(client,value,max_calls=2)
    assert failed['status']=='failed' and failed['result_id'] is None
    assert failed['error']['code']=='REREAD_DEADLINE_EXCEEDED'
    state['hook']=None;before=len(state['requests'])
    _,again=post(client,'/api/v1/rereads/'+request['request_id']+'/resume',{})
    assert again['status']=='failed' and again['error']['code']=='REREAD_DEADLINE_EXCEEDED'
    assert len(state['requests'])==before


def test_newer_document_does_not_replace_original_fixed_document(ready):
    client,state,Session,result_id,*_=ready
    with Session.begin() as session:
        text='A newer unrelated document.'
        session.add(PaperDocumentRecord(id=999,paper_id=1,source_url='file:///new.pdf',source_type='pdf',
            parser='pymupdf',page_count=1,content=text,content_sha256=sha256(text.encode()).hexdigest()))
        session.flush()
        session.add(DocumentChunkRecord(id=777,document_id=999,chunk_index=0,text=text,
            start_char=0,end_char=len(text),page_number=1))
    value=preview(client,result_id)
    assert value['document_id']==10 and all(c['evidence_id']!='E777' for c in value['candidates'])
    _,task=start(client,value)
    assert task['status']=='succeeded',task
    assert state['requests'][-1][2]['evidence'][0]['evidence_id']=='E101'


def test_worker_redelivery_never_overwrites_completed_task(ready):
    client,state,_,result_id,*_=ready
    value=preview(client,result_id)
    _,task=start(client,value)
    assert task['status']=='succeeded',task
    before=len(state['requests'])
    rereads.worker(task['id'])
    assert tasks.get_task(task['id'])==task and len(state['requests'])==before


def test_reread_from_review_linked_execution_drops_stale_review_selection(ready):
    from api import execution_review_links as links
    client,state,_,result_id,_,execution_id=ready
    client.app.include_router(links.router)
    base=BASE+'/executions/'+execution_id
    source=coverage.source_run('library-plan',execution_id)
    review,_=post(client,base+'/review-versions',{'analysis_id':source['papers'][0]['analysis']['analysis_id']})
    branch,checked=post(client,base+'/review-links',{'review_ids':[review['review_id']]})
    assert checked['status']=='succeeded',checked
    value=preview(client,checked['result_id'])
    _,read_task=start(client,value)
    assert read_task['status']=='succeeded',read_task
    adopted,task=post(client,'/api/v1/rereads/'+read_task['id']+'/recheck',{})
    assert task['status']=='succeeded',task
    run=coverage.source_run('library-plan',adopted['execution_id'])
    assert not run.get('review_selection_id') and run['selection_kind']=='reread'
