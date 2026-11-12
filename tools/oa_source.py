# 查找开放获取全文


import os

import requests

from storage.repository import normalize_openalex_id


def get_oa_location(
    openalex_id: str,
) -> dict | None:
    """查询论文的开放获取位置。"""

    normalized = normalize_openalex_id(
        openalex_id
    )

    if not normalized:
        raise ValueError("缺少 OpenAlex ID")

    work_id = normalized.rsplit("/", 1)[-1]

    params = {
        "select": "id,best_oa_location",
    }

    api_key = os.getenv("OPENALEX_API_KEY")

    if api_key:
        params["api_key"] = api_key

    response = requests.get(
        f"https://api.openalex.org/works/{work_id}",
        params=params,
        timeout=30,
    )

    response.raise_for_status()

    data = response.json()

    return data.get("best_oa_location")
