"""引文修复仍须回到本批可唯一确定的连续原文，不能补造内容。"""
from dataclasses import replace
from contextlib import contextmanager
import json
from pathlib import Path

import pytest

from agent.analysis_schema import PaperAnalysis
from agent.analysis_validator import (EvidenceSource, EvidenceValidationError, PaperContext,
                                      evidence_diagnostics, restore_original_quotes, validate_analysis)
from agent.paper_analyzer import analyze_paper
from test.test_paper_analysis import make_analysis, make_context


def context_with_sources(**texts):
    original = make_context()
    return replace(original, sources={key: EvidenceSource(key, text, original.source_url, index, None)
                                      for index, (key, text) in enumerate(texts.items(), 1)})


@pytest.mark.parametrize('original,quote', [
    ('First line.\nSecond line.', r'First line.\nSecond line.'),
    ('First\r\nsecond\tthird.', r'First\r\nsecond\tthird.'),
    ('本文提出深\n度估计方法。', r'本文提出深\n度估计方法。'),
    ('The label is "β 深度 😀".', r'The label is \"β 深度 😀\".'),
    (r'Use C:\paper\file and measure α.', r'Use C:\\paper\\file and measure α.'),
])
def test_single_layer_escapes_restore_exact_original_characters(original, quote):
    context = context_with_sources(E11=original)
    candidate = make_analysis(quote=quote)
    result = restore_original_quotes(candidate, context)
    validate_analysis(result, context)
    assert result.experiments[0].evidence[0].quote == original
    assert candidate.experiments[0].evidence[0].quote == quote


@pytest.mark.parametrize('quote', [r'Use C:\new\test', r'Use \theta and \tau', r'Literally write \n here'])
def test_already_exact_backslashes_are_never_decoded(quote):
    context = context_with_sources(E11=quote)
    result = restore_original_quotes(make_analysis(quote=quote), context)
    validate_analysis(result, context)
    assert result.experiments[0].evidence[0].quote == quote


def test_wrong_valid_id_moves_only_to_unique_source_in_current_batch(caplog):
    context = context_with_sources(E11='First paragraph.', E12='We compare the\nFP32 model on one scene.')
    candidate = make_analysis(quote=r'We compare the\nFP32 model on one scene.')
    with caplog.at_level('INFO', logger='agent.analysis_validator'):
        result = restore_original_quotes(candidate, context)
    validate_analysis(result, context)
    ref = result.experiments[0].evidence[0]
    assert ref.evidence_id == 'E12' and ref.quote == context.sources['E12'].text
    assert candidate.experiments[0].evidence[0].evidence_id == 'E11'
    assert 'from=E11 to=E12' in caplog.text


def test_overlap_keeps_valid_current_id_and_layout():
    context = context_with_sources(E11='We compare\ntwo models.', E12='We compare two models.')
    result = restore_original_quotes(make_analysis(quote='We compare two models.'), context)
    validate_analysis(result, context)
    assert result.experiments[0].evidence[0].evidence_id == 'E11'
    assert result.experiments[0].evidence[0].quote == 'We compare\ntwo models.'


def test_same_quote_in_two_other_windows_cannot_fix_wrong_id():
    context = context_with_sources(E11='Wrong paragraph.', E12='A shared sentence.', E13='A shared\nsentence.')
    candidate = make_analysis(quote='A shared sentence.')
    result = restore_original_quotes(candidate, context)
    assert result == candidate
    with pytest.raises(EvidenceValidationError):
        validate_analysis(result, context)


def test_two_distinct_layout_matches_in_same_source_are_ambiguous():
    context = context_with_sources(E11='A\nshared sentence. A shared\nsentence.')
    candidate = make_analysis(quote='A shared sentence.')
    result = restore_original_quotes(candidate, context)
    assert result == candidate
    with pytest.raises(EvidenceValidationError):
        validate_analysis(result, context)


@pytest.mark.parametrize('quote', [
    '99% accuracy', 'First sentence. Second sentence.', r'\u0057e compare models.',
    r'First\\nsecond.', 'Depth Anything', 'translated conclusion',
])
def test_no_numeric_word_hyphen_unicode_or_cross_chunk_repairs(quote):
    context = context_with_sources(E11='First sentence. We compare models. 90% accuracy. Depth Any-\nthing. First\nsecond.',
                                   E12='Second sentence.')
    candidate = make_analysis(quote=quote)
    result = restore_original_quotes(candidate, context)
    assert result == candidate
    with pytest.raises(EvidenceValidationError):
        validate_analysis(result, context)


def test_unknown_evidence_id_stays_illegal_even_if_quote_matches_batch():
    context = context_with_sources(E11='A valid quote.')
    candidate = make_analysis(evidence_id='E999', quote='A valid quote.')
    result = restore_original_quotes(candidate, context)
    assert result == candidate
    with pytest.raises(EvidenceValidationError, match='非法或越界'):
        validate_analysis(result, context)


@pytest.fixture
def batch_22():
    value = json.loads((Path(__file__).parent / 'fixtures' / 'batch_22_evidence.json').read_text())
    context = PaperContext(paper_id=value['paper_id'], paper_title='Depth Estimator Benchmark', doi=None,
                          document_id=value['document_id'], document_sha256=value['content_sha256'],
                          source_url='test-fixture', total_chunks=84,
                          sources={key: EvidenceSource(key, item['text'], 'test-fixture', item['page_number'], None)
                                   for key, item in value['sources'].items()})
    return value, context


@pytest.mark.parametrize('name', ['wrong_id', 'escaped_newlines'])
def test_real_batch_22_failures_pass_without_second_model_call(batch_22, name):
    value, context = batch_22
    candidate = PaperAnalysis.model_validate(value['cases'][name])

    class Model:
        calls = 0
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            self.calls += 1
            return candidate

    model = Model()
    result = analyze_paper(model, context, max_repair_attempts=0)
    validate_analysis(result, context)
    assert model.calls == 1
    for category in ('methodologies', 'experiments', 'contributions'):
        for finding in getattr(result, category):
            for ref in finding.evidence:
                assert ref.quote in context.sources[ref.evidence_id].text
    if name == 'wrong_id':
        assert result.methodologies[0].evidence[1].evidence_id == 'E447'
        assert candidate.methodologies[0].evidence[1].evidence_id == 'E448'


def test_real_changed_hyphenation_is_still_rejected(batch_22):
    value, context = batch_22
    result = restore_original_quotes(PaperAnalysis.model_validate(value['cases']['changed_hyphenation']), context)
    with pytest.raises(EvidenceValidationError):
        validate_analysis(result, context)
    issues = evidence_diagnostics(result, context)['issues']
    assert len(issues) == 2
    assert all(issue['evidence_id'] == 'E448' for issue in issues)


def test_resume_reuses_21_saved_batches_and_repairs_only_batch_22(batch_22, tmp_path):
    from agent.batch_analysis import analyze_batches
    from storage.analysis_batches import AnalysisBatchStore

    value, last_batch = batch_22
    batches = []
    for index in range(1, 22):
        key, text = f'E{index}', f'Saved original text for batch {index}.'
        batches.append(replace(last_batch, sources={key: EvidenceSource(key, text, 'test-fixture', index, None)}))
    batches.append(last_batch)
    context = replace(last_batch, sources={key: source for batch in batches for key, source in batch.sources.items()})
    store = AnalysisBatchStore(tmp_path / 'batches.sqlite3')
    store.prepare('existing-run', 'unchanged-plan', batches)
    for index, batch in enumerate(batches[:-1], 1):
        source = next(iter(batch.sources.values()))
        cached = make_analysis(evidence_id=source.evidence_id, quote=source.text)
        cached.experiments[0].statement = f'已核验批次 {index}'
        store.save_completed('existing-run', index, cached)
    store.failed('existing-run', 22, 'EvidenceValidationError')
    calls, events = [], []

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            payload = json.loads(messages[1][1])
            calls.append([item['evidence_id'] for item in payload['evidence']])
            return value['cases']['wrong_id']

    @contextmanager
    def factory(**kwargs):
        yield Model()

    result, summary = analyze_batches(context, batches, run_key='existing-run', store=store,
                                      llm_factory=factory, read_timeout=10, method='json_schema',
                                      batch_retries=0, progress=lambda **event: events.append(event))
    validate_analysis(result, context)
    assert calls == [['E447', 'E448']]
    assert summary['resumed_batches'] == 21 and summary['completed_batches'] == 22
    assert sum(event['state'] == 'cached' for event in events) == 21
    assert store.get_completed('existing-run', 22)['methodologies'][0]['evidence'][1]['evidence_id'] == 'E447'


def test_pdf_line_wrap_suggestion_stays_advisory_and_keeps_strict_validation():
    from agent.analysis_validator import suggest_original_quote
    from agent.paper_analyzer import analysis_retry_feedback
    text='These documents are then pro-\ncessed using the same DRM-based screening and\npath construction as in the offline setting.'
    quote=r'These documents are then\\nprocessed using the same DRM-based screening and\\npath construction as in the offline setting.'
    context=context_with_sources(E11=text)
    candidate=make_analysis(quote=quote)
    unchanged=restore_original_quotes(candidate,context)
    with pytest.raises(EvidenceValidationError):validate_analysis(unchanged,context)
    exc=EvidenceValidationError('引用不匹配',evidence_diagnostics(unchanged,context))
    feedback=json.loads(analysis_retry_feedback(exc))
    suggested=feedback['issues'][0]['suggested_original_quote']
    assert suggested==text and suggested in context.sources['E11'].text
    assert suggest_original_quote(quote.replace('DRM','OTHER'),text) is None
    assert suggest_original_quote(quote+' 99%',text) is None
    assert unchanged==candidate


def test_resume_carries_failed_feedback_without_rerunning_14_saved_batches(tmp_path):
    from agent.batch_analysis import analyze_batches,BatchAnalysisFailed
    from storage.analysis_batches import AnalysisBatchStore
    text='These documents are then pro-\ncessed using the same DRM-based screening and\npath construction as in the offline setting.'
    quote=r'These documents are then\\nprocessed using the same DRM-based screening and\\npath construction as in the offline setting.'
    last=context_with_sources(E708=text)
    batches=[context_with_sources(**{f'E{i}':f'Valid saved text for batch {i}.'}) for i in range(1,15)]+[last]
    context=replace(last,sources={key:s for b in batches for key,s in b.sources.items()})
    store=AnalysisBatchStore(tmp_path/'batches.sqlite3');store.prepare('existing','same-fingerprint',batches)
    for i,b in enumerate(batches[:-1],1):
        s=next(iter(b.sources.values()));store.save_completed('existing',i,make_analysis(evidence_id=s.evidence_id,quote=s.text))
    calls=[];repair=False
    class Model:
        def with_structured_output(self,*a,**kw):return self
        def invoke(self,messages):
            calls.append(messages)
            if not repair:return make_analysis(evidence_id='E708',quote=quote)
            error=json.loads(json.loads(messages[-1][1])['validation_error'])
            return make_analysis(evidence_id='E708',quote=error['issues'][0]['suggested_original_quote'])
    @contextmanager
    def factory(**kw):yield Model()
    with pytest.raises(BatchAnalysisFailed):
        analyze_batches(context,batches,run_key='existing',store=store,llm_factory=factory,read_timeout=10,method='json_schema',batch_retries=0)
    assert store.get_feedback('existing',15) and store.get_completed('existing',15) is None
    repair=True
    result,summary=analyze_batches(context,batches,run_key='existing',store=AnalysisBatchStore(store.path),llm_factory=factory,read_timeout=10,method='json_schema',batch_retries=0)
    assert summary['resumed_batches']==14 and len(calls)==3
    validate_analysis(result,context)
    assert store.get_feedback('existing',15) is None
