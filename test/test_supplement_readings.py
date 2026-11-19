from copy import deepcopy
import json
import pytest
from api import coverage, supplement_readings as readings, tasks
from api.analysis_results import load_result
from storage.db_models import PaperRecord, PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_library_execution import library, BASE
from test.test_supplement_search import ready, launch_search


@pytest.fixture
def completed(ready):
    client,state,Session,coverage_id,_=ready
    client.app.include_router(readings.router)
    request,task=launch_search(ready)
    assert task['status']=='succeeded',task
    with Session.begin() as session:
        paper=session.get(PaperRecord,3);paper.title='Robot vision comparison'
        paper.doi='10.48550/arxiv.2610.00001';paper.url='https://arxiv.org/abs/2610.00001'
    _,analysis=post(client,'/api/v1/analyses',{'paper_id':3})
    assert analysis['status']=='succeeded',analysis
    state['coverage_response']=None
    return client,state,Session,request['request_id'],coverage_id,analysis['result_id']


def body(analysis_id):return {'readings':[{'candidate_id':'arxiv:2610.00001','analysis_id':analysis_id}],'reason':'补充能耗比较的实验依据，仍需核对口径。'}


def test_choices_do_not_call_model_and_adoption_pins_versions_preserves_history(completed,monkeypatch):
    client,state,_,request_id,coverage_id,analysis_id=completed
    original=deepcopy(coverage.read(coverage_id));before=len(state['requests'])
    url='/api/v1/supplement-search/'+request_id+'/readings'
    choices=client.get(url).json()
    assert choices['remaining_slots']==3
    assert choices['items'][0]['analyses'][0]['analysis_id']==analysis_id
    assert len(state['requests'])==before
    defaults_b(monkeypatch)
    response,task=post(client,url,body(analysis_id))
    assert task['status']=='succeeded',task
    assert len(state['requests'])==before+1
    actual,schema,payload=state['requests'][-1]
    assert schema=='CoverageDraft' and json.loads(actual.content)['model']=='model-A'
    assert [p['paper_id'] for p in payload['papers']]==[1,2,3]
    assert payload['auxiliary_reviews'][-1]['status']=='not_reviewed'
    result=client.get('/api/v1/coverage/'+task['result_id']).json()
    assert result['is_current'] and result['analysis_ids'][-1]==analysis_id
    assert coverage.read(coverage_id)==original
    record=coverage.read(response['reading_selection_id'])
    assert record['additions'][0]['analysis_sha256'] and record['reason']==body(analysis_id)['reason']
    again,again_task=post(client,url,body(analysis_id))
    assert again['execution_id']==response['execution_id'] and again_task['id']==task['id']
    assert len(state['requests'])==before+1
    run=coverage.source_run('library-plan',response['execution_id'])
    assert run['selection_kind']=='supplement_readings' and not run['review_after_analysis']
    assert run['papers'][-1]['analysis']['source_task_id']==load_result(analysis_id)['source_task_id']
    trace=client.get('/api/v1/tasks/'+task['id']+'/trace').json()
    assert any(t.get('reading_selection_id')==record['id'] for t in trace['tasks'])
    # 新分支可沿用比较/综述接口，不需要重新分析或检索。
    _,report=post(client,BASE+'/executions/'+response['execution_id']+'/reports',{
        'research_question':'方法和能耗如何比较？','comparison_dimensions':['方法','能耗']})
    assert report['status']=='succeeded',report


def test_foreign_candidate_analysis_and_blank_reason_rejected(completed):
    client,state,_,request_id,coverage_id,analysis_id=completed
    url='/api/v1/supplement-search/'+request_id+'/readings';before=len(state['requests'])
    invalid=body(analysis_id);invalid['readings'][0]['candidate_id']='arxiv:9999.99999'
    assert client.post(url,json=invalid).status_code==422
    invalid=body(coverage.read(coverage_id)['analysis_ids'][0])
    assert client.post(url,json=invalid).status_code==422
    invalid=body(analysis_id);invalid['reason']=' '
    assert client.post(url,json=invalid).status_code==422
    assert len(state['requests'])==before


def test_changed_identity_or_document_blocks_adoption(completed):
    client,state,Session,request_id,_,analysis_id=completed
    url='/api/v1/supplement-search/'+request_id+'/readings';before=len(state['requests'])
    with Session.begin() as session:session.get(PaperRecord,3).title='Different paper'
    assert client.post(url,json=body(analysis_id)).status_code==422
    with Session.begin() as session:
        session.get(PaperRecord,3).title='Robot vision comparison'
        session.get(PaperDocumentRecord,12).content_sha256='changed'
    assert client.post(url,json=body(analysis_id)).status_code==422
    assert len(state['requests'])==before


def test_pending_search_cannot_be_adopted(ready):
    client,state,_,_,_=ready;client.app.include_router(readings.router)
    state['fail_schema']='ScreeningBatch'
    request,task=launch_search(ready)
    assert task['status']=='failed'
    assert client.get('/api/v1/supplement-search/'+request['request_id']+'/readings').status_code==422


def test_failed_coverage_reuses_branch_and_retries_original_configuration(completed,monkeypatch):
    client,state,_,request_id,_,analysis_id=completed
    url='/api/v1/supplement-search/'+request_id+'/readings'
    state['fail_schema']='CoverageDraft'
    first,failed=post(client,url,body(analysis_id))
    assert failed['status']=='failed'
    state['fail_schema']=None;defaults_b(monkeypatch)
    second,succeeded=post(client,url,body(analysis_id))
    assert succeeded['status']=='succeeded',succeeded
    assert first['execution_id']==second['execution_id']
    assert succeeded['resumed_from_task_id']==failed['id']
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'


def test_five_paper_limit_does_not_remove_original_material(completed,monkeypatch):
    client,state,_,request_id,_,analysis_id=completed
    original=readings.completed
    def at_capacity(identifier):
        detail,parent,source=original(identifier)
        source=deepcopy(source);source['selection']['paper_ids']=[1,2,5,6,7]
        return detail,parent,source
    monkeypatch.setattr(readings,'completed',at_capacity)
    before=len(state['requests'])
    response=client.post('/api/v1/supplement-search/'+request_id+'/readings',json=body(analysis_id))
    assert response.status_code==422 and '最多5篇' in response.text
    assert len(state['requests'])==before


def test_adoption_does_not_inherit_unrelated_review_selection(completed):
    from api import execution_review_links as links
    client,state,_,request_id,_,analysis_id=completed
    client.app.include_router(links.router)
    request=readings.supplement_search.detail(request_id)['request']
    source=coverage.read(coverage.read(request['coverage_result_id'])['request_id'])
    base=BASE+'/executions/'+source['execution_id']
    review,_=post(client,base+'/review-versions',{'analysis_id':source['selection']['analysis_ids'][0]})
    state['coverage_response']={'questions':[{'question_id':'Q1','status':'unanswered','answer':'','reason':'缺少材料',
        'gaps':['补充对照'],'finding_ids':[],'citations':[],'suggestions':[
            {'action':'search_later','paper_ids':[],'target':'实验对照','rationale':'需要补充材料'}]}],'limitations':['仅本轮材料']}
    linked,checked=post(client,base+'/review-links',{'review_ids':[review['review_id']]})
    assert checked['status']=='succeeded',checked
    searched,search_task=post(client,'/api/v1/coverage/'+checked['result_id']+'/supplement-search',{
        'question_id':'Q1','suggestion_index':0,'queries':['robot energy'],'max_calls':4,'total_seconds':60})
    assert search_task['status']=='succeeded',search_task
    state['coverage_response']=None
    result,task=post(client,'/api/v1/supplement-search/'+searched['request_id']+'/readings',body(analysis_id))
    assert task['status']=='succeeded',task
    adopted=coverage.source_run('library-plan',result['execution_id'])
    assert not adopted.get('review_selection_id')
