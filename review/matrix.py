
import csv
from io import StringIO
from pathlib import Path

from review.loader import ReviewPaper


COLUMNS = {
    "research_questions": "研究问题",
    "methodologies": "研究方法",
    "contributions": "主要贡献",
    "experiments": "实验与结果",
    "limitations": "研究局限",
}


def safe_csv_cell(value: object) -> str:
    """避免来源文本被 Excel 解释成公式。"""

    text = str(value)

    if text.lstrip().startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + text

    return text


def render_matrix_csv(papers: list[ReviewPaper]) -> str:
    """导出带原文证据编号的文献比较矩阵。"""

    rows = []

    for paper in papers:
        row = {
            "论文ID": paper.paper_id,
            "标题": paper.title,
            "DOI": paper.doi or "",
        }

        for key, label in COLUMNS.items():
            findings = getattr(
                paper.analysis, key
            )

            row[label] = "\n".join(
                f"{finding.statement} "
                + " ".join(
                    f"[P{paper.paper_id}:"
                    f"{evidence.evidence_id}]"
                    for evidence in finding.evidence
                )
                for finding in findings
            ) or "未获得有证据的结论"

        rows.append(row)

    fp = StringIO(newline="")
    writer = csv.DictWriter(fp, fieldnames=["论文ID", "标题", "DOI", *COLUMNS.values()])
    writer.writeheader()
    writer.writerows([{key: safe_csv_cell(value) for key, value in row.items()} for row in rows])
    return fp.getvalue()


def write_matrix(papers: list[ReviewPaper], output: str | Path) -> None:
    Path(output).write_text(render_matrix_csv(papers), encoding="utf-8-sig", newline="")
