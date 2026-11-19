from copy import deepcopy
import pytest

from agent.question_coverage import CoverageError, build_input, validate_output


@pytest.fixture
def data():
    snapshots = [{'id': 'analysis_1', 'paper': {'id': 1, 'title': '论文'},
        'document': {'selected_chunks': 1, 'total_chunks': 8}, 'batch_summary': {'scope': 'fast'},
        'reading_selection': {'note': '附录未读'}, 'analysis': {'methodologies': [{
            'statement': '方法研究适用性。', 'evidence': [{'evidence_id': 'E1', 'quote': 'We investigate applicability.'}]}]},
        'evidence_index': {'E1': {'text': 'We investigate applicability.\nIt remains uncertain.', 'page_number': 2}}}]
    payload = build_input(['方法是否有效？'], snapshots, [{'analysis_id':'analysis_1','status':'not_reviewed'}], 60000)
    ref = payload['evidence'][0]
    output = {'questions': [{'question_id':'Q1','status':'partial','answer':'研究了适用性，尚不能证明有效。',
        'reason':'缺少实验结论。','gaps':['需要核对有效性结果。'],'finding_ids':['P1:methodologies:0'],
        'citations':[{'ref_id':'P1:E1','span_ids':[ref['spans'][0]['span_id']]}],
        'suggestions':[{'action':'read_existing','paper_ids':[1],'target':'实验部分（待定位）','rationale':'补充证据。'}]}],
        'limitations':['快读，未提供全文。']}
    return payload, output


def test_extracts_exact_original_and_keeps_unknown_review_and_reading_scope(data):
    payload, output = data
    result = validate_output(output, payload)
    assert result['suggestions_executed'] is False and result['human_review_status']=='not_collected'
    assert result['questions'][0]['citations'][0]['source_spans'][0]['text'] in payload['evidence'][0]['spans'][0]['text']
    assert payload['auxiliary_reviews'][0]['status']=='not_reviewed'
    assert payload['papers'][0]['reading_scope']=='fast' and '附录未读' in payload['papers'][0]['reading_selection']['note']


@pytest.mark.parametrize('change', ['missing_question','duplicate_question','foreign_question','foreign_finding',
    'foreign_evidence','foreign_span','duplicate_span','no_citation','blank_reason','no_gap','foreign_paper','no_paper','fake_quote','conflict_one_side'])
def test_rejects_invalid_model_references_without_relaxing_source_validation(data, change):
    payload, output = deepcopy(data)
    item = output['questions'][0]
    if change=='missing_question': output['questions']=[]
    elif change=='duplicate_question': output['questions'].append(deepcopy(item))
    elif change=='foreign_question': item['question_id']='Q9'
    elif change=='foreign_finding': item['finding_ids']=['P99:methodologies:0']
    elif change=='foreign_evidence': item['citations'][0]['ref_id']='P2:E1'
    elif change=='foreign_span': item['citations'][0]['span_ids']=['P2:E1:S1']
    elif change=='duplicate_span': item['citations'][0]['span_ids']*=2
    elif change=='no_citation': item['citations']=[]
    elif change=='blank_reason': item['reason']=' '
    elif change=='no_gap': item['gaps']=[]
    elif change=='foreign_paper': item['suggestions'][0]['paper_ids']=[99]
    elif change=='no_paper': item['suggestions'][0]['paper_ids']=[]
    elif change=='fake_quote': item['citations'][0]['quote']='fabricated'
    else: item['status']='conflicting'
    with pytest.raises(CoverageError): validate_output(output, payload)


def test_unanswered_allows_no_evidence_but_requires_explanation(data):
    payload, output = data
    output['questions'][0].update(status='unanswered', answer='', finding_ids=[], citations=[])
    assert validate_output(output, payload)['questions'][0]['status']=='unanswered'


def test_rejects_over_budget_without_truncating_or_calling_model(data):
    payload, _ = data
    with pytest.raises(CoverageError, match='预算'):
        build_input(['问题'], [{'id':'a','paper':{'id':1,'title':'p'},'document':{'selected_chunks':1,'total_chunks':1},
            'analysis':{'methodologies':[{'statement':'x','evidence':[{'evidence_id':'E1'}]}]},
            'evidence_index':{'E1':{'text':'Long sentence. '*100,'page_number':1}}}], [], 20)
