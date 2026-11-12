# 文档解析


import re
from pathlib import Path

import pymupdf

from models import PaperDocument, DocumentChunk
from tools.web_reader import read_webpage
from tools.text_chunking import text_windows, resolve_chunk_overlap

PDF_PARSER = 'pymupdf-native-sentence-v2'
NATIVE_PDF_PARSERS = {'pymupdf-native-v1', PDF_PARSER}


# 简单的章节标题识别规则
HEADING = re.compile(
    r"(?im)^(?:#{1,4}\s+(.{3,90})|"
    r"(\d+(?:\.\d+)*\s+[A-Za-z][^\n]{3,80})|"
    r"(Abstract|Introduction|Related Work|"
    r"Methods?|Results?|Discussion|"
    r"Conclusions?|References))\s*$"
)


def normalize_extracted_text(text: str) -> str:
    """用可见的缺字标记替换 NUL；PostgreSQL text 不接受 U+0000。

    必须在计算正文哈希、窗口和字符偏移之前统一处理，不能只修改入库参数。
    逐字符替换保留长度，不猜测 PDF 丢失的字符，也不拼接两侧词语。
    """
    return text.replace('\x00', '\ufffd')


def build_document(
    pages: list[tuple[int | None, str]],
    *,
    source_url: str,
    source_type: str,
    parser: str,
    page_count: int | None = None,
    chunk_size: int = 1200,
    chunk_overlap: int | None = None,
) -> PaperDocument:
    """将逐页文本构建为可追溯文档。"""

    chunk_overlap = resolve_chunk_overlap(chunk_size, chunk_overlap)

    parts = []
    chunks = []

    cursor = 0 # 记录当前字符在【完整全文】中的位置
    last_heading = None # 保存最近识别到的章节标题，作为section_hint

    for page_number, raw in pages: # PDF 单页原始文本
        page_text = normalize_extracted_text(raw).strip()

        if not page_text:
            continue

        # 不同页面用两个换行符分隔
        if parts:
            parts.append("\n\n")
            cursor += 2

        page_start = cursor

        parts.append(page_text)
        cursor += len(page_text)

        # 识别这一页可能存在的章节标题
        headings = list(
            HEADING.finditer(page_text)
        )

        heading_idx = 0

        # 窗口在每个页面内独立滑动，精确保留原文切片及其字符偏移。
        for start, end in text_windows(page_text, chunk_size=chunk_size, chunk_overlap=chunk_overlap):

            while (
                heading_idx < len(headings)
                and headings[heading_idx].start()
                    <= start
            ):
                last_heading = next(
                    (
                        g for g in
                        headings[heading_idx].groups()
                        if g
                    ),
                    None,
                )
                heading_idx += 1

            segment = page_text[start:end]

            chunks.append(
                DocumentChunk(
                    index=len(chunks),
                    text=segment,
                    start_char=page_start + start,
                    end_char=(
                        page_start + start + len(segment)
                    ),
                    page_number=page_number,
                    section_hint=last_heading,
                )
            )

        # 将章节提示延续到下一页
        if headings:
            last_heading = next(
                (
                    g for g in
                    headings[-1].groups() if g
                ),
                last_heading,
            )

    content = "".join(parts)

    if not content:
        raise ValueError(
            "没有提取到正文；扫描版 PDF 可能需要 OCR"
        )

    # 校验每个片段都能在全文中精确定位
    for chunk in chunks:
        assert (
            content[
                chunk.start_char:chunk.end_char
            ] == chunk.text
        )

    document = PaperDocument(
        source_url=source_url,
        source_type=source_type,
        parser=parser,
        text=content,
        chunks=chunks,
        page_count=page_count,
        chunk_overlap=chunk_overlap,
    )
    document.validate()
    return document


def parse_pdf(
    path: str | Path,
    chunk_size: int = 1200,
    *, chunk_overlap: int | None = None,
) -> PaperDocument:
    """读取本地 PDF 并保留页码信息。"""

    pdf_path = Path(path).expanduser().resolve()

    with pymupdf.open(pdf_path) as pdf:
        if not pdf.is_pdf:
            raise ValueError("输入文件不是 PDF")

        pages = [
            (
                i + 1,
                # 按坐标逐行排序会交错双栏论文；保留 PDF 自身的文本阅读顺序。
                page.get_text("text", sort=False),
            )
            for i, page in enumerate(pdf) # 循环时**同时拿到下标和元素本身**，i 默认从0开始
        ]

        page_count = len(pdf)

    return build_document(
        pages,
        source_url=pdf_path.as_uri(),
        source_type="pdf",
        parser=PDF_PARSER,
        page_count=page_count,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )



def parse_webpage(
    url: str,
    chunk_size: int = 1200,
    *, chunk_overlap: int | None = None,
) -> PaperDocument:
    """通过 Jina Reader 解析公开网页。"""

    if not url.startswith(
        ("https://", "http://")
    ):
        raise ValueError("只允许 http/https 网页")

    content = read_webpage(url)

    return build_document(
        [(None, content)],
        source_url=url,
        source_type="web",
        parser="jina_reader",
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
