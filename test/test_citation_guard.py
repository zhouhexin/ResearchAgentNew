
import json
from types import SimpleNamespace

from agent.citation_guard import citation_report


def fake_tool(results):
    """模拟 LangGraph 的工具响应。"""

    return SimpleNamespace(
        name="search_local_evidence",
        content=json.dumps({
            "results": results
        }),
    )


def test_valid_citation():
    messages = [
        fake_tool([
            {"evidence_id": "E21"}
        ])
    ]

    result = citation_report(
        "实验见 [E21]",
        messages,
    )

    assert result["invalid"] == []


def test_fake_citation_is_blocked():
    messages = [
        fake_tool([
            {"evidence_id": "E21"}
        ])
    ]

    result = citation_report(
        "结论见 [E999]",
        messages,
    )

    assert result["invalid"] == ["E999"]


def test_missing_citation_warning():
    messages = [
        fake_tool([
            {"evidence_id": "E21"}
        ])
    ]

    result = citation_report(
        "结论来自文献。",
        messages,
    )

    assert result[
        "has_evidence_without_citation"
    ] is True
