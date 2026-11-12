
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

from models import SearchResult

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def google_search(query: str, limit: int = 5) -> list[dict]:
    api_key = os.getenv("SERPAPI_KEY")

    if not api_key:
        raise RuntimeError("缺少 SERPAPI_KEY，请检查 .env")

    if not query.strip():
        raise ValueError("检索词不能为空")

    if not 1 <= limit <= 10:
        raise ValueError("limit 必须在 1～10 之间")

    response = requests.get(
        "https://serpapi.com/search.json",
        params={
            "engine": "google",
            "q": query,
            "num": limit,
            "api_key": api_key,
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    if data.get("error"):
        raise RuntimeError(
            f"SerpApi 返回错误：{data['error']}"
        )

    results = []
    for item in data.get("organic_results", []):
        if not item.get("link"):
            continue

        result = SearchResult(
            title=item.get("title", ""),
            url=item["link"],
            snippet=item.get("snippet", "")
        )

        results.append(result)

    return results[:limit]

if __name__ == "__main__":
    google_search("弱光求深度")
