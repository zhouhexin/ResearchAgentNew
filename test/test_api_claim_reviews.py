"""固定分析的独立评审任务与来源校验；模型受控，不证明语义准确率。"""
from contextlib import contextmanager
from copy import deepcopy
import json

import pytest

from api import claim_reviews as reviews, documents, tasks
from api.analysis_results import complete_analysis, load_result
from agent.claim_reference_verifier import ReferenceClaimReview
from storage.db_models import DocumentChunkRecord, PaperDocumentRecord
from sqlalchemy import select
from test.test_api_documents import client as database_client


@pytest.fixture
def workspace(database_client, monkeypatch, tmp_path):
    monkeypatch.setattr(reviews, 'make_db', documents.make_db)
    monkeypatch.setattr(reviews, 'RUN_ROOT', tmp_path/'reviews')
    monkeypatch.setenv('LLM_PROVIDER', 'openai')
    monkeypatch.setenv('LLM_HTTP_BACKEND', 'sdk')
    monkeypatch.setenv('LLM_API_KEY', 'private-model-key')
    monkeypatch.setenv('LLM_MODEL', 'test-model')
    monkeypatch.setenv('LLM_READ_TIMEOUT', '30')
    monkeypatch.setenv('LLM_MAX_RETRIES', '0')
    monkeypatch.setenv('STRUCTURED_OUTPUT_METHOD', 'json_schema')
    monkeypatch.setenv('CLAIM_REVIEW_MAX_INPUT_CHARS', '24000')
    _, Session = documents.make_db('sqlite://')
    with Session() as session:
        doc = session.get(PaperDocumentRecord,10)
        chunks = session.scalars(select(DocumentChunkRecord).where(DocumentChunkRecord.document_id==10).order_by(DocumentChunkRecord.id)).all()
        index = {f'E{c.id}': {'text': c.text,'page_number': c.page_number,'section_hint': c.section_hint,'source_url': '本地导入 PDF'} for c in chunks}
        keys = list(index)
        payload = {'paper': {'id':1,'title':'测试论文','doi':None},
            'document':{'id':10,'source_url':'本地导入 PDF','content_sha256':doc.content_sha256,'selected_chunks':2,'total_chunks':2},
            'analysis':{'research_questions':[{'statement':'第一条完整结论','evidence':[{'evidence_id':keys[0],'quote':'第一段'}]}],
                        'methodologies':[{'statement':'第二条完整结论','evidence':[{'evidence_id':keys[1],'quote':'第二段'}]}],
                        'contributions':[],'experiments':[],'limitations':[]},'evidence_index':index}
    task=tasks.create_task(1,task_type='paper_analysis')
    snapshot=complete_analysis(task['id'],payload)
    database_client.app.include_router(tasks.router)
    database_client.app.include_router(reviews.router)
    return database_client,snapshot,Session


@pytest.fixture
def model(monkeypatch):
    state={'calls':[],'fail_id':None,'hook':None}
    class Model:
        def with_structured_output(self,schema,**kwargs):
            self.schema=schema
            return self
        def invoke(self,messages):
            value=json.loads(messages[1][1]);state['calls'].append(deepcopy(value))
            if state['hook']:state['hook']()
            if issubclass(self.schema,ReferenceClaimReview):
                return {'item_id':value['sample_id'],'verdict':'supported','reason':'原文支持完整结论。',
                    'evidence_assessments':[{'evidence_id':e['evidence_id'],'relation':'supports','reason':'依据所给原文。',
                        'span_ids':['unknown-span' if state['fail_id']==value['sample_id'] else e['spans'][0]['span_id']]} for e in value['evidence']]}
            return {'item_id':value['sample_id'],'verdict':'supported','reason':'原文支持完整结论。',
                'evidence_assessments':[{'evidence_id':e['evidence_id'],'relation':'supports','reason':'依据所给原文。',
                    'quoted_spans':['不存在的引用' if state['fail_id']==value['sample_id'] else e['quote']]} for e in value['evidence']]}
    @contextmanager
    def factory(**kwargs):yield Model()
    monkeypatch.setattr(reviews,'analysis_llm',factory)
    return state


def start(client,snapshot):
    response=client.post(f"/api/v1/analyses/{snapshot['id']}/claim-reviews")
    assert response.status_code==202,response.text
    accepted=response.json()
    return accepted,client.get('/api/v1/claim-reviews/'+accepted['review_id']).json()


def test_separate_task_source_quotes_requery_and_success_dedup(workspace,model):
    client,snapshot,_=workspace
    before=deepcopy(load_result(snapshot['id']))
    accepted,record=start(client,snapshot)
    assert record['completed']==record['total']==2 and record['is_current']
    assert record['task']['status']=='succeeded' and record['task']['type']=='claim_review'
    assert all(v['status']=='succeeded' for v in record['items'])
    assert record['items'][0]['review']['verdict']=='supported'
    assert len(model['calls'])==2
    assert all(set(c)=={'sample_id','paper_title','statement','evidence'} for c in model['calls'])
    assert load_result(snapshot['id'])==before
    assert 'private-model-key' not in json.dumps(record) and '/private/' not in json.dumps(record)
    assert client.get(f"/api/v1/analyses/{snapshot['id']}/claim-reviews").json()['items'][0]['id']==record['id']
    again,_=start(client,snapshot)
    assert again['reused'] and again['task_id']==accepted['task_id'] and len(model['calls'])==2


def test_partial_failure_resumes_only_failed_item_preserves_task_history(workspace,model):
    client,snapshot,_=workspace
    model['fail_id']='methodologies-0'
    first,record=start(client,snapshot)
    assert record['task']['status']=='failed' and record['completed']==1
    assert record['items'][1]['review'] is None and record['items'][1]['error']['code']=='CLAIM_REFERENCE_INVALID'
    model['fail_id']=None
    second,record=start(client,snapshot)
    assert second['review_id']==first['review_id'] and second['task_id']!=first['task_id']
    assert record['completed']==2 and len(model['calls'])==3
    assert [c['sample_id'] for c in model['calls']].count('research_questions-0')==1
    assert client.get('/api/v1/tasks/'+first['task_id']).json()['status']=='failed'


def test_document_change_during_model_call_does_not_publish_as_current(workspace,model):
    client,snapshot,Session=workspace
    def changed():
        with Session.begin() as session:session.get(PaperDocumentRecord,10).content_sha256='changed'
    model['hook']=changed
    _,record=start(client,snapshot)
    assert record['task']['status']=='failed' and record['task']['error']['code']=='DOCUMENT_CHANGED'
    assert not record['is_current'] and record['completed']==0 and len(model['calls'])==1
    assert client.post(f"/api/v1/analyses/{snapshot['id']}/claim-reviews").status_code==409


@pytest.mark.parametrize('change',['chunk','page','hash','identity'])
def test_changed_sources_prevent_any_new_model_calls_and_mark_old_review_historical(workspace,model,change):
    client,snapshot,Session=workspace
    accepted,original=start(client,snapshot)
    with Session.begin() as session:
        doc=session.get(PaperDocumentRecord,10)
        if change=='hash':doc.content_sha256='updated'
        elif change=='identity':doc.paper_id=2
        else:
            c=session.scalar(select(DocumentChunkRecord).where(DocumentChunkRecord.document_id==10))
            if change=='chunk':c.text='被更改的片段'
            else:c.page_number=99
    old=client.get('/api/v1/claim-reviews/'+accepted['review_id']).json()
    assert old['completed']==2 and not old['is_current']
    assert client.post(f"/api/v1/analyses/{snapshot['id']}/claim-reviews").status_code==409
    assert len(model['calls'])==2 and old['items']==original['items']


def test_model_change_creates_another_review_without_overwriting_history(workspace,model,monkeypatch):
    client,snapshot,_=workspace
    first,_=start(client,snapshot)
    monkeypatch.setenv('LLM_MODEL','another-model')
    second,_=start(client,snapshot)
    assert first['review_id']!=second['review_id'] and len(model['calls'])==4
    assert len(client.get(f"/api/v1/analyses/{snapshot['id']}/claim-reviews").json()['items'])==2


def test_restart_recovery_reschedules_same_review(workspace,model):
    from fastapi import BackgroundTasks
    client,snapshot,_=workspace
    queued=reviews.start_review(snapshot['id'],BackgroundTasks())
    duplicate=reviews.start_review(snapshot['id'],BackgroundTasks())
    assert duplicate['reused'] and duplicate['task_id']==queued['task_id']
    tasks.recover_interrupted_tasks()
    new,record=start(client,snapshot)
    assert new['review_id']==queued['review_id'] and new['task_id']!=queued['task_id']
    assert record['completed']==2 and len(model['calls'])==2


@pytest.mark.parametrize('mode',['quote','invalid_json'])
def test_tampered_saved_quote_is_not_displayed_as_success_and_retried(workspace,model,mode):
    client,snapshot,_=workspace
    accepted,_=start(client,snapshot)
    path=next((reviews.RUN_ROOT/accepted['review_id']).glob('*/items/research_questions-0.json'))
    result=json.loads(path.read_text());result['review']['evidence_assessments'][0]['quoted_spans']=['伪造的引文']
    path.write_text(json.dumps(result) if mode=='quote' else 'invalid checkpoint json')
    record=client.get('/api/v1/claim-reviews/'+accepted['review_id']).json()
    assert record['completed']==1 and record['items'][0]['review'] is None
    _,fixed=start(client,snapshot)
    assert fixed['completed']==2 and len(model['calls'])==3


def test_missing_analysis_and_review_do_not_call_models(workspace,model):
    client,_,_=workspace
    assert client.post('/api/v1/analyses/missing/claim-reviews').status_code==404
    assert client.get('/api/v1/analyses/missing/claim-reviews').status_code==404
    assert client.get('/api/v1/claim-reviews/missing').status_code==404
    assert model['calls']==[]


def test_non_pdf_analysis_is_rejected_without_model_calls(workspace,model):
    client,snapshot,Session=workspace
    with Session.begin() as session:session.get(PaperDocumentRecord,10).source_type='web'
    response=client.post(f"/api/v1/analyses/{snapshot['id']}/claim-reviews")
    assert response.status_code==422 and 'PDF' in response.json()['detail']
    assert model['calls']==[]


def test_input_budget_never_truncates_material_or_calls_model(workspace,model,monkeypatch):
    client,snapshot,_=workspace
    monkeypatch.setenv('CLAIM_REVIEW_MAX_INPUT_CHARS','1')
    _,record=start(client,snapshot)
    assert record['task']['status']=='failed' and record['completed']==0
    assert all(v['status']=='precheck_failed' for v in record['items'])
    assert model['calls']==[]


def test_same_evidence_multiple_quotes_uses_actual_full_context_without_dropping_material(workspace):
    _,snapshot,_=workspace
    finding=snapshot['analysis']['research_questions'][0]
    finding['evidence'].append(dict(finding['evidence'][0],quote='一段'))
    values=reviews.build_items(snapshot)
    assert len(values[0]['evidence'])==1
    assert values[0]['evidence'][0]['quote']==values[0]['evidence'][0]['context']=='第一段'


@pytest.mark.parametrize('partial',[False,True])
def test_reference_upgrade_explicitly_imports_only_legacy_successes_and_preserves_originals(workspace,model,monkeypatch,partial):
    client,snapshot,_=workspace
    original_configuration=reviews.configuration
    def legacy_configuration(snapshot,settings):
        config=original_configuration(snapshot,settings)
        config.pop('quote_protocol')
        return config
    with monkeypatch.context() as temporary:
        temporary.setattr(reviews,'configuration',legacy_configuration)
        if partial:model['fail_id']='methodologies-0'
        legacy,old=start(client,snapshot)
    old_calls=len(model['calls'])
    old_dir=reviews.RUN_ROOT/legacy['review_id']
    old_files={p:p.read_bytes() for p in old_dir.glob('**/*.json')}
    model['fail_id']=None
    upgraded,current=start(client,snapshot)
    assert upgraded['review_id']!=legacy['review_id']
    assert current['quote_protocol']=='references-v2' and current['completed']==2
    assert current['legacy_reused']==(1 if partial else 2)
    assert len(model['calls'])==old_calls+(1 if partial else 0)
    for p,content in old_files.items():assert p.read_bytes()==content
    old_after=client.get('/api/v1/claim-reviews/'+legacy['review_id']).json()
    assert old_after['items']==old['items']
    first=current['items'][0]
    assert first['quote_protocol']=='legacy-import' and first['legacy_source_review_id']==legacy['review_id']
    assert first['review']==old['items'][0]['review']
    assert current['items'][1]['quote_protocol']==('references-v2' if partial else 'legacy-import')


def test_reference_upgrade_does_not_import_tampered_legacy_requests(workspace,model,monkeypatch):
    client,snapshot,_=workspace
    original_configuration=reviews.configuration
    def legacy_configuration(snapshot,settings):
        config=original_configuration(snapshot,settings);config.pop('quote_protocol');return config
    with monkeypatch.context() as temporary:
        temporary.setattr(reviews,'configuration',legacy_configuration)
        legacy,_=start(client,snapshot)
    path=next((reviews.RUN_ROOT/legacy['review_id']).glob('*/requests/research_questions-0.json'))
    request=json.loads(path.read_text());request['input']['statement']='被修改的其他结论'
    path.write_text(json.dumps(request))
    _,record=start(client,snapshot)
    assert record['legacy_reused']==1 and len(model['calls'])==3


def test_reference_upgrade_waits_for_active_legacy_task(workspace,model,monkeypatch):
    from fastapi import BackgroundTasks
    client,snapshot,_=workspace
    original_configuration=reviews.configuration
    def legacy_configuration(snapshot,settings):
        config=original_configuration(snapshot,settings);config.pop('quote_protocol');return config
    with monkeypatch.context() as temporary:
        temporary.setattr(reviews,'configuration',legacy_configuration)
        reviews.start_review(snapshot['id'],BackgroundTasks())
    response=client.post(f"/api/v1/analyses/{snapshot['id']}/claim-reviews")
    assert response.status_code==409 and model['calls']==[]
