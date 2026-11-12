
import os

from dotenv import load_dotenv

from storage.repository import PaperRepository
from tools.arxiv_search import search_arxiv


def main():
    # 1. 读取 .env
    load_dotenv()

    database_url = os.environ["DATABASE_URL"]

    # 2. 输入检索主题
    query = input(
        "请输入研究主题（回车使用默认主题）："
    ).strip()

    query = query or (
        "medical image segmentation transformer"
    )

    # 3. 搜索学术论文
    papers = search_arxiv(
        query,
        limit=20,
        start_year=2022,
        end_year=2026,
    )

    # 4. 保存到 PostgreSQL
    with PaperRepository(database_url) as repo:
        before = repo.count()

        repo.save_many(papers)

        after = repo.count()

        print(f"本次检索：{len(papers)} 篇")
        print(f"新增论文：{after - before} 篇")
        print(f"数据库总数：{after} 篇")

        # 5. 显示部分论文
        for paper in repo.list_all()[:5]:
            print(f"[{paper.year}] {paper.title}")


if __name__ == "__main__":
    main()
