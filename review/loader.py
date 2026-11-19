
import json
import re
from dataclasses import dataclass
from pathlib import Path

from agent.analysis_schema import PaperAnalysis


CATEGORIES = (
    "research_questions",
    "methodologies",
    "contributions",
    "experiments",
    "limitations",
)

# rozen=True → 冻结这个数据类，实例一旦创建就不能修改属性，变成只读对象。
@dataclass(frozen=True)
class ReviewPaper:
    paper_id: int
    title: str
    doi: str | None
    document_id: int
    document_hash: str
    source_url: str
    analysis: PaperAnalysis
    evidence_index: dict[str, dict]


def parse_analyses(records: list[dict]) -> list[ReviewPaper]:
    """核验并恢复分析快照，供命令行和 API 共用。"""

    if not 2 <= len(records) <= 5:
        raise ValueError("第一版需要 2～5 篇论文")
    return _parse_records(records)


def parse_analysis(record: dict) -> ReviewPaper:
    """单篇版本取回复用同一核验，不改变多篇综述的数量要求。"""
    return _parse_records([record])[0]


def _parse_records(records: list[dict]) -> list[ReviewPaper]:

    papers = []
    seen = set()

    for raw in records:
        path = raw.get("id", "分析快照")

        paper = raw["paper"]
        doc = raw["document"]

        paper_id = int(paper["id"])

        if paper_id in seen:
            raise ValueError(
                f"重复的论文 ID：{paper_id}"
            )

        seen.add(paper_id)

        # 恢复第六课的 Pydantic 分析模型
        analysis = PaperAnalysis.model_validate(
            raw["analysis"]
        )

        index = raw["evidence_index"]

        # 再次检查证据编号和原文
        for category in CATEGORIES:
            findings = getattr(
                analysis, category
            )

            for finding in findings:
                if (
                    not finding.statement.strip()
                    or not finding.evidence
                ):
                    raise ValueError(
                        f"{path}: 存在无证据结论"
                    )

                for evidence in finding.evidence:
                    if not re.fullmatch(
                        r"E[1-9]\d*",
                        evidence.evidence_id,
                    ):
                        raise ValueError(
                            "无效的 E 编号"
                        )

                    source = index.get(
                        evidence.evidence_id
                    )

                    if (
                        not isinstance(source, dict)
                        or not evidence.quote.strip()
                        or evidence.quote not in
                            source.get("text", "")
                        or source.get("source_url")
                            != doc["source_url"]
                    ):
                        raise ValueError(
                            f"{path}: 证据不匹配 "
                            f"{evidence.evidence_id}"
                        )

        papers.append(
            ReviewPaper(
                paper_id=paper_id,
                title=paper["title"],
                doi=paper.get("doi"),
                document_id=int(doc["id"]),
                document_hash=doc[
                    "content_sha256"
                ],
                source_url=doc["source_url"],
                analysis=analysis,
                evidence_index=index,
            )
        )

    return papers


def load_analyses(paths: list[str | Path]) -> list[ReviewPaper]:
    """加载命令行提供的分析 JSON，继续使用同一套快照核验。"""
    return parse_analyses([json.loads(Path(path).read_text(encoding="utf-8")) for path in paths])
