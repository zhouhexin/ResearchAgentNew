
from pathlib import Path
from urllib.parse import urlsplit

import requests


ALLOWED_HOSTS = {"arxiv.org", "export.arxiv.org"}
MAX_BYTES = 20 * 1024 * 1024


def download_arxiv_pdf(
    url: str,
    destination: str | Path,
) -> Path:
    """下载公开 arXiv PDF，限制大小和来源。"""

    parsed = urlsplit(url)

    if (
        parsed.scheme != "https"
        or parsed.hostname not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("仅允许指定的 arXiv HTTPS 地址")

    destination = Path(destination)

    with requests.get(
        url,
        stream=True,
        timeout=(10, 45),
        allow_redirects=False,
    ) as response:
        if response.is_redirect:
            raise RuntimeError(
                "遇到重定向，请核验目标 URL 后再下载"
            )

        response.raise_for_status()

        content = bytearray()

        for block in response.iter_content(
            chunk_size=65536
        ):
            content.extend(block)

            if len(content) > MAX_BYTES:
                raise ValueError("PDF 超过 20MB 限制")

    if not content.startswith(b"%PDF-"):
        raise ValueError("响应内容不是有效的 PDF 文件头")

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    destination.write_bytes(content)
    return destination
