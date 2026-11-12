"""引用展示不丢失实际引句，脚注去重，编号和来源快照保持一致。"""
from copy import deepcopy
from dataclasses import replace
import re

from review.citations import build_citation_index
from review.synthesis import render_review_md
from test.test_detailed_review import draft, paper


def test_full_quotes_after_chunk_prefix_and_multiple_quotes_are_preserved():
    papers = [paper(1, count=2), paper(2, count=2)]
    first = papers[0]
    original = first.analysis.research_questions[0].evidence[0].quote
    second = 'Another distinct original sentence after the main quotation.'
    first.evidence_index['E1']['text'] = 'unrelated prefix ' * 40 + original + second
    first.analysis.experiments[0].evidence[0].quote = second
    before = deepcopy(first.analysis.model_dump())
    citations = build_citation_index(draft(), papers)
    assert citations['P1:E1']['quotes'] == [original, second]
    markdown = render_review_md(draft(), papers)
    assert original in markdown and second in markdown
    assert 'unrelated prefix' not in markdown
    assert first.analysis.model_dump() == before
    assert '[^evidence-1]:' in markdown


def test_numbering_uses_paper_order_and_first_citation_order_with_shared_footnotes():
    result = draft()
    result.comparisons[0].evidence_refs.reverse()
    papers = [paper(2, count=2), paper(1, count=2)]
    citations = build_citation_index(result, papers)
    assert list(citations) == ['P2:E1', 'P1:E1', 'P2:E2']
    assert citations['P2:E1']['paper_number'] == 1
    assert citations['P1:E1']['paper_number'] == 2
    markdown = render_review_md(result, papers)
    uses = re.findall(r'\[\^(evidence-\d+)\](?!:)', markdown)
    definitions = re.findall(r'^\[\^(evidence-\d+)\]:', markdown, re.M)
    assert set(uses) == set(definitions)
    assert len(definitions) == len(set(definitions)) == 3
    assert uses.count('evidence-1') > 1
    assert '- [1] Paper 2' in markdown and '- [2] Paper 1' in markdown


def test_missing_page_and_markdown_in_source_cannot_create_fake_links():
    papers = [paper(1, count=2), paper(2, count=2)]
    quote = 'Literal [^fake] and <script>alert(1)</script>\nSecond line with * and _.'
    source = papers[0]
    source.evidence_index['E1'].update(text=quote, page_number=None)
    for findings in (source.analysis.research_questions, source.analysis.methodologies, source.analysis.experiments):
        findings[0].evidence[0].quote = quote
    citations = build_citation_index(draft(), papers)
    assert citations['P1:E1']['quotes'] == [quote]
    markdown = render_review_md(draft(), papers)
    assert '[1，无页码][^evidence-1]' in markdown
    assert '<script>' not in markdown
    assert r'Literal \[^fake\]' in markdown
    assert '    > Second line' in markdown


def test_legacy_missing_separate_quote_is_explicit_full_source_fallback():
    papers = [paper(1, count=2), paper(2, count=2)]
    first = papers[0]
    text = 'An entire legacy source chunk. ' * 30
    papers[0] = replace(first, evidence_index=dict(first.evidence_index, E1={
        'text': text, 'page_number': 7, 'source_url': 'fixture'}))
    citation = build_citation_index(draft(), papers)['P1:E1']
    assert citation['quote_kind'] == 'source_chunk' and citation['quotes'] == [text]
    markdown = render_review_md(draft(), papers)
    assert '未保存独立引句，以下为完整原文片段' in markdown and text in markdown
