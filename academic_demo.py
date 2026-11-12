
import json
import os
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from tools.arxiv_search import search_arxiv

from tools.academic_search import (
    search_google_scholar,
)


def main():
    load_dotenv()

    # 1. 用户输入
    query = input("研究主题（回车使用示例）：").strip()

    query = query or (
        "medical image segmentation transformer"
    )

    # 2. arXiv 检索
    papers = search_arxiv(
        query,
        limit=20,
        start_year=2024,
        end_year=2026,
    )

    # 3. 创建目录并保存结果
    output = Path("outputs")
    output.mkdir(exist_ok=True)

    (output / "papers.json").write_text(
        json.dumps(
            [asdict(p) for p in papers],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # 4. 输出到终端
    print(f"\narXiv 检索到 {len(papers)} 篇论文：")

    for i, paper in enumerate(papers, 1):
        print(f"{i}. [{paper.year}] {paper.title}")
        print(
            f"   DOI: {paper.doi or '暂无'}"
            f" | 引用量: {paper.citation_count}"
        )

    # 5. 可选：Google Scholar 检索
    if os.getenv("SERPAPI_KEY"):
        results = search_google_scholar(
            query,
            limit=5,
        )

        (output / "scholar_results.json").write_text(
            json.dumps(
                [asdict(r) for r in results],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            f"\nGoogle Scholar 找到 "
            f"{len(results)} 条搜索结果"
        )
    else:
        print("\n未配置 SERPAPI_KEY，跳过 Google Scholar。")

    print("\n数据已保存到 outputs/ 目录")


if __name__ == "__main__":
    main()
