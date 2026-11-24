"""离线输入契约拒绝畸形结构、越界归属及额外答案字段。"""
from copy import deepcopy
import pytest

from agent.runtime_snapshot import RuntimeUnavailable, fingerprint
from review.synthesis import SYSTEM, WRITING_PROFILE, ReviewDraft, check_review_request
from review.loader import CATEGORIES


def request():
    papers=[]
    for pid in (1,2):
        values={key:[] for key in CATEGORIES}
        values['research_questions']=[{'statement':'给定材料考察研究问题。','evidence':[
            {'ref':f'P{pid}:E{pid}','quote':'Original text.','page_number':None}]}]
        papers.append({'paper_id':pid,'title':f'Paper {pid}','doi':None,'findings':values,
            'material_coverage':{key:{'available':len(v),'provided':len(v)} for key,v in values.items()}})
    content={'protocol':'review-synthesis-v1','prompt':SYSTEM,'schema':ReviewDraft.model_json_schema(),
        'method':'json_schema','writing_profile':WRITING_PROFILE,'payload':{
            'review_focus':'固定问题','coverage_note':'仅所给材料。','papers':papers},
        'allowed_refs':['P1:E1','P2:E2']}
    return {**content,'sha256':fingerprint(content)}


def test_valid_original_review_input_keeps_its_content():
    value=request();original=deepcopy(value)
    check_review_request(value)
    assert value==original


@pytest.mark.parametrize('mutation',[
    lambda v:v.update(payload={}),
    lambda v:v.update(method=[]),
    lambda v:v['payload'].update(human_gold='must not be forwarded'),
    lambda v:v['payload']['papers'][0].update(gold_label='supported'),
    lambda v:v['payload']['papers'][0]['findings']['research_questions'][0]['evidence'][0].update(ref='P2:E1'),
    lambda v:v['payload']['papers'][0]['material_coverage']['research_questions'].update(provided=9),
    lambda v:v['payload']['papers'].append(deepcopy(v['payload']['papers'][0])),
    lambda v:v.update(allowed_refs=['P1:E1']),
])
def test_rehashed_malformed_input_is_rejected_as_a_contract_error(mutation):
    value=request();mutation(value)
    value['sha256']=fingerprint({k:v for k,v in value.items() if k!='sha256'})
    with pytest.raises(RuntimeUnavailable):check_review_request(value)


@pytest.mark.parametrize('extra', [
    {'human_gold':'independent answer'},
    {'issues':[{'assessment':{'human_judgments':[{'verdict':'met'}]}}]},
    {'issues':[{'assessment':{'is_human_gold':True}}]},
    {'issues':[{'error':{'diagnostic':{'requirements':['hidden requirement']}}}]},
])
def test_source_feedback_cannot_smuggle_human_answers(extra):
    from evaluation.report_protocol import check_feedback
    value={'report_id':'report','source_results':[], 'issues':[],
           'semantic_human_gold':'not_collected', 'original_protocol':'review-synthesis-v1'}
    with pytest.raises(ValueError):check_feedback({**value,**extra})


@pytest.mark.parametrize('native,has_request,same_source',[
    (True,False,True),(False,True,True),(True,True,False),
])
def test_report_bindings_reject_qa_fallback_and_foreign_requests(native,has_request,same_source):
    import json
    from types import SimpleNamespace
    from evaluation.report_protocol import check_binding
    value=request()
    version=SimpleNamespace(quote_protocol='review-synthesis-v1' if native else 'task-replay-pointers-v1',
        tools_json=json.dumps({'source_request_sha256':value['sha256'] if same_source else 'foreign'}))
    case=SimpleNamespace(review_request=value if has_request else None)
    with pytest.raises(RuntimeUnavailable):check_binding(case,version)


def test_matching_report_binding_keeps_the_original_request():
    import json
    from types import SimpleNamespace
    from evaluation.report_protocol import check_binding
    value=request();original=deepcopy(value)
    version=SimpleNamespace(quote_protocol='review-synthesis-v1',tools_json=json.dumps({'source_request_sha256':value['sha256']}))
    check_binding(SimpleNamespace(review_request=value),version)
    assert value==original
