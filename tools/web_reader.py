
import os
import requests


def read_webpage(url: str) -> str:
    if not url.startswith(("https://", "http://")):
        raise ValueError("只能读取 http/https URL")

    headers = {
        "Accept": "text/plain",
        "X-Respond-With": "markdown",
    }

    # api_key = os.getenv("JINA_API_KEY")
    #
    # if api_key:
    #     headers["Authorization"] = f"Bearer {api_key}"

    response = requests.get(
        f"https://r.jina.ai/{url}",
        headers=headers,
        timeout=60,
    )

    response.raise_for_status()

    if not response.ok:
        print("Jina 状态码：", response.status_code)
        print("Jina 错误详情：", response.text)

    if not response.text.strip():
        raise RuntimeError("Jina Reader 返回了空内容")

    return response.text
