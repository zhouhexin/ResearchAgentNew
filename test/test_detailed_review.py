"""详细综述的材料覆盖、完整引文、分段引用和历史结构兼容。"""
from copy import deepcopy
import json

import pytest

from agent.analysis_schema import PaperAnalysis
from review.loader import ReviewPaper
from review.synthesis import (ReviewDraft, build_detailed_payload, render_review_md,
                              review_output_tokens, validate_review)


def paper(paper_id, count=12, quote_size=40):
    findings, index = [], {}
    for i in range(count):
        quote = f'Paper {paper_id} observation {i}: ' + 'original evidence ' * quote_size + f' result {i}. '
        key = f'E{i+1}'
        findings.append({'statement': f'论文 {paper_id} 的分析发现 {i}', 'evidence': [{'evidence_id': key, 'quote': quote}]})
        index[key] = {'text': quote, 'page_number': i+1, 'source_url': 'fixture'}
    return ReviewPaper(paper_id, f'Paper {paper_id}', None, paper_id, 'hash', 'fixture',
                       PaperAnalysis(research_questions=deepcopy(findings), methodologies=deepcopy(findings),
                                     contributions=[], experiments=deepcopy(findings), limitations=[]), index)


def test_includes_later_findings_and_full_quotes_with_truthful_coverage():
    papers = [paper(1), paper(2)]
    payload, allowed = build_detailed_payload(papers, '方法的适用条件')
    assert payload['review_focus'] == '方法的适用条件'
    for source, item in zip(papers, payload['papers']):
        findings = item['findings']['experiments']
        assert len(findings) > 3
        assert any(value['statement'] == source.analysis.experiments[-1].statement for value in findings)
        assert findings[0]['evidence'][0]['quote'] == source.analysis.experiments[0].evidence[0].quote
        assert len(findings[0]['evidence'][0]['quote']) > 350
        for category, values in item['findings'].items():
            assert item['material_coverage'][category]['provided'] == len(values)
            assert item['material_coverage'][category]['available'] == len(getattr(source.analysis, category))
    provided = {ref['ref'] for item in payload['papers'] for values in item['findings'].values() for finding in values for ref in finding['evidence']}
    assert allowed == provided


def test_budget_is_shared_without_truncating_or_mutating_evidence():
    papers = [paper(1, quote_size=4), paper(2, quote_size=4)]
    original = [p.analysis.model_dump() for p in papers]
    payload, allowed = build_detailed_payload(papers, max_chars=4200)
    assert len(json.dumps(payload, ensure_ascii=False)) <= 4200
    assert any(ref.startswith('P1:') for ref in allowed) and any(ref.startswith('P2:') for ref in allowed)
    for source, item in zip(papers, payload['papers']):
        for values in item['findings'].values():
            for finding in values:
                for ref in finding['evidence']:
                    key = ref['ref'].split(':')[1]
                    assert ref['quote'] == source.evidence_index[key]['text']
    assert [p.analysis.model_dump() for p in papers] == original


def test_too_large_finding_is_omitted_as_a_whole():
    source = paper(1, count=2, quote_size=1)
    source.analysis.experiments[0].evidence[0].quote = 'x' * 8000
    payload, allowed = build_detailed_payload([source, paper(2, count=2, quote_size=1)], max_chars=5000)
    values = payload['papers'][0]['findings']['experiments']
    assert len(values) == 1 and values[0]['evidence'][0]['ref'] == 'P1:E2'
    assert payload['papers'][0]['material_coverage']['experiments'] == {'available': 2, 'provided': 1}


def draft():
    return ReviewDraft(title='详细比较', background=[], common_methods=[], limitations=[],
                       comparisons=[{'heading': '结合实验条件理解差异', 'claim': '两篇论文具有可比较的方法发现。',
                                     'evidence_refs': ['P1:E1', 'P2:E1'],
                                     'paragraphs': [{'text': '第一段解释论文一的输入和处理步骤。', 'evidence_refs': ['P1:E1']},
                                                    {'text': '第二段解释另一个实验条件；它不能脱离测量口径进行比较。', 'evidence_refs': ['P2:E2']}]}],
                       synthesis=[{'claim': '回答本次问题时，需要同时考虑两篇论文给出的适用条件。', 'evidence_refs': ['P1:E1', 'P2:E1']}],
                       tentative_directions=[])


def test_paragraph_refs_are_validated_and_exported_in_evidence_index():
    result = draft()
    validate_review(result, {'P1:E1', 'P2:E1', 'P2:E2'})
    markdown = render_review_md(result, [paper(1, count=2), paper(2, count=2)])
    assert '### 结合实验条件理解差异' in markdown
    assert '第一段解释论文一的输入和处理步骤。 [1，第1页][^evidence-1]' in markdown
    assert '第二段解释另一个实验条件' in markdown
    assert '[^evidence-3]: 文献 [2]' in markdown
    assert '内部定位：`P2:E2`' in markdown  # 只在解释段落使用的引用也必须出现在脚注。
    assert '## 围绕研究问题的综合回答' in markdown


@pytest.mark.parametrize('text,refs', [('', ['P2:E2']), ('解释', []), ('解释', ['P2:E999']), ('解释', ['P2:E2', 'P2:E2'])])
def test_explanations_cannot_bypass_citation_checks(text, refs):
    result = draft()
    result.comparisons[0].paragraphs[0].text = text
    result.comparisons[0].paragraphs[0].evidence_refs = refs
    with pytest.raises(ValueError):
        validate_review(result, {'P1:E1', 'P2:E1', 'P2:E2'})


def test_legacy_review_structure_remains_readable():
    value = draft().model_dump()
    value.pop('synthesis')
    for item in value['comparisons']:
        item.pop('heading');item.pop('paragraphs')
    result = ReviewDraft.model_validate(value)
    validate_review(result, {'P1:E1', 'P2:E1'})
    assert result.synthesis == [] and result.comparisons[0].paragraphs == []


@pytest.mark.parametrize('value', ['0', '-1', 'invalid'])
def test_output_budget_requires_positive_integer(monkeypatch, value):
    monkeypatch.setenv('REVIEW_MAX_OUTPUT_TOKENS', value)
    with pytest.raises(ValueError, match='REVIEW_MAX_OUTPUT_TOKENS'):
        review_output_tokens()
