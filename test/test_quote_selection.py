"""真实 E708 两处排版错误回归；模型选择不能绕过连续原文核验。"""
from contextlib import contextmanager
from dataclasses import replace
import json
from pathlib import Path
import pytest

from agent.analysis_schema import PaperAnalysis
from agent.analysis_validator import EvidenceValidationError,restore_original_quotes,validate_analysis
from agent.paper_analyzer import analyze_paper
from agent.quote_selection import LayoutQuoteSelection,select_original_quotes
from test.test_evidence_repairs import context_with_sources
from test.test_paper_analysis import make_analysis


@pytest.fixture
def actual():
    data=json.loads((Path(__file__).parent/'fixtures/batch_15_layout_quotes.json').read_text())
    context=context_with_sources(E708=data['source_text'])
    return PaperAnalysis.model_validate(data['candidate_analysis']),context


class Model:
    def __init__(self,analysis,selection=None):self.analysis=analysis;self.selection=selection if selection is not None else {'selected_candidates':['Q1','Q2']};self.calls=[]
    def with_structured_output(self,schema,**kwargs):self.schema=schema;return self
    def invoke(self,messages):
        self.calls.append((self.schema,messages))
        return self.selection if self.schema is LayoutQuoteSelection else self.analysis


def test_actual_two_quotes_are_filled_from_original_after_pointer_selection(actual):
    candidate,context=actual
    with pytest.raises(EvidenceValidationError):validate_analysis(restore_original_quotes(candidate,context),context)
    model=Model(candidate)
    result=analyze_paper(model,context,max_repair_attempts=0,allow_quote_selection=True)
    assert [s for s,_ in model.calls]==[PaperAnalysis,LayoutQuoteSelection]
    validate_analysis(result,context)
    assert 'se-\nlect' in result.methodologies[0].evidence[0].quote
    assert 'pro-\ncessed' in result.methodologies[1].evidence[0].quote
    assert candidate!=result and candidate.methodologies[1].evidence[0].quote not in context.sources['E708'].text
    assert [f.statement for f in candidate.methodologies]==[f.statement for f in result.methodologies]


@pytest.mark.parametrize('selection',[{'selected_candidates':[]},{'selected_candidates':['Q1']},
    {'selected_candidates':['Q1','Q1']},{'selected_candidates':['Q1','Q99']},
    {'selected_candidates':['Q1','Q2'],'quote':'forged'},
    {'raw':None,'parsed':None}, {'unexpected':True}])
def test_missing_duplicate_unknown_or_rewritten_choices_are_rejected(actual,selection):
    candidate,context=actual;model=Model(candidate,selection)
    with pytest.raises(EvidenceValidationError):analyze_paper(model,context,max_repair_attempts=0,allow_quote_selection=True)
    assert len(model.calls)==2


@pytest.mark.parametrize('change',['number','word','unknown','cross_chunk'])
def test_content_errors_never_trigger_layout_selection(actual,change):
    candidate,context=actual;candidate=candidate.model_copy(deep=True)
    ref=candidate.methodologies[0].evidence[0]
    if change=='number':ref.quote+=' 99%'
    elif change=='word':ref.quote=ref.quote.replace('dense retrieval','invented retrieval')
    elif change=='unknown':ref.evidence_id='E99999'
    else:ref.quote=ref.quote+' Text from another fragment.'
    model=Model(candidate)
    assert select_original_quotes(model,candidate,context,'json_schema') is None
    assert not model.calls


def test_resume_14_saved_batches_uses_one_analysis_and_one_pointer_call(actual,tmp_path):
    from agent.batch_analysis import analyze_batches
    from storage.analysis_batches import AnalysisBatchStore
    candidate,last=actual
    batches=[context_with_sources(**{f'E{i}':f'Original text for saved batch {i}.'}) for i in range(1,15)]+[last]
    context=replace(last,sources={key:s for b in batches for key,s in b.sources.items()})
    store=AnalysisBatchStore(tmp_path/'batches.sqlite3');store.prepare('original-run','unchanged-fingerprint',batches)
    for i,batch in enumerate(batches[:-1],1):
        source=next(iter(batch.sources.values()))
        store.save_completed('original-run',i,make_analysis(evidence_id=source.evidence_id,quote=source.text))
    model=Model(candidate)
    @contextmanager
    def factory(**kw):yield model
    result,summary=analyze_batches(context,batches,run_key='original-run',store=store,llm_factory=factory,read_timeout=10,method='json_schema',batch_retries=0)
    assert summary['resumed_batches']==14 and len(model.calls)==2
    assert store.get_completed('original-run',15) is not None
    validate_analysis(result,context)
