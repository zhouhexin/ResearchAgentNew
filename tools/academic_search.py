
import os

import requests

from models import Paper, SearchResult


OPENALEX_URL = "https://api.openalex.org/works"
SERPAPI_URL = "https://serpapi.com/search.json"


def restore_abstract(index: dict | None) -> str:
    """把 OpenAlex 倒排索引恢复为正常摘要。"""

    if not index:
        return ""

    words = {}

    for word, positions in index.items():
        for position in positions:
            words[position] = word

    return " ".join(
        words[position]
        for position in sorted(words)
    )


def search_openalex(
    query: str,
    limit: int = 20,
    start_year: int | None = None,
    end_year: int | None = None,
    sort: str = "relevance",
) -> list[Paper]:
    """使用 OpenAlex 检索论文，并返回 Paper 对象。"""

    # 1. 参数检查
    if not query.strip():
        raise ValueError("检索关键词不能为空")

    if not 1 <= limit <= 100:
        raise ValueError("limit 必须在 1～100 之间")

    if (
        start_year is not None
        and end_year is not None
        and start_year > end_year
    ):
        raise ValueError("起始年份不能晚于结束年份")

    # 2. 排序配置
    sort_fields = {
        "relevance": None,
        "newest": "publication_date:desc",
        "cited": "cited_by_count:desc",
    }

    if sort not in sort_fields:
        raise ValueError("不支持的排序方式")

    # 3. 请求参数
    params = {
        "search": query,
        "per_page": limit,
        "select": (
            "id,title,doi,publication_year,authorships,"
            "abstract_inverted_index,cited_by_count,"
            "primary_location"
        ),
    }

    # 4. 年份筛选
    filters = []

    if start_year is not None:
        filters.append(
            f"from_publication_date:{start_year}-01-01"
        )

    if end_year is not None:
        filters.append(
            f"to_publication_date:{end_year}-12-31"
        )

    if filters:
        params["filter"] = ",".join(filters)

    if sort_fields[sort]:
        params["sort"] = sort_fields[sort]

    # 5. API Key 可选
    api_key = os.getenv("OPENALEX_API_KEY")

    if api_key:
        params["api_key"] = api_key

    # 6. 请求 OpenAlex
    response = requests.get(
        OPENALEX_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    # 7. 将 API 数据转换为 Paper
    papers = []

    for item in data.get("results", []):

        authors = [
            entry["author"]["display_name"]
            for entry in item.get("authorships", [])
            if entry.get("author", {}).get("display_name")
        ]

        raw_doi = item.get("doi")

        doi = (
            raw_doi.removeprefix("https://doi.org/")
            if raw_doi else None
        )

        landing_url = (
            (item.get("primary_location") or {})
            .get("landing_page_url")
        )

        paper = Paper(
            title=item.get("title") or "",
            authors=authors,
            abstract=restore_abstract(
                item.get("abstract_inverted_index")
            ),
            year=item.get("publication_year"),
            doi=doi,
            url=(
                landing_url
                or raw_doi
                or item.get("id")
                or ""
            ),
            citation_count=item.get("cited_by_count") or 0,
            openalex_id=item.get("id") or "",
        )

        papers.append(paper)

    return papers


def search_google_scholar(
    query: str,
    limit: int = 10,
    start_year: int | None = None,
    end_year: int | None = None,
) -> list[SearchResult]:
    """通过 SerpApi 检索 Google Scholar。"""

    api_key = os.getenv("SERPAPI_KEY")

    if not api_key:
        raise RuntimeError("缺少 SERPAPI_KEY")

    if not query.strip():
        raise ValueError("检索关键词不能为空")

    if not 1 <= limit <= 20:
        raise ValueError("limit 必须在 1～20 之间")

    if (
        start_year is not None
        and end_year is not None
        and start_year > end_year
    ):
        raise ValueError("起始年份不能晚于结束年份")

    params = {
        "engine": "google_scholar",
        "q": query,
        "num": limit,
        "api_key": api_key,
    }

    if start_year is not None:
        params["as_ylo"] = start_year

    if end_year is not None:
        params["as_yhi"] = end_year

    response = requests.get(
        SERPAPI_URL,
        params=params,
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(
            f"SerpApi 返回错误：{data['error']}"
        )

    return [
        SearchResult(
            title=item.get("title", ""),
            url=item["link"],
            snippet=item.get("snippet", ""),
        )
        for item in data.get("organic_results", [])[:limit]
        if item.get("link")
    ]
