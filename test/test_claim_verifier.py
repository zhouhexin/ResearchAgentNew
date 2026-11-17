"""只测试契约和恢复逻辑；模拟模型标签不代表真实语义判断能力。"""
from contextlib import contextmanager
from copy import deepcopy
from hashlib import sha256
import json

import fitz
from langchain_core.messages import AIMessage
import pytest

from agent.claim_verifier import (ClaimReview, ReviewError, ReviewPrecheckError, prepare_claim,
                                  review_claim,ReviewQuoteSelection,restore_review_quotes,validate_review_output)
from agent.codex_model import CodexExecutionError
from evaluation.claim_quality import (compare_results, fingerprint, load_annotations, load_dataset,
                                      render_comparison, run_reviews)


def item(key='case-1', statement='The score is 80 percent.'):
    quote = 'The score is 80 percent.'
    return {'sample_id': key, 'paper_key': 'fixture', 'paper_title': 'Fixture paper',
            'statement': statement, 'evidence': [{'evidence_id': 'LOCAL:test', 'page_number': 1,
                'quote': quote, 'context': quote + ' It is measured on Dataset A.'}], 'input_hash': key}


def response_for(payload, verdict='supported'):
    return {'item_id': payload['sample_id'], 'verdict': verdict, 'reason': '给定材料提供了实验分数。',
            'evidence_assessments': [{'evidence_id': evidence['evidence_id'], 'relation': 'supports',
                'reason': '此处给出了实验值。', 'quoted_spans': [evidence['quote']]}
                for evidence in payload['evidence']]}


class FixtureModel:
    def __init__(self, change=None, failure=None, selection=None):
        self.calls = []
        self.change, self.failure = change, failure
        self.selection=selection if selection is not None else []

    def with_structured_output(self, schema, **settings):
        assert schema in (ClaimReview,ReviewQuoteSelection) and settings['include_raw'] is True
        self.schema=schema
        return self

    def invoke(self, messages):
        self.calls.append(deepcopy(messages))
        if self.failure:
            raise self.failure
        if self.schema is ReviewQuoteSelection:
            return {'selected_candidates':self.selection, 'reason':'已逐段核对候选的行尾断词。'}
        payload = json.loads(messages[1][1])
        result = response_for(payload)
        return self.change(result) if self.change else result


def test_model_receives_only_claim_and_all_evidence_not_human_labels():
    original = item()
    original.update(gold_label='contradicted', gold_reason='NEVER_SEND_HUMAN_REASON',
                    sample_kind='controlled_negative', parent_sample_id='hidden-parent', annotation_focus='HIDDEN_FOCUS')
    before = deepcopy(original)
    original['evidence'].append(dict(original['evidence'][0], evidence_id='LOCAL:second'))
    model = FixtureModel()
    review = review_claim(model, original)
    payload = json.loads(model.calls[0][1][1])
    assert set(payload) == {'sample_id', 'paper_title', 'statement', 'evidence'}
    assert len(payload['evidence']) == len(review.evidence_assessments) == 2
    assert 'NEVER_SEND' not in str(model.calls) and 'hidden-parent' not in str(model.calls)
    assert 'HIDDEN_FOCUS' not in str(model.calls) and 'controlled_negative' not in str(model.calls)
    assert original['gold_label'] == before['gold_label']


@pytest.mark.parametrize('change', ['quote', 'empty', 'duplicate', 'budget'])
def test_bad_inputs_never_invoke_model(change):
    source = item()
    model = FixtureModel()
    if change == 'quote': source['evidence'][0]['quote'] = 'Invented 99 percent.'
    if change == 'empty': source['statement'] = '  '
    if change == 'duplicate': source['evidence'].append(deepcopy(source['evidence'][0]))
    with pytest.raises(ReviewPrecheckError):
        review_claim(model, source, max_input_chars=10 if change == 'budget' else 24000)
    assert not model.calls


@pytest.mark.parametrize('change', ['wrong_id', 'verdict', 'ref', 'duplicate', 'missing', 'rewritten', 'blank', 'extra'])
def test_response_cannot_change_ids_or_quotes_or_omit_evidence(change):
    def modify(result):
        if change == 'wrong_id': result['item_id'] = 'other'
        if change == 'verdict': result['verdict'] = 'definitely_true'
        if change == 'ref': result['evidence_assessments'][0]['evidence_id'] = 'E999'
        if change == 'duplicate': result['evidence_assessments'] *= 2
        if change == 'missing': result['evidence_assessments'] = []
        if change == 'rewritten': result['evidence_assessments'][0]['quoted_spans'] = ['The score is 99 percent.']
        if change == 'blank': result['reason'] = '   '
        if change == 'extra': result['new_statement'] = 'rewritten claim'
        return result
    with pytest.raises(ReviewError):
        review_claim(FixtureModel(change=modify), item())


def test_unique_whitespace_difference_restores_actual_source_without_changing_verdict():
    source = item()
    source['evidence'][0].update(quote='The score is 80\npercent.', context='The score is 80\npercent. Dataset A.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = ['The score is 80 percent.']
        return result
    review = review_claim(FixtureModel(change=modify), source)
    assert review.verdict == 'supported'
    assert review.evidence_assessments[0].quoted_spans == ['The score is 80\npercent.']
    assert review._quote_repairs[0]['model_quote'] == 'The score is 80 percent.'
    assert review._original_model_review['verdict'] == review.verdict
    assert '_quote_repairs' not in ClaimReview.model_json_schema()['properties']


@pytest.mark.parametrize('change', ['number', 'word', 'case', 'punctuation', 'hyphen', 'foreign_id'])
def test_whitespace_restoration_does_not_accept_content_changes(change):
    source = item()
    span = {'number': 'The score is 99 percent.', 'word': 'The result is 80 percent.',
            'case': 'THE score is 80 percent.', 'punctuation': 'The score is 80 percent!',
            'hyphen': 'The method uses multi-step retrieval.', 'foreign_id': 'The score is 80 percent.'}[change]
    if change == 'hyphen':
        source['evidence'][0].update(quote='The method uses multi-\nstep retrieval.', context='The method uses multi-\nstep retrieval.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = [span]
        if change == 'foreign_id': result['evidence_assessments'][0]['evidence_id'] = 'LOCAL:other'
        return result
    with pytest.raises(ReviewError) as failure:
        review_claim(FixtureModel(change=modify), source)
    assert failure.value.diagnostics['unverified_candidate_review']['evidence_assessments'][0]['quoted_spans'] == [span]


def test_ambiguous_whitespace_matches_remain_failed():
    source = item()
    source['evidence'][0].update(quote='The score is\n80 percent.',
                                context='The score is\n80 percent. The score is 80\npercent.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = ['The score is 80 percent.']
        return result
    with pytest.raises(ReviewError) as failure:
        review_claim(FixtureModel(change=modify), source)
    assert failure.value.diagnostics['issues'][0]['reason'] == 'quote_mismatch'


@pytest.mark.parametrize('selected',[['Q1'],[],['Q1','Q1'],['Q99']])
def test_pdf_hyphenated_word_requires_explicit_candidate_confirmation(selected):
    source=item()
    original='The ar-\nchitecture consists of three components.'
    source['evidence'][0].update(quote=original,context=original)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['The architecture consists of three components.']
        return result
    model=FixtureModel(change=modify,selection=selected)
    if selected!=['Q1']:
        with pytest.raises(ReviewError):review_claim(model,source)
    else:
        review=review_claim(model,source)
        assert review.evidence_assessments[0].quoted_spans==[original]
        assert review.verdict=='supported'
        assert review._quote_repairs[0]['reason']=='model_confirmed_line_end_hyphenation'
        assert review._layout_confirmation['status']=='confirmed'
    assert len(model.calls)==2
    payload=json.loads(model.calls[-1][1][1])
    assert set(payload)=={'sample_id','candidates'} and payload['candidates'][0]['original_quote']==original
    assert payload['candidates'][0]['alignment']['source_form']==payload['candidates'][0]['alignment']['model_form']


@pytest.mark.parametrize('original,model_quote',[
    ('The ar-\nchitecture consists of three components.','The architecture consists of three components.'),
    ('For augmenta-\ntion density, we compare retrieval models.','For augmen-\ntation density, we compare retrieval models.'),
])
def test_saved_hyphenated_candidate_only_calls_selection_not_classifier(tmp_path,original,model_quote):
    source=item()
    source['evidence'][0].update(quote=original,context=original)
    candidate=response_for(prepare_claim(source).model_dump())
    candidate['evidence_assessments'][0]['quoted_spans']=[model_quote]
    error=ReviewError('旧引用不匹配',{'unverified_candidate_review':candidate})
    @contextmanager
    def first():yield FixtureModel(failure=error)
    run_dir,_=run_reviews([source],first,tmp_path,{})
    model=FixtureModel(selection=['Q1'])
    @contextmanager
    def confirm():yield model
    _,results=run_reviews([source],confirm,tmp_path,{})
    assert len(model.calls)==1
    assert 'candidates' in json.loads(model.calls[0][1][1])
    assert results[0]['review']['verdict']==candidate['verdict']
    assert results[0]['review']['reason']==candidate['reason']
    assert results[0]['layout_confirmation']['status']=='confirmed'
    assert json.loads((run_dir/'quote_selections/case-1.json').read_text())['status']=='confirmed'


def test_ordinary_hyphens_and_numbers_cannot_use_layout_confirmation():
    source=item()
    source['evidence'][0].update(quote='The re-sign operation scores 80 percent.',context='The re-sign operation scores 80 percent.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['The resign operation scores 99 percent.']
        return result
    model=FixtureModel(change=modify,selection=['Q1'])
    with pytest.raises(ReviewError):review_claim(model,source)
    assert len(model.calls)==1


@pytest.mark.parametrize('original,model_quote',[
    ('We assess per-\nformance at 80 percent.','We assess performance at 80 percent.'),
    ('The multi-\nstep architecture has three components.','The multi-step architecture has three components.'),
])
def test_layout_confirmation_aligns_both_joined_words_and_retained_hyphens(original,model_quote):
    source=item()
    source['evidence'][0].update(quote=original,context=original)
    source.update(gold_label='contradicted',gold_reason='DO_NOT_SEND_LABEL_REASON')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=[model_quote]
        return result
    model=FixtureModel(change=modify,selection=['Q1'])
    review=review_claim(model,source)
    assert review.evidence_assessments[0].quoted_spans==[original]
    assert review._layout_confirmation['confirmation_reason']
    assert 'DO_NOT_SEND_LABEL_REASON' not in str(model.calls)


@pytest.mark.parametrize('original,model_quote',[
    ('This aug-\nmentation can achieve per-\nformance with smaller models.',
     'This augmentation can achieve per-\nformance with smaller models.'),
    ('For augmenta-\ntion density, we compare retrieval models.',
     'For augmen-\ntation density, we compare retrieval models.'),
    ('This aug-\nmentation can achieve per-\nformance with smaller models.',
     r'This augmentation can achieve per-\nformance with smaller models.'),
    ('This aug-\nmentation can achieve per-\nformance with smaller models.',
     r'This augmentation can achieve per-\\nformance with smaller models.'),
    ('The augmentation improves retrieval quality.',
     'The augmen-\ntation improves retrieval quality.'),
])
def test_relocated_or_partially_joined_line_end_words_require_confirmation(original,model_quote):
    source=item()
    source['evidence'][0].update(quote=original,context=original)
    candidate=response_for(prepare_claim(source).model_dump())
    candidate['verdict']='insufficient'
    candidate['evidence_assessments'][0]['quoted_spans']=[model_quote]
    parsed=ClaimReview.model_validate(candidate)
    with pytest.raises(ReviewError):
        validate_review_output(restore_review_quotes(parsed,prepare_claim(source)),prepare_claim(source))
    model=FixtureModel(change=lambda _:deepcopy(candidate),selection=['Q1'])
    review=review_claim(model,source)
    assert len(model.calls)==2
    assert review.evidence_assessments[0].quoted_spans==[original]
    assert review.verdict=='insufficient' and review.reason==candidate['reason']
    assert review._layout_confirmation['policy']=='model-confirmed-layout-v3'
    alignment=json.loads(model.calls[-1][1][1])['candidates'][0]['alignment']
    assert alignment['source_form']==alignment['model_form']


@pytest.mark.parametrize('model_quote',[
    'The augmentation improves pseudoqueries by 10 percent.',
    'The augmentation improves pseudo-queries by 99 percent.',
    'The augmentation decreases pseudo-queries by 10 percent.',
    'The Augmentation improves pseudo-queries by 10 percent.',
])
def test_symmetric_layout_matching_cannot_hide_ordinary_hyphen_number_word_or_case_changes(model_quote):
    source=item()
    original='The augmen-\ntation improves pseudo-queries by 10 percent.'
    source['evidence'][0].update(quote=original,context=original)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=[model_quote]
        return result
    model=FixtureModel(change=modify,selection=['Q1'])
    with pytest.raises(ReviewError):review_claim(model,source)
    assert len(model.calls)==1


def test_symmetric_layout_matching_rejects_multiple_original_candidates():
    source=item()
    first='This aug-\nmentation improves retrieval quality.'
    second='This augmenta-\ntion improves retrieval quality.'
    source['evidence'][0].update(quote=first,context=first+' '+second)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['This augmentation improves retrieval quality.']
        return result
    model=FixtureModel(change=modify,selection=['Q1'])
    with pytest.raises(ReviewError):review_claim(model,source)
    assert len(model.calls)==1


@pytest.mark.parametrize('response',[
    {'selected_candidates':['Q1']},
    {'selected_candidates':['Q1'],'reason':'  '},
    {'selected_candidates':[],'reason':'无法确认这个单词的排版还原。'},
])
def test_layout_confirmation_requires_reason_and_keeps_rejection_diagnostics(response):
    source=item()
    original='The ar-\nchitecture consists of three components.'
    source['evidence'][0].update(quote=original,context=original)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['The architecture consists of three components.']
        return result
    class SelectionResponse(FixtureModel):
        def invoke(self,messages):
            if self.schema is ReviewQuoteSelection:
                return response
            return super().invoke(messages)
    with pytest.raises(ReviewError) as failure:
        review_claim(SelectionResponse(change=modify),source)
    diagnostic=failure.value.diagnostics['layout_confirmation']
    assert diagnostic['status']=='unconfirmed'
    if response['selected_candidates']==[]:
        assert diagnostic['confirmation_reason']==response['reason']
        assert response['reason'] in str(failure.value)


def test_layout_confirmation_connection_failure_preserves_candidate_for_resume(tmp_path):
    source=item()
    original='The ar-\nchitecture consists of three components.'
    source['evidence'][0].update(quote=original,context=original)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['The architecture consists of three components.']
        return result
    class InterruptedSelection(FixtureModel):
        def invoke(self,messages):
            if self.schema is ReviewQuoteSelection:
                raise ConnectionError('fixture connection interrupted')
            return super().invoke(messages)
    @contextmanager
    def interrupted():yield InterruptedSelection(change=modify)
    _,failed=run_reviews([source],interrupted,tmp_path,{})
    assert failed[0]['status']=='failed'
    assert failed[0]['pending_candidate_review']['evidence_assessments'][0]['quoted_spans']==['The architecture consists of three components.']
    model=FixtureModel(selection=['Q1'])
    @contextmanager
    def resumed():yield model
    _,results=run_reviews([source],resumed,tmp_path,{})
    assert len(model.calls)==1 and results[0]['status']=='succeeded'
    assert 'pending_candidate_review' not in results[0]


@pytest.mark.parametrize('failure',['refusal','truncated','wrong_tool'])
def test_layout_selection_refuses_invalid_response_even_with_valid_candidate_ids(failure):
    source=item()
    original='The ar-\nchitecture consists of three components.'
    source['evidence'][0].update(quote=original,context=original)
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans']=['The architecture consists of three components.']
        return result
    class InvalidSelection(FixtureModel):
        def invoke(self,messages):
            if self.schema is not ReviewQuoteSelection:
                return super().invoke(messages)
            raw=AIMessage(content=json.dumps({'selected_candidates':['Q1']}))
            parsed=ReviewQuoteSelection(selected_candidates=['Q1'],reason='排版断词一致。')
            if failure=='refusal':raw.additional_kwargs['refusal']='fixture refusal'
            if failure=='truncated':raw.response_metadata['finish_reason']='length'
            if failure=='wrong_tool':
                parsed=None
                raw.tool_calls=[{'id':'fixture','name':'ForeignSelection','args':{'selected_candidates':['Q1']}}]
            return {'raw':raw,'parsed':parsed}
    with pytest.raises(ReviewError):review_claim(InvalidSelection(change=modify),source)


@pytest.mark.parametrize('escaped', [r'\n', r'\\n', r'\r\n', r'\t'])
def test_literal_whitespace_escapes_restore_only_actual_unique_source(escaped):
    source = item()
    original = 'The score is 80\npercent.'
    source['evidence'][0].update(quote=original, context=original)
    candidate = 'The score is 80'+escaped+'percent.'
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = [candidate]
        return result
    review = review_claim(FixtureModel(change=modify), source)
    assert review.evidence_assessments[0].quoted_spans == [original]
    assert review._quote_repairs[0]['model_quote'] == candidate
    assert review._quote_repairs[0]['escape_layers'] in (1, 2)


@pytest.mark.parametrize('candidate', [r'The score is 80\u000apercent.', r'The score is 80\x0apercent.',
                                      r'The score is 80\\\\npercent.', r'The score is 99\npercent.'])
def test_escape_restoration_rejects_other_encodings_depth_or_changed_numbers(candidate):
    source = item()
    source['evidence'][0].update(quote='The score is 80\npercent.', context='The score is 80\npercent.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = [candidate]
        return result
    with pytest.raises(ReviewError): review_claim(FixtureModel(change=modify), source)


def test_escape_decoding_does_not_choose_between_literal_and_decoded_locations():
    source = item()
    source['evidence'][0].update(quote='The score is 80\npercent.',
        context='The score is 80\npercent. The score is 80'+r'\n'+'percent.')
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = [r'The score is 80\\npercent.']
        return result
    with pytest.raises(ReviewError): review_claim(FixtureModel(change=modify), source)


@pytest.mark.parametrize('mode', ['json', 'tool', 'truncated', 'refused'])
def test_raw_structured_responses_and_incomplete_output(mode):
    def modify(result):
        if mode == 'tool':
            raw = AIMessage(content='', tool_calls=[{'id': 'call1', 'name': 'ClaimReview', 'args': result}])
        else:
            raw = AIMessage(content='```json\n'+json.dumps(result)+'\n```',
                            response_metadata={'finish_reason': 'length'} if mode == 'truncated' else {},
                            additional_kwargs={'refusal': 'declined'} if mode == 'refused' else {})
        return {'raw': raw, 'parsed': None, 'parsing_error': None}
    model = FixtureModel(change=modify)
    if mode in {'truncated', 'refused'}:
        with pytest.raises(ReviewError): review_claim(model, item())
    else:
        assert review_claim(model, item()).verdict == 'supported'


@pytest.fixture
def dataset(tmp_path):
    path = tmp_path/'dataset'
    path.mkdir()
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 60), 'The score is 80 percent. It is measured on Dataset A.')
        pdf.save(path/'fixture.pdf')
    with fitz.open(path/'fixture.pdf') as pdf:
        text = pdf[0].get_text().strip()
    pdf_hash = sha256((path/'fixture.pdf').read_bytes()).hexdigest()
    text_hash = sha256(text.encode()).hexdigest()
    quote = 'The score is 80 percent.'
    evidence = {'evidence_id': f'LOCAL:{pdf_hash[:12]}:P1:0-{len(quote)}', 'database_evidence_id': None,
                'page_number': 1, 'quote': quote, 'context': text, 'start_char': 0, 'end_char': len(quote),
                'page_start_char': 0, 'page_end_char': len(quote), 'context_start_char': 0,
                'context_end_char': len(text), 'context_scope': 'entire_pdf_page',
                'pdf_sha256': pdf_hash, 'content_sha256': text_hash, 'source_url': 'fixture.pdf'}
    inputs = []
    for number in range(1, 4):
        data = dict(item(f'case-{number}'), evidence=[deepcopy(evidence)])
        data.pop('input_hash')
        data['input_hash'] = fingerprint(data)
        inputs.append(data)
    source = {'paper_key': 'fixture', 'title': 'Fixture paper', 'pdf_file': 'fixture.pdf',
              'pdf_sha256': pdf_hash, 'content_sha256': text_hash, 'page_count': 1, 'split': 'development'}
    manifest = {'dataset_id': 'fixture-only', 'samples': len(inputs), 'pilot_ids': ['case-1', 'case-2'],
                'reviewer_inputs_sha256': fingerprint(inputs), 'sources': [source]}
    (path/'manifest.json').write_text(json.dumps(manifest))
    source = dict(source, text=text, pages={'1': {'text': text, 'start_char': 0, 'end_char': len(text)}})
    (path/'source_snapshots.json').write_text(json.dumps({'fixture': source}))
    (path/'reviewer_inputs.jsonl').write_text(''.join(json.dumps(data)+'\n' for data in inputs))
    annotations = {'dataset_id': manifest['dataset_id'], 'reviewer_inputs_sha256': manifest['reviewer_inputs_sha256'],
        'annotations': [{'sample_id': row['sample_id'], 'input_hash': row['input_hash'],
            'annotation_status': 'reviewed', 'gold_label': 'supported', 'gold_reason': 'HUMAN_ONLY',
            'annotator': 'fixture annotator', 'annotated_at': '2026-11-13T00:00:00Z'} for row in inputs]}
    (path/'annotations').mkdir()
    (path/'annotations/pilot_round_001.json').write_text(json.dumps(annotations))
    return path, inputs, manifest, annotations


def test_dataset_precheck_reads_real_pdf_and_defaults_to_pilot_only(dataset):
    path, inputs, manifest, _ = dataset
    _, chosen = load_dataset(path)
    assert [row['sample_id'] for row in chosen] == manifest['pilot_ids']
    assert inputs[-1]['sample_id'] not in {row['sample_id'] for row in chosen}


def test_explicit_development_split_selects_all_development_samples(dataset):
    path, inputs, _, _ = dataset
    _, chosen = load_dataset(path, split='development')
    assert [row['sample_id'] for row in chosen] == [row['sample_id'] for row in inputs]
    with pytest.raises(ReviewPrecheckError): load_dataset(path, ['case-1'], split='development')
    with pytest.raises(ReviewPrecheckError): load_dataset(path, split='validation_reserved')


@pytest.mark.parametrize('change', ['pdf', 'text', 'input_hash', 'gold_in_input', 'offset', 'path_escape'])
def test_dataset_rejects_changed_or_forged_material_before_model(dataset, change):
    path, inputs, manifest, _ = dataset
    if change == 'pdf': (path/'fixture.pdf').write_bytes(b'%PDF-forged')
    if change in {'text', 'path_escape'}:
        sources = json.loads((path/'source_snapshots.json').read_text())
        if change == 'text': sources['fixture']['text'] = 'forged'
        else:
            sources['fixture']['pdf_file'] = '../outside.pdf'
            manifest['sources'][0]['pdf_file'] = '../outside.pdf'
        (path/'source_snapshots.json').write_text(json.dumps(sources))
    if change in {'input_hash', 'gold_in_input', 'offset'}:
        if change == 'input_hash': inputs[0]['statement'] = 'changed'
        if change == 'gold_in_input': inputs[0]['gold_label'] = 'supported'
        if change == 'offset': inputs[0]['evidence'][0]['start_char'] = -1
        if change != 'input_hash':
            value = {key: value for key, value in inputs[0].items() if key != 'input_hash'}
            inputs[0]['input_hash'] = fingerprint(value)
        manifest['reviewer_inputs_sha256'] = fingerprint(inputs)
        (path/'reviewer_inputs.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in inputs))
    (path/'manifest.json').write_text(json.dumps(manifest))
    with pytest.raises(ReviewPrecheckError): load_dataset(path)


def test_annotations_are_versioned_and_only_confirmed_selected_labels_are_scored(dataset):
    path, inputs, manifest, annotations = dataset
    chosen = inputs[:2]
    annotations['annotations'][0]['annotation_status'] = 'disputed'
    file = path/'annotations.json'
    file.write_text(json.dumps(annotations))
    labels = load_annotations(file, manifest, chosen)
    assert set(labels) == {'case-2'}
    annotations['annotations'][1]['input_hash'] = 'wrong'
    file.write_text(json.dumps(annotations))
    with pytest.raises(ReviewPrecheckError): load_annotations(file, manifest, chosen)


def test_partial_failure_then_resume_reuses_success_and_retries_failed_only(tmp_path):
    items = [item('case-'+str(i)) for i in range(1, 4)]
    models = [FixtureModel(), FixtureModel(failure=CodexExecutionError('CODEX_PERMISSION_DENIED', '权限失败', reason='permission'))]
    @contextmanager
    def first_factory(): yield models.pop(0)
    run_dir, first = run_reviews(items, first_factory, tmp_path, {'model': 'fixture'})
    assert [row['status'] for row in first] == ['succeeded', 'failed', 'not_attempted']
    model = FixtureModel()
    @contextmanager
    def second_factory(): yield model
    resumed_dir, resumed = run_reviews(items, second_factory, tmp_path, {'model': 'fixture'})
    assert run_dir == resumed_dir and len(model.calls) == 2
    assert resumed[0]['resumed'] and all(row['status'] == 'succeeded' for row in resumed)
    assert 'gold_label' not in (run_dir/'requests/case-1.json').read_text()
    new_dir, _ = run_reviews(items, second_factory, tmp_path, {'model': 'changed-fixture'})
    assert new_dir != run_dir and len(model.calls) == 5


def test_corrupt_cached_quote_is_revalidated_and_requested_again(tmp_path):
    model = FixtureModel()
    @contextmanager
    def factory(): yield model
    run_dir, _ = run_reviews([item()], factory, tmp_path, {})
    file = run_dir/'items/case-1.json'
    cached = json.loads(file.read_text())
    cached['review']['evidence_assessments'][0]['quoted_spans'] = ['Invented source']
    file.write_text(json.dumps(cached))
    _, results = run_reviews([item()], factory, tmp_path, {})
    assert len(model.calls) == 2 and results[0]['status'] == 'succeeded' and not results[0]['resumed']


def test_failed_quote_is_saved_as_unverified_diagnostic_not_scored_result(tmp_path):
    def modify(result):
        result['evidence_assessments'][0]['quoted_spans'] = ['An invented source sentence.']
        return result
    model = FixtureModel(change=modify)
    @contextmanager
    def factory(): yield model
    run_dir, results = run_reviews([item()], factory, tmp_path, {})
    record = json.loads((run_dir/'items/case-1.json').read_text())
    assert record['status'] == 'failed' and 'review' not in record
    assert record['error']['diagnostic']['issues'][0]['quoted_span'] == 'An invented source sentence.'
    assert 'unverified_candidate_review' in record['error']['diagnostic']
    report = compare_results([item()], results, {'case-1': {'gold_label': 'supported', 'gold_reason': 'human'}})
    assert report['compared'] == 0 and report['details'][0]['model_label'] is None


def test_saved_failed_candidate_recovers_without_call_or_changing_verdict(tmp_path):
    source = item()
    original = 'The score is 80\npercent.'
    source['evidence'][0].update(quote=original, context=original)
    candidate = response_for(prepare_claim(source).model_dump(), verdict='contradicted')
    candidate['evidence_assessments'][0]['quoted_spans'] = [r'The score is 80\npercent.']
    error = ReviewError('旧版本无法恢复转义', {'unverified_candidate_review': candidate})
    @contextmanager
    def first(): yield FixtureModel(failure=error)
    run_dir, results = run_reviews([source], first, tmp_path, {})
    assert results[0]['status'] == 'failed'
    @contextmanager
    def never():
        pytest.fail('恢复已保存结果不能再次请求模型')
        yield
    _, restored = run_reviews([source], never, tmp_path, {})
    record = restored[0]
    assert record['status'] == 'succeeded' and record['recovered_from_diagnostic']
    assert record['attempts'] == 1 and record['recovery_model_calls'] == 0
    assert record['review']['verdict'] == 'contradicted'
    assert record['original_model_review'] == candidate
    assert len(list((run_dir/'history/case-1').glob('*.json'))) == 1
    report = compare_results([source], restored, {'case-1': {'gold_label': 'insufficient', 'gold_reason': 'human'}})
    assert report['compared'] == 1 and report['accuracy'] == 0


def test_saved_candidate_with_mismatching_request_cannot_be_recovered(tmp_path):
    source = item()
    candidate = response_for(prepare_claim(source).model_dump())
    error = ReviewError('fixture', {'unverified_candidate_review': candidate})
    @contextmanager
    def first(): yield FixtureModel(failure=error)
    run_dir, _ = run_reviews([source], first, tmp_path, {})
    file=run_dir/'requests/case-1.json'
    request=json.loads(file.read_text());request['input']['statement']='changed request'
    file.write_text(json.dumps(request))
    model=FixtureModel()
    @contextmanager
    def retry(): yield model
    _, results=run_reviews([source], retry, tmp_path, {})
    assert len(model.calls)==1 and not results[0].get('recovered_from_diagnostic')


def test_changed_material_cannot_reuse_cache_even_if_caller_forgets_input_hash(tmp_path):
    model = FixtureModel()
    @contextmanager
    def factory(): yield model
    source = item()
    first_dir, _ = run_reviews([source], factory, tmp_path, {})
    changed = dict(source, statement='A different research claim.')
    second_dir, _ = run_reviews([changed], factory, tmp_path, {})
    assert second_dir != first_dir and len(model.calls) == 2


def test_larger_scope_reuses_existing_successes_without_new_calls_for_them(tmp_path):
    model = FixtureModel()
    @contextmanager
    def factory(): yield model
    items = [item('case-'+str(i)) for i in range(1, 4)]
    initial, _ = run_reviews(items[:2], factory, tmp_path, {'model': 'fixture'})
    assert len(model.calls) == 2
    expanded, results = run_reviews(items, factory, tmp_path, {'model': 'fixture'})
    assert initial != expanded and len(model.calls) == 3
    assert [row['resumed'] for row in results] == [True, True, False]
    assert all(row['reused_from_run_key'] == json.loads((initial/'run.json').read_text())['run_key'] for row in results[:2])
    assert json.loads((expanded/'requests/case-1.json').read_text())['reused_from_run_key']
    run_reviews(items, factory, tmp_path, {'model': 'fixture'})
    assert len(model.calls) == 3


def test_scope_reuse_rejects_different_configuration_and_modified_request(tmp_path):
    model = FixtureModel()
    @contextmanager
    def factory(): yield model
    items = [item('case-'+str(i)) for i in range(1, 4)]
    initial, _ = run_reviews(items[:2], factory, tmp_path, {'model': 'fixture'})
    request = initial/'requests/case-1.json'
    data = json.loads(request.read_text());data['system'] = 'changed instruction'
    request.write_text(json.dumps(data))
    _, results = run_reviews(items, factory, tmp_path, {'model': 'fixture'})
    assert [row['resumed'] for row in results] == [False, True, False]
    assert len(model.calls) == 4
    _, results = run_reviews(items, factory, tmp_path, {'model': 'other-model'})
    assert not any(row['resumed'] for row in results) and len(model.calls) == 7


def test_failed_calls_are_not_semantic_needs_review_or_100_percent_accuracy():
    inputs = [item('one'), item('two'), item('three')]
    labels = {'one': {'gold_label': 'supported', 'gold_reason': 'human'},
              'two': {'gold_label': 'contradicted', 'gold_reason': 'human'},
              'three': {'gold_label': 'insufficient', 'gold_reason': 'human'}}
    results = [{'sample_id': 'one', 'status': 'failed', 'error': {'code': 'MODEL_TIMEOUT', 'message': 'timeout'}},
               {'sample_id': 'two', 'status': 'not_attempted'}, {'sample_id': 'three', 'status': 'not_attempted'}]
    report = compare_results(inputs, results, labels)
    assert report['accuracy'] is None and report['compared'] == 0 and report['completion_rate'] == 0
    assert report['needs_review_count'] == 0 and report['supported_precision'] is None
    assert report['macro_f1_all_four_classes'] is None and report['false_supported']['rate'] is None
    assert '不可计算' in render_comparison(report)


def test_scores_show_false_supported_and_missing_classes():
    items = [item('one'), item('two'), item('three')]
    labels = {key: {'gold_label': value, 'gold_reason': 'human'} for key, value in
              [('one', 'supported'), ('two', 'contradicted'), ('three', 'needs_review')]}
    results = [{'sample_id': key, 'status': 'succeeded', 'review': {'verdict': value, 'reason': 'fixture'}}
               for key, value in [('one', 'supported'), ('two', 'supported'), ('three', 'needs_review')]]
    report = compare_results(items, results, labels)
    assert report['accuracy'] == pytest.approx(2/3) and report['supported_precision'] == .5
    assert report['false_supported'] == {'count': 1, 'eligible_compared': 1, 'rate': 1., 'sample_ids': ['two']}
    assert report['missing_human_classes'] == ['insufficient']
    assert report['macro_f1_all_four_classes'] is None


def test_cli_check_only_never_opens_model(dataset, monkeypatch, capsys):
    import run_claim_evaluation as cli
    path, _, _, _ = dataset
    monkeypatch.setattr(cli, 'analysis_llm', lambda **kwargs: pytest.fail('模型不应被调用'))
    assert cli.main(['--dataset', str(path), '--check-only']) == 0
    assert json.loads(capsys.readouterr().out)['selected_samples'] == 2


def test_default_pilot_requires_all_selected_human_labels_before_requests(dataset, monkeypatch):
    import run_claim_evaluation as cli
    path, _, _, annotations = dataset
    annotations['annotations'][0]['annotation_status'] = 'unreviewed'
    (path/'annotations/pilot_round_001.json').write_text(json.dumps(annotations))
    monkeypatch.setattr(cli, 'analysis_llm', lambda **kwargs: pytest.fail('模型不应被调用'))
    with pytest.raises(ReviewPrecheckError, match='尚未全部人工确认'):
        cli.main(['--dataset', str(path)])


def test_cli_fake_model_end_to_end_report_and_second_run_without_new_calls(dataset, monkeypatch, capsys):
    import run_claim_evaluation as cli
    path, _, _, _ = dataset
    model = FixtureModel()
    @contextmanager
    def factory(**kwargs): yield model
    monkeypatch.setattr(cli, 'analysis_llm', factory)
    monkeypatch.setattr(cli, 'validate_model_configuration', lambda: None)
    assert cli.main(['--dataset', str(path), '--timeout', '5']) == 0
    assert len(model.calls) == 2
    assert cli.main(['--dataset', str(path), '--timeout', '5']) == 0
    assert len(model.calls) == 2 and '复用已核验评审' in capsys.readouterr().out
    reports = list((path/'review_runs').glob('*/comparison.json'))
    assert len(reports) == 1
    report = json.loads(reports[0].read_text())
    assert report['compared'] == 2 and report['accuracy'] == 1
    assert 'HUMAN_ONLY' not in str(model.calls)


def test_cli_uses_current_labels_expands_scope_reuses_cache_and_preserves_old_scores(dataset, monkeypatch):
    import run_claim_evaluation as cli
    path, _, _, annotations = dataset
    model = FixtureModel()
    @contextmanager
    def factory(**kwargs): yield model
    monkeypatch.setattr(cli, 'analysis_llm', factory)
    monkeypatch.setattr(cli, 'validate_model_configuration', lambda: None)
    assert cli.main(['--dataset', str(path), '--timeout', '5']) == 0
    assert len(model.calls) == 2
    latest = path/'annotations/round2.json'
    latest.write_text(json.dumps(annotations))
    (path/'annotation_progress.json').write_text(json.dumps({'source_annotations': 'annotations/round2.json'}))
    args = ['--dataset', str(path), '--timeout', '5', '--split', 'development']
    assert cli.main(args) == 0 and len(model.calls) == 3
    reports = [p for p in (path/'review_runs').glob('*/comparison.json') if json.loads(p.read_text())['selected'] == 3]
    assert len(reports) == 1
    annotations['annotations'][0].update(gold_label='contradicted',gold_reason='Revised human label')
    annotations['evaluation_context'] = {'model_influenced_labels': True, 'note': 'POST_ADJUDICATION_NOTE'}
    latest.write_text(json.dumps(annotations))
    assert cli.main(args) == 0 and len(model.calls) == 3
    report = json.loads(reports[0].read_text())
    assert report['accuracy'] == pytest.approx(2/3)
    assert report['annotation_context']['model_influenced_labels'] is True
    assert '改标后的结果范围' in reports[0].with_suffix('.md').read_text()
    assert len(list((reports[0].parent/'comparisons').glob('*.json'))) == 2
    assert 'HUMAN_ONLY' not in str(model.calls) and 'Revised human label' not in str(model.calls)
    assert 'POST_ADJUDICATION_NOTE' not in str(model.calls)


def test_progress_annotation_path_cannot_escape_dataset(dataset, monkeypatch):
    import run_claim_evaluation as cli
    path, _, _, _ = dataset
    (path/'annotation_progress.json').write_text(json.dumps({'source_annotations': '../outside.json'}))
    monkeypatch.setattr(cli, 'analysis_llm', lambda **kwargs: pytest.fail('不应请求模型'))
    with pytest.raises(ReviewPrecheckError,match='来源路径超出'):
        cli.main(['--dataset', str(path), '--check-only'])


def test_validation_split_uses_its_frozen_labels_and_validation_report_scope(dataset, monkeypatch):
    import run_claim_evaluation as cli
    path, _, manifest, annotations = dataset
    manifest['sources'][0]['split'] = 'validation_reserved'
    (path/'manifest.json').write_text(json.dumps(manifest))
    sources=json.loads((path/'source_snapshots.json').read_text())
    sources['fixture']['split']='validation_reserved'
    (path/'source_snapshots.json').write_text(json.dumps(sources))
    labels=path/'annotations/validation.json'
    labels.write_text(json.dumps(dict(annotations,evaluation_context={'kind':'held_out_paper_validation','model_influenced_labels':False})))
    (path/'annotation_progress.json').write_text(json.dumps({'source_annotations':'missing-development.json','split_annotations':{'validation_reserved':'annotations/validation.json'}}))
    model=FixtureModel()
    @contextmanager
    def factory(**kwargs):yield model
    monkeypatch.setattr(cli,'analysis_llm',factory)
    monkeypatch.setattr(cli,'validate_model_configuration',lambda:None)
    assert cli.main(['--dataset',str(path),'--split','validation_reserved','--timeout','5'])==0
    report_path=next((path/'review_runs').glob('*/comparison.json'))
    report=json.loads(report_path.read_text())
    assert report['scope']=='held_out_paper_validation' and report['compared']==3
    assert report['annotation_context']['model_influenced_labels'] is False
    assert '留出论文验证' in report_path.with_suffix('.md').read_text()
    annotations['annotations'][0].update(gold_label='contradicted',gold_reason='USER_CONFIRMED_POST_MODEL')
    annotations['evaluation_context']={'kind':'post_model_review_human_adjudication','model_influenced_labels':True}
    labels.write_text(json.dumps(annotations))
    calls_before=len(model.calls)
    assert cli.main(['--dataset',str(path),'--split','validation_reserved','--timeout','5'])==0
    assert len(model.calls)==calls_before
    revised=json.loads(report_path.read_text())
    assert revised['scope']=='held_out_paper_post_model_adjudication'
    assert revised['accuracy']==pytest.approx(2/3)
    assert '留出论文人工复核对照' in report_path.with_suffix('.md').read_text()
    assert '复核后的标签一致率' in report_path.with_suffix('.md').read_text()
    archived=[json.loads(p.read_text()) for p in (report_path.parent/'comparisons').glob('*.json')]
    assert len(archived)==2
    original=next(r for r in archived if not r['annotation_context']['model_influenced_labels'])
    assert original['scope']=='held_out_paper_validation' and original['accuracy']==1
    assert 'USER_CONFIRMED_POST_MODEL' not in str(model.calls)
