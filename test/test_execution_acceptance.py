from copy import deepcopy
import pytest
from api import coverage, execution_acceptance as acceptance
from storage.db_models import PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post
from test.test_library_execution import library, BASE
from test.test_api_coverage import ready


@pytest.fixture
def execution(ready):
    client,state,Session,url=ready;client.app.include_router(acceptance.router)
    _,checked=post(client,url,{})
    assert checked['status']=='succeeded'
    execution_id=url.split('/')[-2]
    base=BASE+'/executions/'+execution_id
    _,report=post(client,base+'/reports',{'research_question':'方法如何比较？','comparison_dimensions':['方法']})
    assert report['status']=='succeeded',report
    return client,state,Session,base,{'coverage_id':checked['result_id'],'report_id':report['result_id']}


def manual():return [{'key':key,'verdict':'not_checked','reason':''} for key in sorted(acceptance.MANUAL_KEYS)]


def test_inspect_save_read_export_are_model_free_and_do_not_claim_acceptance(execution):
    client,state,_,base,selection=execution;before=len(state['requests'])
    response=client.post(base+'/acceptance/inspect',json=selection)
    assert response.status_code==200,response.text
    inspected=response.json()
    assert all(c['status']=='passed' for c in inspected['checks'] if c['key']!='associated_reviews')
    assert next(c for c in inspected['checks'] if c['key']=='associated_reviews')['status']=='not_selected'
    assert len(inspected['missing_review_analysis_ids'])==2
    assert {a['kind'] for a in inspected['artifacts']}=={'analysis','coverage','report'}
    body=selection|{'expected_fingerprint':inspected['fingerprint'],'reviewer':'本机用户','manual':manual()}
    saved=client.post(base+'/acceptance',json=body)
    assert saved.status_code==201,saved.text
    record=saved.json();assert record['evaluation_allowed'] is False and not record['reviewer_identity_verified']
    url='/api/v1/acceptance/'+record['id']
    assert client.get(url).json()['inputs_unchanged']
    for fmt in ('md','json'):assert client.get(url+'/files/'+fmt).status_code==200
    assert client.get(base+'/acceptance').json()['items'][0]['id']==record['id']
    assert len(state['requests'])==before


def test_changed_material_rejects_stale_declaration_preserves_history(execution):
    client,state,Session,base,selection=execution
    inspected=client.post(base+'/acceptance/inspect',json=selection).json()
    body=selection|{'expected_fingerprint':inspected['fingerprint'],'reviewer':'用户','manual':manual()}
    saved=client.post(base+'/acceptance',json=body).json()
    before=len(state['requests'])
    with Session.begin() as session:session.get(PaperDocumentRecord,10).content_sha256='changed'
    assert client.post(base+'/acceptance',json=body).status_code==409
    historical=client.get('/api/v1/acceptance/'+saved['id']).json()
    assert historical['inputs_unchanged'] is False
    assert historical['inspection']==saved['inspection']
    assert any(c['status']=='failed' for c in historical['current_inspection']['checks'])
    assert client.get('/api/v1/acceptance/'+saved['id']+'/files/md').status_code==200
    assert len(state['requests'])==before


def test_missing_foreign_artifacts_and_manual_reasons_not_silently_passed(execution):
    client,state,_,base,selection=execution
    partial=client.post(base+'/acceptance/inspect',json={}).json()
    assert sum(c['status']=='not_selected' for c in partial['checks'])==3
    bad=client.post(base+'/acceptance/inspect',json={'coverage_id':'nonexistent'}).json()
    assert next(c for c in bad['checks'] if c['key']=='coverage')['status']=='failed'
    attested=manual();attested[0]['verdict']='passed'
    body={'expected_fingerprint':partial['fingerprint'],'reviewer':'用户','manual':attested}
    assert client.post(base+'/acceptance',json=body).status_code==422
    attested[0]['verdict']='not_checked';attested[1]['key']=attested[0]['key']
    assert client.post(base+'/acceptance',json=body).status_code==422
    assert client.get('/api/v1/acceptance/'+selection['coverage_id']).status_code==404


def test_foreign_report_is_rejected_and_failed_execution_is_not_hidden(execution):
    from api import tasks
    client,state,_,base,selection=execution
    run_id=base.split('/')[-1]
    before=len(state['requests'])
    inspected=client.post(base+'/acceptance/inspect',json=selection).json()
    snapshot=acceptance.reviews.get_review(selection['report_id'])
    from unittest.mock import patch
    foreign=deepcopy(snapshot);foreign['research_context']['execution_id']='another-execution'
    with patch.object(acceptance.reviews,'get_review',return_value=foreign):
        result=client.post(base+'/acceptance/inspect',json=selection).json()
        assert next(c for c in result['checks'] if c['key']=='report')['status']=='failed'
    with tasks.connection() as db:
        import json
        row=db.execute('SELECT payload FROM research_executions WHERE id=?',(run_id,)).fetchone()
        task_id=json.loads(row[0])['task_id']
    tasks.update_task(task_id,status='failed',error={'code':'TEST_FAILURE','message':'controlled'})
    result=client.post(base+'/acceptance/inspect',json=selection).json()
    assert result['fingerprint']!=inspected['fingerprint']
    assert next(c for c in result['checks'] if c['key']=='execution_completed')['status']=='failed'
    body=selection|{'expected_fingerprint':result['fingerprint'],'reviewer':'用户','manual':manual()}
    saved=client.post(base+'/acceptance',json=body)
    assert saved.status_code==201 and saved.json()['inspection']['execution_status']=='failed'
    assert len(state['requests'])==before
