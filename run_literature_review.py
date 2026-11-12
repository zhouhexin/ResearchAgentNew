
import argparse
import os
import json

from pathlib import Path
from dotenv import load_dotenv
from agent.llm_runtime import analysis_llm, validate_model_configuration
from agent.codex_model import CodexExecutionError

from review.loader import load_analyses
from review.db_guard import verify_sources
from review.matrix import write_matrix
from review.synthesis import (
    synthesize_review,
    render_review_md,
    ReviewValidationError,
    review_output_tokens,
)


def main():
    parser = argparse.ArgumentParser(
        description="ResearchPilot 第七课"
    )

    parser.add_argument(
        "files",
        nargs="+",
        help="2～5 个第六课分析 JSON",
    )

    parser.add_argument(
        "--out",
        default="outputs/review07",
    )

    parser.add_argument(
        "--topic",
        default="",
        help="综述研究主题",
    )

    args = parser.parse_args()

    load_dotenv()
    validate_model_configuration()

    # 1. 加载已有分析
    papers = load_analyses(
        args.files
    )
    print("输入论文：")
    for paper in papers:
        print(f"  P{paper.paper_id}: {paper.title}")
    print(f"综述主题：{args.topic or '所选论文的共同研究主题'}")

    database_url = os.environ[
        "DATABASE_URL"
    ]

    # 2. 与 PostgreSQL 核对证据
    verify_sources(
        database_url,
        papers,
    )

    # 3. 生成确定性的比较矩阵
    out = Path(args.out)

    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    write_matrix(
        papers,
        out / "comparison_matrix.csv",
    )

    print("比较矩阵已生成。")

    # 5. 生成结构化综述
    method = os.getenv(
        "STRUCTURED_OUTPUT_METHOD",
        "json_schema",
    )

    try:
        with analysis_llm(read_timeout=float(os.getenv('LLM_READ_TIMEOUT', '400')),
                          retries=int(os.getenv('LLM_MAX_RETRIES', '0')), trust_env=True,
                          max_output_tokens=review_output_tokens()) as llm:
            draft, _ = synthesize_review(llm, papers, method=method, topic=args.topic)
    except CodexExecutionError as exc:
        raise SystemExit(f'{exc.code} · {exc}') from None
    except ReviewValidationError as exc:
        path = out / "review_validation_error.json"
        path.write_text(json.dumps(exc.diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
        raise SystemExit(
            f"综述未通过验收：{exc}\n请检查主题与输入论文是否相关；"
            f"未导出本次综述。失败草稿与引用情况：{path}"
        ) from None

    # 6. 检查 LLM 执行期间数据是否变化
    verify_sources(
        database_url,
        papers,
    )

    # 7. 保存 JSON
    (out / "literature_review.json").write_text(
        draft.model_dump_json(indent=2),
        encoding="utf-8",
    )

    # 8. 保存 Markdown 综述
    (out / "literature_review.md").write_text(
        render_review_md(draft, papers),
        encoding="utf-8",
    )

    print(f"生成完成：{out}")


if __name__ == "__main__":
    main()
