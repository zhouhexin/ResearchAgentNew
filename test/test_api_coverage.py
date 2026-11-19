import json
import pytest

from api import coverage, research_execution, tasks
from storage.db_models import PaperDocumentRecord
from test.test_api_documents import client as database_client
from test.test_research_runtime_provenance import workspace, post, defaults_b
from test.test_library_execution import library, BASE, launch, current


@pytest.fixture
def ready(library, monkeypatch):
    client, state, Session = library
    monkeypatch.setenv('COVERAGE_MAX_OUTPUT_TOKENS', '3456')
    monkeypatch.setenv('COVERAGE_MAX_INPUT_CHARS', '60000')
    client.app.include_router(coverage.router)
    response, task = launch(client)
    assert task['status']=='succeeded', task
    return client, state, Session, BASE+'/executions/'+response['execution_id']+'/coverage'


def test_coverage_pins_actual_model_inputs_extracts_sources_and_exports(ready, monkeypatch):
    client, state, _, url = ready
    defaults_b(monkeypatch)
    request, task = post(client, url, {'research_questions':['方法如何比较？','哪些条件尚未确定？']})
    assert task['status']=='succeeded',task
    result = client.get('/api/v1/coverage/'+task['result_id'])
    assert result.status_code==200,result.text
    saved = result.json()
    assert saved['is_current'] is True and len(saved['report']['questions'])==2
    assert saved['report']['suggestions_executed'] is False
    actual, schema, payload = state['requests'][-1]
    body=json.loads(actual.content)
    assert schema=='CoverageDraft' and body['model']=='model-A' and actual.url.host=='a.example'
    assert body['max_completion_tokens']==3456 and actual.extensions['timeout']['read']==17
    from storage.runtime_versions import load_immutable
    from api import provenance
    from agent.question_coverage import POLICY
    version=provenance.transaction(lambda session: load_immutable(session,task['agent_version_id']))
    assert version.quote_protocol==POLICY
    schema_json=json.dumps(body['response_format'])
    assert 'P1:research_questions:0' in schema_json and 'P1:E1:S1' in schema_json
    assert all(r['status']=='not_reviewed' for r in payload['auxiliary_reviews'])
    quote=saved['report']['questions'][0]['citations'][0]['source_spans'][0]['text']
    assert quote==payload['evidence'][0]['spans'][0]['text']
    for fmt in ('md','json'):
        download=client.get('/api/v1/coverage/'+saved['id']+'/files/'+fmt)
        assert download.status_code==200 and quote in download.text
    before=len(state['requests'])
    again, _=post(client,url,{'research_questions':['方法如何比较？','哪些条件尚未确定？']})
    assert again['reused'] is True and again['request_id']==request['request_id']
    assert len(state['requests'])==before
    trace=client.get('/api/v1/tasks/'+task['id']+'/trace').json()
    assert any(t['id']==task['id'] and t['parent_task_id'] for t in trace['tasks'])


def test_failure_restart_restores_same_config_without_reanalyzing(ready, monkeypatch):
    client,state,_,url=ready
    state['fail_schema']='CoverageDraft'
    first,failed=post(client,url,{})
    assert failed['status']=='failed'
    counts=sum(schema=='PaperAnalysis' for _,schema,_ in state['requests'])
    state['fail_schema']=None
    defaults_b(monkeypatch)
    monkeypatch.setenv('RESEARCH_RUNTIME_PINNING','false')
    _,resumed=post(client,url,{'resume_request_id':first['request_id']})
    assert resumed['status']=='succeeded',resumed
    assert resumed['resumed_from_task_id']==failed['id']
    assert sum(schema=='PaperAnalysis' for _,schema,_ in state['requests'])==counts
    assert json.loads(state['requests'][-1][0].content)['model']=='model-A'


def test_stale_sources_block_new_requests_but_history_is_exportable(ready):
    client,state,Session,url=ready
    _,task=post(client,url,{})
    assert task['status']=='succeeded',task
    before=len(state['requests'])
    with Session.begin() as session: session.get(PaperDocumentRecord,10).content_sha256='changed'
    assert client.get('/api/v1/coverage/'+task['result_id']).json()['is_current'] is False
    assert client.get('/api/v1/coverage/'+task['result_id']+'/files/md').status_code==200
    response=client.post(url,json={})
    assert response.status_code in {409,422}
    assert len(state['requests'])==before


def test_bad_model_question_or_reference_does_not_publish_report(ready):
    client,state,_,url=ready
    state['coverage_response']={'questions':[{'question_id':'Q999','status':'unanswered','answer':'',
        'reason':'没有足够材料','gaps':['缺证据'],'finding_ids':[],'citations':[],'suggestions':[]}], 'limitations':['仅本轮材料']}
    _,task=post(client,url,{})
    assert task['status']=='failed' and task['result_id'] is None
    assert task['error']['code']=='COVERAGE_VALIDATION_FAILED'


def test_queued_input_change_prevents_model_request_and_keeps_analyses(ready,monkeypatch):
    client,state,Session,url=ready
    worker=coverage.worker;pending=[]
    monkeypatch.setattr(coverage,'worker',pending.append)
    _,queued=post(client,url,{})
    before=len(state['requests'])
    with Session.begin() as session: session.get(PaperDocumentRecord,10).content_sha256='changed'
    worker(pending[0])
    failed=tasks.get_task(queued['id'])
    assert failed['status']=='failed' and failed['error']['code']=='COVERAGE_INPUTS_CHANGED'
    assert len(state['requests'])==before
    assert all(p['analysis']['analysis_id'] for p in current(client)['papers'])


def test_completed_result_recovers_after_task_interruption_without_new_call(ready):
    client,state,_,url=ready
    response,task=post(client,url,{})
    assert task['status']=='succeeded'
    before=len(state['requests'])
    tasks.update_task(task['id'],status='failed',error={'code':'SERVICE_RESTARTED','message':'受控中断'})
    _,restored=post(client,url,{'resume_request_id':response['request_id']})
    assert restored['status']=='succeeded' and restored['result_id']==task['result_id']
    assert len(state['requests'])==before


def test_input_budget_rejects_without_model_or_new_coverage_task(library,monkeypatch):
    client,state,_=library
    client.app.include_router(coverage.router)
    monkeypatch.setenv('COVERAGE_MAX_INPUT_CHARS','20')
    execution,task=launch(client)
    assert task['status']=='succeeded'
    before=len(state['requests'])
    response=client.post(BASE+'/executions/'+execution['execution_id']+'/coverage',json={})
    assert response.status_code==422 and response.json()['detail']['code']=='COVERAGE_VALIDATION_FAILED'
    assert len(state['requests'])==before


def test_explicit_reviews_are_advisory_and_changed_checkpoints_stop_resume(library,monkeypatch):
    from api import claim_reviews
    client,state,_=library
    client.app.include_router(coverage.router)
    execution,task=launch(client,review_after_analysis=True)
    assert task['status']=='succeeded',task
    run=current(client)
    url=BASE+'/executions/'+execution['execution_id']+'/coverage'
    _,result=post(client,url,{})
    assert result['status']=='succeeded',result
    _,_,payload=state['requests'][-1]
    assert all(r.get('review_id') for r in payload['auxiliary_reviews'])
    assert all(r['human_verdict'] is None for r in payload['auxiliary_reviews'])
    assert client.post(url,json={'review_ids':['foreign-review']}).status_code==404
    original=coverage.worker; pending=[]
    monkeypatch.setattr(coverage,'worker',pending.append)
    _,queued=post(client,url,{'research_questions':['适用条件是什么？']})
    path=next(claim_reviews.RUN_ROOT.glob('**/items/*.json'))
    saved=json.loads(path.read_text());saved['review']['reason']='变更后的模型评审理由'
    path.write_text(json.dumps(saved))
    before=len(state['requests'])
    original(pending[0])
    assert tasks.get_task(queued['id'])['status']=='failed'
    assert len(state['requests'])==before


def test_new_execution_with_old_role_bundle_refuses_silent_config_fallback(ready):
    client,state,_,url=ready
    with tasks.connection() as db:
        row=db.execute('SELECT id,payload FROM research_executions').fetchone()
        run=json.loads(row[1]);run['runtime_bindings'].pop('coverage')
        db.execute('UPDATE research_executions SET payload=? WHERE id=?',(json.dumps(run),row[0]))
    before=len(state['requests'])
    response=client.post(url,json={})
    assert response.status_code==409 and response.json()['detail']['code']=='RUNTIME_SNAPSHOT_UNAVAILABLE'
    assert len(state['requests'])==before
