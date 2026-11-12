
import pytest

from pydantic import ValidationError

from agent.analysis_schema import PaperAnalysis

from agent.analysis_validator import (
    EvidenceSource,
    PaperContext,
    validate_analysis,
    export_analysis,
    restore_original_quotes,
    EvidenceValidationError,
)


def make_context():
    return PaperContext(
        paper_id=7,
        paper_title="Example",
        doi="10.1234/demo",
        document_id=3,
        document_sha256="hash",
        source_url="https://example.org/p.pdf",
        total_chunks=5,
        sources={
            "E11": EvidenceSource(
                evidence_id="E11",
                text="We evaluate on Dataset A.",
                source_url="https://example.org/p.pdf",
                page_number=2,
                section_hint="Results",
            )
        },
    )


def make_analysis(
    evidence_id="E11",
    quote="evaluate on Dataset A",
):
    return PaperAnalysis.model_validate({
        "research_questions": [],
        "methodologies": [],
        "contributions": [],
        "experiments": [
            {
                "statement": "在数据集 A 上进行评估",
                "evidence": [
                    {
                        "evidence_id": evidence_id,
                        "quote": quote,
                    }
                ],
            }
        ],
        "limitations": [],
    })


def test_valid_quote():
    """合法的编号和引用应该通过。"""

    context = make_context()
    analysis = make_analysis()

    validate_analysis(analysis, context)

    output = export_analysis(
        analysis, context
    )

    assert output["evidence_index"][
        "E11"
    ]["page_number"] == 2


def test_nonexistent_evidence():
    """拒绝不存在的证据编号。"""

    with pytest.raises(
        ValueError,
        match="非法或越界",
    ):
        validate_analysis(
            make_analysis(evidence_id="E999"),
            make_context(),
        )


def test_fabricated_quote():
    """拒绝模型编造的原文。"""

    with pytest.raises(
        ValueError,
        match="原文不匹配",
    ):
        validate_analysis(
            make_analysis(
                quote="99% accuracy"
            ),
            make_context(),
        )


def test_no_evidence_for_claim():
    """有研究结论但没有证据时拒绝输出。"""

    analysis = make_analysis()

    analysis.experiments[0].evidence = []

    with pytest.raises(
        ValueError,
        match="缺少证据",
    ):
        validate_analysis(
            analysis,
            make_context(),
        )


def test_schema_rejects_extra_field():
    """Pydantic 应拒绝多余字段。"""

    with pytest.raises(ValidationError):
        PaperAnalysis.model_validate({
            "research_questions": [],
            "methodologies": [],
            "contributions": [],
            "experiments": [],
            "limitations": [],
            "fictional_field": "no",
        })


@pytest.mark.parametrize("original,quote", [
    ("We evaluate\non Dataset A.", "evaluate on Dataset A"),
    ("本文提出深\n度估计方法。", "深度估计方法"),
])
def test_restore_layout_whitespace_to_exact_source(original, quote):
    from dataclasses import replace
    context = make_context()
    context = replace(context, sources={"E11": replace(context.sources["E11"], text=original)})
    candidate = make_analysis(quote=quote)
    restored = restore_original_quotes(candidate, context)
    validate_analysis(restored, context)
    assert restored.experiments[0].evidence[0].quote in original
    assert candidate.experiments[0].evidence[0].quote == quote


@pytest.mark.parametrize("original,quote", [
    ("The value is 1 0.", "The value is 10."),
    ("准确率为 90%。", "准确率为 99%。"),
    ("First sentence. Second sentence.", "First...Second"),
    ("We evaluate on Dataset A.", "在数据集 A 上评估"),
])
def test_restore_does_not_accept_changed_content(original, quote):
    from dataclasses import replace
    context = make_context()
    context = replace(context, sources={"E11": replace(context.sources["E11"], text=original)})
    restored = restore_original_quotes(make_analysis(quote=quote), context)
    with pytest.raises(EvidenceValidationError):
        validate_analysis(restored, context)


def test_analyzer_repairs_once_and_revalidates():
    from agent.paper_analyzer import analyze_paper

    class Model:
        def __init__(self):
            self.calls = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            return make_analysis(quote="fabricated" if self.calls == 1 else "evaluate on Dataset A")

    model = Model()
    result = analyze_paper(model, make_context())
    assert model.calls == 2
    validate_analysis(result, make_context())


def test_analyzer_rejects_after_bounded_repair():
    from agent.paper_analyzer import analyze_paper

    class Model:
        calls = 0

        def with_structured_output(self, *args, **kwargs):
            return self

        def invoke(self, messages):
            self.calls += 1
            return make_analysis(quote="fabricated")

    model = Model()
    with pytest.raises(EvidenceValidationError) as error:
        analyze_paper(model, make_context())
    assert model.calls == 2
    assert error.value.diagnostics["issues"][0]["candidate_quote"] == "fabricated"
    assert error.value.diagnostics["issues"][0]["source_text"] == make_context().sources["E11"].text


def test_structured_missing_result_has_clear_error():
    from agent.paper_analyzer import parse_analysis_response, StructuredAnalysisError
    from langchain_core.messages import AIMessage
    response = {"raw": AIMessage(content="我会分析这篇论文"), "parsed": None, "parsing_error": None}
    with pytest.raises(StructuredAnalysisError, match="未返回可解析") as error:
        parse_analysis_response(response, "function_calling")
    assert error.value.diagnostics["raw_response"]["content"] == "我会分析这篇论文"


def test_structured_text_json_is_validated_and_evidence_checked():
    from agent.paper_analyzer import parse_analysis_response
    from langchain_core.messages import AIMessage
    raw = AIMessage(content="```json\n" + make_analysis().model_dump_json() + "\n```")
    analysis = parse_analysis_response({"raw": raw, "parsed": None}, "function_calling")
    validate_analysis(analysis, make_context())


@pytest.mark.parametrize("response", [None, {}, {"experiments": []}])
def test_invalid_response_is_never_empty_success(response):
    from agent.paper_analyzer import parse_analysis_response, StructuredAnalysisError
    with pytest.raises(StructuredAnalysisError):
        parse_analysis_response(response, "function_calling")


def test_truncated_response_rejected_even_if_parsed():
    from agent.paper_analyzer import parse_analysis_response, StructuredAnalysisError
    from langchain_core.messages import AIMessage
    response = {"raw": AIMessage(content="", response_metadata={"finish_reason": "length"}),
                "parsed": make_analysis()}
    with pytest.raises(StructuredAnalysisError, match="截断"):
        parse_analysis_response(response, "json_schema")


def wrapped_tool_response(quote='evaluate on Dataset A'):
    from langchain_core.messages import AIMessage
    value = make_analysis(quote=quote).model_dump()
    evidence = value['experiments'][0]['evidence'][0]
    value['experiments'][0]['evidence'][0] = {'evidence': evidence}
    return {'raw': AIMessage(content='', response_metadata={'finish_reason': 'stop'},
                            tool_calls=[{'name': 'PaperAnalysis', 'args': value, 'id': 'call-test'}]),
            'parsed': None, 'parsing_error': ValueError('SDK parser rejected nested evidence')}


def test_raw_tool_arguments_with_single_evidence_wrapper_are_revalidated():
    from copy import deepcopy
    from agent.paper_analyzer import parse_analysis_response
    response = wrapped_tool_response()
    original = deepcopy(response['raw'].tool_calls)
    parsed = parse_analysis_response(response, 'function_calling')
    assert parsed == make_analysis()
    assert response['raw'].tool_calls == original
    validate_analysis(parsed, make_context())


@pytest.mark.parametrize('quote,valid', [('evaluate on Dataset A', True), ('fabricated results', False)])
def test_wrapper_repair_still_requires_exact_source_quotes(quote, valid):
    from agent.paper_analyzer import analyze_paper

    class Model:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages):
            return wrapped_tool_response(quote)

    if valid:
        validate_analysis(analyze_paper(Model(), make_context(), max_repair_attempts=0), make_context())
    else:
        with pytest.raises(EvidenceValidationError):
            analyze_paper(Model(), make_context(), max_repair_attempts=0)


@pytest.mark.parametrize('item', [
    {'evidence': {'evidence_id': 'E11'}},
    {'evidence': {'evidence_id': 'E11', 'quote': 'evaluate on Dataset A', 'extra': 'ambiguous'}},
    {'evidence': {'evidence_id': 'E11', 'quote': 'evaluate on Dataset A'}, 'quote': 'conflicting quote'},
])
def test_wrapper_repair_rejects_missing_or_conflicting_fields(item):
    import json
    from agent.paper_analyzer import parse_analysis_response, StructuredAnalysisError, analysis_retry_feedback
    response = wrapped_tool_response()
    response['raw'].tool_calls[0]['args']['experiments'][0]['evidence'][0] = item
    with pytest.raises(StructuredAnalysisError) as error:
        parse_analysis_response(response, 'function_calling')
    assert error.value.diagnostics['validation_errors'][0]['loc'][:4] == ('experiments', 0, 'evidence', 0)
    feedback = json.loads(analysis_retry_feedback(error.value))
    assert feedback['validation_errors']
    assert set(feedback['expected_evidence_item']) == {'evidence_id', 'quote'}


def test_raw_unexpected_function_is_not_treated_as_analysis():
    from agent.paper_analyzer import parse_analysis_response, StructuredAnalysisError
    response = wrapped_tool_response()
    response['raw'].tool_calls[0]['name'] = 'UnrelatedTool'
    with pytest.raises(StructuredAnalysisError):
        parse_analysis_response(response, 'function_calling')


def test_evidence_retry_feedback_contains_every_mismatched_quote():
    import json
    from agent.analysis_validator import evidence_diagnostics
    from agent.paper_analyzer import analysis_retry_feedback
    analysis = make_analysis(quote='fabricated result')
    exc = EvidenceValidationError('引用不匹配', evidence_diagnostics(analysis, make_context()))
    feedback = json.loads(analysis_retry_feedback(exc))
    assert feedback['issues'][0]['candidate_quote'] == 'fabricated result'
    assert feedback['issues'][0]['evidence_id'] == 'E11'
    assert feedback['issues'][0]['finding_index'] == 0
    assert 'source_text' not in feedback['issues'][0]  # 原始请求已含全文片段。
    assert '禁止拼接左右栏' in feedback['instruction']
