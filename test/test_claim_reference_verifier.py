"""编号协议保证引用来自后端原文，模拟评审不代表模型语义能力。"""
from contextlib import contextmanager
from copy import deepcopy
import json

from langchain_core.messages import AIMessage
import pytest

from agent.claim_verifier import prepare_claim, ReviewPrecheckError
from pydantic import ValidationError
from test.test_claim_verifier import dataset
from agent.claim_reference_verifier import (QUOTE_PROTOCOL, SPAN_POLICY, ReferenceClaimReview, ReferenceReviewError,
    source_spans, reference_payload, response_schema, review_claim_references)
from evaluation.claim_quality import run_reviews, checked_cache, review_run_key


def item():
    context='  The augmenta-\ntion improves retrieval.\nNumbers remain 8.8M and 0.93 seconds.\n'
    return {'sample_id':'case-1','paper_title':'Test paper','statement':'待核对的完整结论',
            'input_hash':'source-hash','evidence':[{'evidence_id':'E1','page_number':1,
            'quote':'The augmenta-\ntion improves retrieval.','context':context}]}


class Model:
    def __init__(self,change=None):self.calls=[];self.change=change
    def with_structured_output(self,schema,**kwargs):
        assert issubclass(schema,ReferenceClaimReview) and kwargs['include_raw']
        self.schema=schema
        return self
    def invoke(self,messages):
        self.calls.append(deepcopy(messages))
        payload=json.loads(messages[1][1])
        result={'item_id':payload['sample_id'],'verdict':'insufficient','reason':'未提供全部所需条件。',
            'evidence_assessments':[{'evidence_id':e['evidence_id'],'relation':'insufficient','reason':'材料覆盖范围有限。',
                'span_ids':[e['spans'][0]['span_id']]} for e in payload['evidence']]}
        return self.change(result) if self.change else result


def test_model_returns_only_ids_backend_preserves_pdf_bytes_without_quote_repair():
    source=item();source.update(gold_label='supported',gold_reason='HUMAN_LABEL_SECRET',sample_kind='NEGATIVE_KIND_SECRET')
    model=Model();review=review_claim_references(model,source)
    quote=review.evidence_assessments[0].quoted_spans[0]
    assert quote==source['evidence'][0]['context']
    assert 'augmenta-\ntion' in quote and '0.93 seconds' in quote
    assert review.verdict=='insufficient' and review._quote_repairs==[]
    assert len(model.calls)==1
    payload=json.loads(model.calls[0][1][1])
    assert set(payload)=={'sample_id','paper_title','statement','evidence'}
    assert 'HUMAN_LABEL_SECRET' not in str(model.calls) and 'NEGATIVE_KIND_SECRET' not in str(model.calls)
    schema=ReferenceClaimReview.model_json_schema()
    assert 'quoted_spans' not in str(schema) and 'span_ids' in str(schema)


def test_request_schema_limits_ids_to_the_current_sources_and_keeps_sdk_compatibility():
    from langchain_core.utils.function_calling import convert_to_openai_function
    source=item();source['evidence'].append(dict(source['evidence'][0],evidence_id='E2'))
    payload=reference_payload(prepare_claim(source))
    schema=response_schema(payload)
    serialized=convert_to_openai_function(schema,strict=True)['parameters']
    assessment=serialized['properties']['evidence_assessments']['items']['properties']
    assert set(assessment['evidence_id']['enum'])=={'E1','E2'}
    assert set(assessment['span_ids']['items']['enum'])=={'E1:S1','E2:S1'}
    response={'item_id':'case-1','verdict':'supported','reason':'受控结构检查。','evidence_assessments':[
        {'evidence_id':e['evidence_id'],'relation':'supports','reason':'受控结构检查。','span_ids':[e['spans'][0]['span_id']]}
        for e in payload['evidence']]}
    assert schema.model_validate_json(json.dumps(response)).item_id=='case-1'
    response['evidence_assessments'][0]['span_ids']=['unknown']
    with pytest.raises(ValidationError):schema.model_validate(response)


@pytest.mark.parametrize('context',[
    'A full paragraph without any punctuation '+('x'*1500),
    ('A complete sentence.\n'*100)+'最后一句。\n\n',
    'Leading\n\n  paragraph.  Next sentence!\nLong augmen-\ntation words remain intact.\n\n',
    r'Literal \n and \\n escape text with unicode αβ and a backslash \\.',
])
def test_backend_span_directory_covers_every_character_without_cutting_long_sentences(context):
    source=item();source['evidence'][0].update(quote=context,context=context)
    evidence=prepare_claim(source).evidence[0]
    spans=source_spans(evidence)
    assert ''.join(s['text'] for s in spans)==context
    previous=0
    for s in spans:
        assert s['start_char']==previous and context[s['start_char']:s['end_char']]==s['text']
        previous=s['end_char']
    assert previous==len(context)
    if 'x'*1500 in context:assert len(spans)==1
    review=review_claim_references(Model(),source)
    assert all(q in context for a in review.evidence_assessments for q in a.quoted_spans)


@pytest.mark.parametrize('change',['unknown','foreign','duplicate','wrong_item','missing','duplicate_evidence','text','bad_label','blank_reason'])
def test_invalid_ids_generated_quote_text_and_incomplete_assessments_are_rejected(change):
    source=item();source['evidence'].append(dict(source['evidence'][0],evidence_id='E2'))
    def modify(result):
        a=result['evidence_assessments'][0]
        if change=='unknown':a['span_ids']=['E1:S999']
        if change=='foreign':a['span_ids']=['E2:S1']
        if change=='duplicate':a['span_ids']*=2
        if change=='wrong_item':result['item_id']='other'
        if change=='missing':result['evidence_assessments'].pop()
        if change=='duplicate_evidence':result['evidence_assessments'][1]['evidence_id']='E1'
        if change=='text':a['quoted_spans']=['The augmentation improves retrieval.']
        if change=='bad_label':result['verdict']='definitely supported'
        if change=='blank_reason':a['reason']='  '
        return result
    with pytest.raises(ReferenceReviewError):review_claim_references(Model(modify),source)


@pytest.mark.parametrize('kind',['parsed','json','tool','truncated','refused'])
def test_reference_structured_parsing_and_refusal_or_truncation(kind):
    def modify(result):
        raw=AIMessage(content=json.dumps(result))
        parsed=ReferenceClaimReview.model_validate(result) if kind=='parsed' else None
        if kind=='tool':
            raw=AIMessage(content='',tool_calls=[{'id':'tool-1','name':'ReferenceClaimReview','args':result}])
        if kind=='truncated':raw.response_metadata['finish_reason']='length'
        if kind=='refused':raw.additional_kwargs['refusal']='fixture refusal'
        return {'raw':raw,'parsed':parsed}
    if kind in {'truncated','refused'}:
        with pytest.raises(ReferenceReviewError):review_claim_references(Model(modify),item())
    else:
        assert review_claim_references(Model(modify),item()).verdict=='insufficient'


def test_reference_budget_does_not_discard_material_or_invoke_model():
    claim=prepare_claim(item());source_size=len(json.dumps(claim.model_dump(),ensure_ascii=False))
    assert len(json.dumps(reference_payload(claim),ensure_ascii=False))>source_size
    model=Model()
    with pytest.raises(ReviewPrecheckError):review_claim_references(model,item(),max_input_chars=source_size)
    assert model.calls==[]


def test_reference_cache_reconstructs_quotes_and_detects_tampered_pointer_or_quote(tmp_path):
    model=Model()
    @contextmanager
    def factory():yield model
    config={'quote_protocol':QUOTE_PROTOCOL}
    directory,results=run_reviews([item()],factory,tmp_path,config)
    assert results[0]['status']=='succeeded' and len(model.calls)==1
    record=results[0]
    assert record['span_policy']==SPAN_POLICY and record['reference_response']
    _,again=run_reviews([item()],factory,tmp_path,config)
    assert len(model.calls)==1 and again[0]['resumed']
    tampered=deepcopy(record);tampered['reference_response']['evidence_assessments'][0]['span_ids']=['E1:S999']
    assert not checked_cache(tampered,record['run_key'],item(),prepare_claim(item()),quote_protocol=QUOTE_PROTOCOL)
    tampered=deepcopy(record);tampered['review']['evidence_assessments'][0]['quoted_spans']=['invented quote']
    assert not checked_cache(tampered,record['run_key'],item(),prepare_claim(item()),quote_protocol=QUOTE_PROTOCOL)
    request=json.loads((directory/'requests/case-1.json').read_text())
    assert request['quote_protocol']==QUOTE_PROTOCOL and request['span_policy']==SPAN_POLICY
    assert review_run_key([item()],{})!=record['run_key']


def test_new_reference_protocol_never_reuses_unmarked_legacy_results(tmp_path):
    from test.test_claim_verifier import FixtureModel
    legacy=FixtureModel()
    @contextmanager
    def old():yield legacy
    old_dir,_=run_reviews([item()],old,tmp_path,{})
    model=Model()
    @contextmanager
    def new():yield model
    new_dir,records=run_reviews([item()],new,tmp_path,{'quote_protocol':QUOTE_PROTOCOL})
    assert old_dir!=new_dir and len(model.calls)==1 and not records[0]['resumed']


def test_cli_explicit_reference_protocol_keeps_frozen_legacy_run_and_labels_separate(dataset,monkeypatch):
    import run_claim_evaluation as cli
    from test.test_claim_verifier import FixtureModel
    path,_,_,_=dataset
    legacy=FixtureModel()
    @contextmanager
    def old(**kwargs):yield legacy
    monkeypatch.setattr(cli,'analysis_llm',old)
    monkeypatch.setattr(cli,'validate_model_configuration',lambda:None)
    args=['--dataset',str(path),'--timeout','5']
    assert cli.main(args)==0 and len(legacy.calls)==2
    before={p:p.read_bytes() for p in (path/'review_runs').glob('*/comparison.json')}
    model=Model()
    @contextmanager
    def new(**kwargs):yield model
    monkeypatch.setattr(cli,'analysis_llm',new)
    assert cli.main(args+['--quote-protocol',QUOTE_PROTOCOL])==0
    assert len(model.calls)==2
    for p,content in before.items():assert p.read_bytes()==content
    assert len(list((path/'review_runs').glob('*/comparison.json')))==2
    assert 'HUMAN_ONLY' not in str(model.calls)
