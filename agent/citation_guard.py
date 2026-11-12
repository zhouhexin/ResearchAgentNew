
import json
import re


def citation_report(
    answer: str,
    messages: list,
) -> dict:
    """检查 [E编号] 是否真实出现在本轮工具结果中。"""

    allowed = set()

    for message in messages:
        # 尝试读取 message.name 如果 message 没有 name 属性，不会抛 AttributeError，直接返回 None
        if getattr(message, "name", None) not in {
            "search_local_evidence",
            "read_evidence",
        }:
            continue

        try:
            data = json.loads(message.content)
        except (ValueError, TypeError):
            continue

        if not isinstance(data, dict):
            continue

        # 收集搜索工具返回的证据
        for item in data.get("results", []):
            if not isinstance(item, dict):
                continue

            evidence_id = str(
                item.get("evidence_id", "")
            )

            if re.fullmatch(
                r"E[1-9]\d*", evidence_id
            ):
                allowed.add(evidence_id)

        # 收集单条证据查询结果
        evidence_id = str(
            data.get("evidence_id", "")
        )

        if re.fullmatch(
            r"E[1-9]\d*", evidence_id
        ):
            allowed.add(evidence_id)

    cited = set(
        re.findall(
            r"\[(E[1-9]\d*)\]",
            answer,
        )
    )

    return {
        "allowed": sorted(allowed),
        "cited": sorted(cited),
        "invalid": sorted(cited - allowed),
        "has_evidence_without_citation": (
            bool(allowed and not cited)
        ),
    }
