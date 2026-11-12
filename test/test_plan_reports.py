import json
import pytest
from sqlalchemy import select

from api import plan_reports,reviews,tasks,documents
from test.test_api_documents import client as database_client
from test.test_api_analyses import client as analysis_client,model as analysis_model
from test.test_api_reviews import client as review_client,model as review_model
from test.test_research_planning import proposal

BASE='/api/v1/agent/plans/plan-fixed/executions/execution-fixed'
BODY={'research_question':'两种方法如何比较？','comparison_dimensions':['精度','能耗']}


@pytest.fixture
def setup(review_client,review_model):
    client=review_client;client.app.include_router(plan_reports.router)
    task=tasks.create_task(task_type='research_execution');tasks.update_task(task['id'],status='succeeded')
    ids=[client.get('/api/v1/analyses',params={'paper_id':pid}).json()[0]['id'] for pid in (1,3)]
    run=dict(id='execution-fixed',plan_id='plan-fixed',revision=7,task_id=task['id'],plan=proposal(),goal='比较方法和证据',search_run_id='search-fixed',papers=[dict(paper_id=pid,analysis={'status':'succeeded','analysis_id':aid}) for pid,aid in zip((1,3),ids)])
    with tasks.connection() as db:
        plan_reports.initialize(db)
        db.execute('INSERT INTO research_executions VALUES (?,?,?,?,?)',(run['id'],run['plan_id'],7,'key',json.dumps(run)))
    return client,review_model,ids


def test_full_fixed_plan_comparison_review_exports_and_repeat_reuse(setup):
    client,model,ids=setup
    compare=client.get(BASE+'/comparison')
    assert compare.status_code==200,compare.text
    assert compare.json()['analysis_ids']==ids and model['calls']==0
    response=client.post(BASE+'/reports',json=BODY)
    assert response.status_code==202,response.text
    record=client.get(BASE+'/reports').json()['items'][0]
    assert record['status']=='succeeded',record
    assert record['revision']==7 and record['selection']['analysis_ids']==ids
    result=client.get('/api/v1/reviews/'+record['review_id']).json()
    assert result['analysis_ids']==ids and result['research_context']['plan_revision']==7
    assert result['research_context']['comparison_dimensions']==['精度','能耗']
    for fmt in ('md','csv','json'):
        export=client.get('/api/v1/reviews/'+record['review_id']+'/files/'+fmt)
        assert export.status_code==200 and export.content
        if fmt=='md':assert '计划与阅读范围' in export.text and '版本：7' in export.text
    again=client.post(BASE+'/reports',json=BODY).json()
    assert again['reused'] and again['report_id']==record['id'] and model['calls']==1
    assert client.get('/api/v1/tasks/'+response.json()['task_id']).json()['result_id']==record['review_id']


def test_bad_citations_retain_matrix_and_retry_without_new_analysis(setup):
    client,model,ids=setup;model['mode']='bad_reference'
    client.post(BASE+'/reports',json=BODY)
    failed=client.get(BASE+'/reports').json()['items'][0]
    assert failed['status']=='failed' and failed['comparison']['analysis_ids']==ids
    assert not failed['review_id'] and failed['error']['code']=='REVIEW_VALIDATION_FAILED'
    model['mode']='valid';client.post(BASE+'/reports',json=BODY)
    good=client.get(BASE+'/reports').json()['items'][0]
    assert good['id']==failed['id'] and good['status']=='succeeded'
    assert good['selection']['analysis_ids']==ids


def test_stale_inputs_and_foreign_execution_are_rejected(setup):
    from storage.db_models import PaperDocumentRecord
    client,_,_=setup
    assert client.post(BASE.replace('plan-fixed','other')+'/reports',json=BODY).status_code==404
    _,Session=documents.make_db('sqlite://')
    with Session.begin() as session:session.get(PaperDocumentRecord,10).content_sha256='changed'
    assert client.post(BASE+'/reports',json=BODY).status_code==422
    assert not client.get(BASE+'/reports').json()['items']


def test_active_dedup_and_restart_only_retries_report(setup,monkeypatch):
    client,model,ids=setup;worker=plan_reports.worker
    monkeypatch.setattr(plan_reports,'worker',lambda r:None)
    first=client.post(BASE+'/reports',json=BODY).json()
    assert client.post(BASE+'/reports',json=BODY).json()['task_id']==first['task_id']
    assert client.post(BASE+'/reports',json=dict(BODY,research_question='不同问题')).status_code==409
    tasks.recover_interrupted_tasks()
    assert client.get(BASE+'/reports').json()['items'][0]['status']=='failed'
    monkeypatch.setattr(plan_reports,'worker',worker)
    result=client.post(BASE+'/reports',json=BODY)
    assert result.status_code==202 and result.json()['task_id']!=first['task_id']
    assert client.get(BASE+'/reports').json()['items'][0]['status']=='succeeded'


def test_fast_coverage_reaches_report_prompt_and_markdown(setup):
    client,model,ids=setup
    with tasks.connection() as db:
        for aid in ids:
            row=json.loads(db.execute('SELECT payload FROM analyses WHERE id=?',(aid,)).fetchone()[0])
            row['batch_summary']['scope']='fast';row['reading_selection']={'note':'快速阅读，仅覆盖关键章节；附录未读。'}
            db.execute('UPDATE analyses SET payload=? WHERE id=?',(json.dumps(row),aid))
    client.post(BASE+'/reports',json=BODY)
    report=client.get(BASE+'/reports').json()['items'][0]
    assert report['status']=='succeeded',report
    assert all(c['scope']=='fast' for c in report['context']['reading_coverage'])
    assert '附录未读' in json.dumps(model['payload'],ensure_ascii=False)
    exported=client.get('/api/v1/reviews/'+report['review_id']+'/files/md')
    assert '附录未读' in exported.text


@pytest.mark.parametrize('body',[dict(BODY,research_question=' '),dict(BODY,analysis_ids=['forged']),dict(BODY,comparison_dimensions=['精度','精度'])])
def test_invalid_report_inputs_never_run_model(setup,body):
    client,model,_=setup
    assert client.post(BASE+'/reports',json=body).status_code==422 and model['calls']==0


def test_completed_child_is_restored_after_parent_interruption(setup):
    client,model,_=setup
    client.post(BASE+'/reports',json=BODY)
    first=client.get(BASE+'/reports').json()['items'][0]
    tasks.update_task(first['task_id'],status='failed',error={'code':'SERVICE_RESTARTED','message':'服务重启'})
    client.post(BASE+'/reports',json=BODY)
    restored=client.get(BASE+'/reports').json()['items'][0]
    assert restored['status']=='succeeded' and restored['review_id']==first['review_id']
    assert model['calls']==1
