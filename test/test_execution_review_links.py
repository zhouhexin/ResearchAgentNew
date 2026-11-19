from copy import deepcopy
import json
import pytest
from api import coverage, execution_review_links as links, tasks
from storage.db_models import PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_library_execution import library, BASE
from test.test_api_coverage import ready


@pytest.fixture
def execution(ready):
    client,state,Session,url=ready
    client.app.include_router(links.router)
    run=client.get(BASE+'/executions').json()['items'][0]
    return client,state,Session,run,BASE+'/executions/'+run['id']


def launch(execution):
    client,_,_,run,url=execution
    return post(client,url+'/review-versions',{'analysis_id':run['papers'][0]['analysis']['analysis_id']})


def test_explicit_review_pinned_context_and_link_to_coverage_and_report(execution,monkeypatch):
    client,state,_,run,url=execution
    before=len(state['requests']);original=deepcopy(run)
    versions=client.get(url+'/review-versions')
    assert versions.status_code==200,versions.text
    assert versions.json()['items'][0]['claim_count']==1 and len(state['requests'])==before
    defaults_b(monkeypatch)
    response,review_task=launch(execution)
    assert review_task['status']=='succeeded',review_task
    assert review_task['parent_task_id']==run['task_id'] and review_task['execution_id']==run['id']
    actual,schema,_=state['requests'][-1]
    assert schema=='ReferenceClaimReview' and json.loads(actual.content)['model']=='model-A'
    assert actual.url.host=='a.example' and actual.extensions['timeout']['read']==17
    assert json.loads(actual.content)['max_completion_tokens']==2000
    request,checked=post(client,url+'/review-links',{'review_ids':[response['review_id']], 'research_questions':['新增问题是否已有依据？']})
    assert checked['status']=='succeeded',checked
    result=client.get('/api/v1/coverage/'+checked['result_id']).json()
    assert result['review_ids']==[response['review_id']] and result['is_current']
    assert result['report']['questions'][0]['question']=='新增问题是否已有依据？'
    payload=state['requests'][-1][2]
    assert payload['auxiliary_reviews'][0]['review_id']==response['review_id']
    assert payload['auxiliary_reviews'][1]['status']=='not_reviewed'
    assert coverage.source_run('library-plan',run['id'])==original
    linked=coverage.source_run('library-plan',request['execution_id'])
    assert linked['review_selection_id']==request['review_selection_id'] and not linked['review_after_analysis']
    assert linked['papers'][0]['review']['review_id']==response['review_id']
    before=len(state['requests'])
    same,same_task=post(client,url+'/review-links',{'review_ids':[response['review_id']], 'research_questions':['新增问题是否已有依据？']})
    assert same['execution_id']==request['execution_id'] and same_task['id']==checked['id']
    assert len(state['requests'])==before
    _,report=post(client,BASE+'/executions/'+request['execution_id']+'/reports',{'research_question':'方法如何比较？','comparison_dimensions':['方法']})
    assert report['status']=='succeeded',report
    final=client.get('/api/v1/reviews/'+report['result_id']).json()
    assert final['research_context']['auxiliary_reviews'][0]['review_id']==response['review_id']
    trace=client.get('/api/v1/tasks/'+checked['id']+'/trace').json()
    assert any(a['id']==response['review_id'] and a['relation']=='reused' for a in trace['artifacts'])


def test_failed_review_resumes_without_reanalysis(execution,monkeypatch):
    client,state,_,run,url=execution
    state['fail_schema']='ReferenceClaimReview'
    response,failed=launch(execution)
    assert failed['status']=='failed'
    rejected=client.post(url+'/review-links',json={'review_ids':[response['review_id']]})
    assert rejected.status_code==422
    before=sum(schema=='PaperAnalysis' for _,schema,_ in state['requests'])
    state['fail_schema']=None;defaults_b(monkeypatch)
    _,success=post(client,url+'/review-versions',{'analysis_id':run['papers'][0]['analysis']['analysis_id'],'resume_review_id':response['review_id']})
    assert success['status']=='succeeded',success
    assert success['resumed_from_task_id']==failed['id']
    assert sum(schema=='PaperAnalysis' for _,schema,_ in state['requests'])==before
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'


def test_foreign_duplicate_and_stale_inputs_rejected(execution):
    client,state,Session,run,url=execution
    assert client.post(url+'/review-versions',json={'analysis_id':'foreign-analysis'}).status_code==422
    response,_=launch(execution)
    assert client.post(url+'/review-links',json={'review_ids':[response['review_id']]*2}).status_code==422
    before=len(state['requests'])
    with Session.begin() as session:session.get(PaperDocumentRecord,10).content_sha256='changed'
    assert client.post(url+'/review-links',json={'review_ids':[response['review_id']]}).status_code in {409,422}
    assert len(state['requests'])==before


def test_nonsupported_verdict_can_be_linked_without_relabeling(execution):
    client,state,_,run,url=execution
    state['verdict']='contradicted'
    response,review=launch(execution)
    assert review['status']=='succeeded'
    adopted,checked=post(client,url+'/review-links',{'review_ids':[response['review_id']]})
    assert checked['status']=='succeeded',checked
    linked=coverage.source_run('library-plan',adopted['execution_id'])
    assert linked['papers'][0]['review']['verdict_counts']['contradicted']==1
    assert state['requests'][-1][2]['auxiliary_reviews'][0]['items'][0]['review']['verdict']=='contradicted'


def test_legacy_execution_cannot_capture_new_defaults(execution):
    client,state,_,run,url=execution
    with tasks.connection() as db:
        row=db.execute('SELECT payload FROM research_executions WHERE id=?',(run['id'],)).fetchone()
        saved=json.loads(row[0]);saved.pop('runtime_bindings')
        db.execute('UPDATE research_executions SET payload=? WHERE id=?',(json.dumps(saved),run['id']))
    before=len(state['requests'])
    assert client.get(url+'/review-versions').status_code==409
    assert len(state['requests'])==before


def test_two_versions_of_same_analysis_are_not_both_linked(execution,monkeypatch):
    client,state,_,run,url=execution
    first,_=launch(execution)
    defaults_b(monkeypatch)
    other,task=post(client,'/api/v1/analyses/'+run['papers'][0]['analysis']['analysis_id']+'/claim-reviews?new_run=true',{})
    assert task['status']=='succeeded',task
    before=len(state['requests'])
    response=client.post(url+'/review-links',json={'review_ids':[first['review_id'],other['review_id']]})
    assert response.status_code==422 and len(state['requests'])==before


def test_failed_coverage_reuses_selection_and_original_binding(execution,monkeypatch):
    client,state,_,_,url=execution
    review,_=launch(execution)
    state['fail_schema']='CoverageDraft'
    first,failed=post(client,url+'/review-links',{'review_ids':[review['review_id']]})
    assert failed['status']=='failed'
    defaults_b(monkeypatch);state['fail_schema']=None
    again,success=post(client,url+'/review-links',{'review_ids':[review['review_id']]})
    assert success['status']=='succeeded',success
    assert first['execution_id']==again['execution_id'] and first['review_selection_id']==again['review_selection_id']
    assert success['resumed_from_task_id']==failed['id']
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'


def test_changed_linked_review_cannot_enter_new_coverage_or_report(execution,monkeypatch):
    from api import claim_reviews
    client,state,_,_,url=execution
    review,_=launch(execution)
    selected,checked=post(client,url+'/review-links',{'review_ids':[review['review_id']]})
    assert checked['status']=='succeeded'
    original=claim_reviews.get_review
    def changed(identifier):
        result=deepcopy(original(identifier))
        if identifier==review['review_id']:result['items'][0]['statement']='changed checkpoint'
        return result
    monkeypatch.setattr(claim_reviews,'get_review',changed)
    before=len(state['requests'])
    assert client.get('/api/v1/coverage/'+checked['result_id']).json()['is_current'] is False
    branch_url=BASE+'/executions/'+selected['execution_id']
    response=client.post(branch_url+'/reports',json={'research_question':'问题','comparison_dimensions':['方法']})
    assert response.status_code==409,response.text
    response=client.post(branch_url+'/coverage',json={})
    assert response.status_code==409,response.text
    assert len(state['requests'])==before


def test_saved_report_becomes_historical_when_linked_review_changes(execution,monkeypatch):
    from api import claim_reviews
    client,state,_,_,url=execution
    review,_=launch(execution)
    branch,_=post(client,url+'/review-links',{'review_ids':[review['review_id']]})
    report_url=BASE+'/executions/'+branch['execution_id']+'/reports'
    _,task=post(client,report_url,{'research_question':'方法比较','comparison_dimensions':['方法']})
    assert task['status']=='succeeded',task
    saved_url='/api/v1/reviews/'+task['result_id']
    assert client.get(saved_url).json()['review_links_current'] is True
    original=claim_reviews.get_review
    def changed(identifier):
        result=deepcopy(original(identifier))
        if identifier==review['review_id']:result['items'][0]['statement']='modified after publication'
        return result
    monkeypatch.setattr(claim_reviews,'get_review',changed)
    result=client.get(saved_url)
    assert result.status_code==200 and result.json()['is_current'] is False
    assert result.json()['review_links_current'] is False
    assert client.get(saved_url+'/files/md').status_code==200
