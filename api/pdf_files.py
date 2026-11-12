"""只从已保存的全文记录定位本地 PDF，不接收客户端文件路径。"""
from pathlib import Path
from urllib.parse import unquote, urlsplit


def local_pdf_path(document) -> Path | None:
    if document.source_type != "pdf":
        return None
    try:
        source = urlsplit(document.source_url)
        if source.scheme.lower() != "file" or source.netloc not in ("", "localhost"):
            return None
        if source.query or source.fragment:
            return None
        path = Path(unquote(source.path))
        if not path.is_absolute():
            return None
        path = path.resolve(strict=True)
        if not path.is_file() or path.suffix.lower() != ".pdf":
            return None
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return None
        return path
    except (OSError, ValueError):
        return None
