from types import SimpleNamespace
from dataclasses import replace
import hashlib
import pytest

from agent.fast_reading import choose_chunks,heading_kind,VERSION
from agent.batch_analysis import plan_fingerprint,split_context
from test.test_api_documents import client as database_client
from test.test_api_analyses import client as analysis_client,model


def article():
    text='';chunks=[]
    for title,count in [('Abstract',2),('1 Introduction',5),('2 Related Work',8),('3 Methods',20),('4 Experimental Setup',8),('5 Results and Discussion',8),('6 Conclusion and Limitations',3),('References',12),('Appendix A More experiments',20)]:
        for i in range(count):
            value=(title+'\n' if i==0 else '')+f'{title} section sentence {i}. This is source evidence.\n'
            chunks.append(SimpleNamespace(id=len(chunks)+1,start_char=len(text),end_char=len(text)+len(value),text=value,page_number=len(chunks)//3+1,section_hint=None))
            text+=value
    return text,chunks


def test_budget_covers_every_priority_section_and_excludes_back_matter():
    text,chunks=article();selected,meta=choose_chunks(chunks,text,18)
    assert len(selected)==18 and [c.id for c in selected]==sorted(c.id for c in selected)
    assert not any('Appendix' in c.text or 'References' in c.text for c in selected)
    covered={s['category']:s['selected_chunks'] for s in meta['sections']}
    assert all(covered[k]>=2 for k in ('overview','method','experiments','conclusion'))
    assert not meta['missing_sections'] and meta['strategy']=='section_priority'
    assert all(text[c.start_char:c.end_char]==c.text for c in selected)
    assert [c.id for c in choose_chunks(chunks,text,18)[0]]==[c.id for c in selected]


@pytest.mark.parametrize('heading,kind',[('3.1 Methodology','method'),('4 Experimental Setup','experiments'),('5 Results and Discussion','experiments'),('6 Conclusion and Limitations','conclusion'),('参考文献','references'),('Appendix A More experiments','appendix')])
def test_headings(heading,kind):assert heading_kind(heading)==kind


def test_missing_headings_falls_back_honestly():
    chunks=[SimpleNamespace(id=i,text='No standard heading.\n',start_char=i*21,end_char=(i+1)*21,page_number=i+1) for i in range(30)]
    selected,meta=choose_chunks(chunks,'No standard heading.\n'*30,8)
    assert len(selected)==8 and selected[0].id==0 and selected[-1].id==29
    assert meta['strategy']=='body_fallback' and len(meta['missing_sections'])==4


def test_small_document_spanning_several_sections_records_all_coverage():
    text='Abstract\nProblem.\nMethods\nOur approach.\nResults\nGood.\nConclusion\nLimit.'
    chunk=SimpleNamespace(id=1,text=text,start_char=0,end_char=len(text),page_number=1)
    selected,meta=choose_chunks([chunk],text,18)
    assert selected==[chunk] and not meta['missing_sections']


@pytest.fixture
def long_paper(analysis_client):
    from api import documents
    from storage.db_models import PaperDocumentRecord,DocumentChunkRecord
    from sqlalchemy import delete
    text,chunks=article();_,Session=documents.make_db('sqlite://')
    with Session.begin() as session:
        doc=session.get(PaperDocumentRecord,10);doc.content=text;doc.content_sha256=hashlib.sha256(text.encode()).hexdigest();doc.page_count=40
        session.execute(delete(DocumentChunkRecord).where(DocumentChunkRecord.document_id==10))
        for i,c in enumerate(chunks):session.add(DocumentChunkRecord(document_id=10,chunk_index=i,text=c.text,start_char=c.start_char,end_char=c.end_char,page_number=c.page_number))
    return analysis_client


def test_fast_api_result_budget_metadata_and_cache_separation(long_paper,model):
    client=long_paper
    response=client.post('/api/v1/analyses',json={'paper_id':1,'scope':'fast'})
    assert response.status_code==202,response.text
    task=client.get('/api/v1/tasks/'+response.json()['task_id']).json()
    assert task['status']=='succeeded',task
    result=client.get('/api/v1/analyses/'+task['result_id']).json()
    assert result['batch_summary']['scope']=='fast' and result['document']['selected_chunks']==18
    assert result['reading_selection']['policy_version']==VERSION
    assert result['is_current'] and model['calls']==9
    again=client.post('/api/v1/analyses',json={'paper_id':1,'scope':'fast'}).json()
    repeated=client.get('/api/v1/tasks/'+again['task_id']).json()
    assert repeated['resumed_batches']==9 and model['calls']==9
    mismatch=client.post('/api/v1/analyses',json={'paper_id':1,'scope':'full','resume_task_id':task['id']})
    assert mismatch.status_code==409
    assert client.post('/api/v1/analyses',json={'paper_id':1,'scope':'fast','fast_max_chunks':4}).status_code==422
    assert client.post('/api/v1/analyses',json={'paper_id':1,'scope':'fast','fast_max_chunks':8,'resume_task_id':task['id']}).status_code==409
    shorter=client.post('/api/v1/analyses',json={'paper_id':1,'scope':'fast','fast_max_chunks':8}).json()
    smaller=client.get('/api/v1/tasks/'+shorter['task_id']).json()
    assert smaller['status']=='succeeded' and smaller['selected_chunks']==8
    assert smaller['batch_run_key']!=task['batch_run_key'] and smaller['resumed_batches']==0


def test_full_fingerprint_unchanged_and_fast_policy_is_part_of_key(long_paper):
    from agent.paper_context import load_paper_context
    ctx=load_paper_context('sqlite://',1,max_chunks=None)
    def key(c,scope):return plan_fingerprint(c,split_context(c),model='test',base_url='test',method='json_schema',scope=scope)
    changed=replace(ctx,reading_selection={'policy_version':'changed'})
    assert key(ctx,'full')==key(changed,'full')
    assert key(ctx,'fast')!=key(changed,'fast')
