from dataclasses import replace
import pytest
from agent.analysis_validator import EvidenceSource, PaperContext
from agent.targeted_reading import candidates
from api import tasks
from storage import reread_budget as budget


def test_candidates_use_original_ids_full_text_and_mark_read_scope():
    context=PaperContext(paper_id=1,paper_title='题目',doi=None,document_id=1,document_sha256='x',source_url='local',total_chunks=3,sources={
        'E10':EvidenceSource('E10','A benchmark for stereo models.','local',1,'Introduction'),
        'E11':EvidenceSource('E11','Experiments compare runtime and memory.','local',4,'Experiments'),
        'E12':EvidenceSource('E12','Runtime is measured on the device.','local',5,'Results')})
    rows=candidates(context,'运行条件如何？','experiments runtime',{'E10','E11'})
    assert rows[0]['evidence_id']=='E11' and rows[0]['already_read']
    assert {r['evidence_id'] for r in rows}==set(context.sources)
    assert next(r for r in rows if r['evidence_id']=='E12')['already_read'] is False
    assert all(r['text']==context.sources[r['evidence_id']].text for r in rows)


def test_budget_reserves_failed_and_unknown_attempts_and_does_not_reset(monkeypatch,tmp_path):
    monkeypatch.setattr(tasks,'TASK_DB_PATH',tmp_path/'tasks.sqlite')
    a=tasks.create_task();b=tasks.create_task()
    record={'id':'request','budget':{'max_calls':2,'total_seconds':30}}
    assert budget.acquire(record,a['id'])
    assert budget.acquire(record,b['id']) is False
    index,_=budget.reserve('request',a['id'])
    budget.finish('request',index,status='failed')
    budget.release('request',a['id'])
    assert budget.acquire(record,b['id'])
    budget.reserve('request',b['id'])
    with pytest.raises(budget.ReadingStopped,match='预算已用完'):
        budget.reserve('request',b['id'])
    assert budget.summary('request')['calls_used']==2
    with tasks.connection() as db: db.execute('UPDATE reread_budgets SET deadline=0 WHERE id=?',('request',))
    with pytest.raises(budget.ReadingStopped) as caught: budget.reserve('request',b['id'])
    assert caught.value.code=='REREAD_DEADLINE_EXCEEDED'


def test_quote_candidate_call_shares_analysis_budget(monkeypatch,tmp_path):
    """真实分析及候选选择调用同一预算代理；第二次调用不能越过剩余额度。"""
    from contextlib import contextmanager
    from types import SimpleNamespace
    from agent.analysis_schema import PaperAnalysis
    from agent.paper_analyzer import analyze_paper
    from agent.runtime_snapshot import RuntimeSnapshot
    from api import rereads
    import json
    from pathlib import Path
    from test.test_evidence_repairs import context_with_sources
    from test.test_quote_selection import Model
    fixture=json.loads((Path(__file__).parent/'fixtures/batch_15_layout_quotes.json').read_text())
    context=context_with_sources(E708=fixture['source_text'])
    candidate=PaperAnalysis.model_validate(fixture['candidate_analysis'])
    monkeypatch.setattr(tasks,'TASK_DB_PATH',tmp_path/'tasks.sqlite')
    runtime=RuntimeSnapshot(provider='openai',model='test',endpoint='https://example.test',backend='sdk',read_timeout=10,
        retries=0,trust_env=False,credential_ref='env:LLM_API_KEY',credential_identity='a'*64,network_identity='b'*64)
    binding=SimpleNamespace(runtime=runtime,check=lambda:None)
    monkeypatch.setattr(rereads,'verify',lambda record:None)
    monkeypatch.setattr(rereads.provenance,'transaction',lambda action:None)
    model=Model(candidate)
    @contextmanager
    def factory(**kwargs):yield model
    monkeypatch.setattr(rereads,'analysis_llm',factory)
    record={'id':'quote-budget','budget':{'max_calls':1,'total_seconds':30}}
    task=tasks.create_task();assert budget.acquire(record,task['id'])
    proxy=rereads.BudgetedModel(record,task['id'],binding)
    with pytest.raises(budget.ReadingStopped) as caught:
        analyze_paper(proxy,context,max_repair_attempts=0,allow_quote_selection=True)
    assert caught.value.code=='REREAD_BUDGET_EXHAUSTED' and len(model.calls)==1
    assert budget.summary(record['id'])['calls_used']==1
