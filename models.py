
from dataclasses import dataclass, field

from hashlib import sha256

@dataclass
class SearchResult:
    """表示一条搜索引擎返回的结果"""

    title: str
    url: str
    snippet: str = ""

@dataclass
class Paper:
    """经过学术数据源解析的论文记录"""

    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    year: int | None = None
    doi: str | None = None
    url: str = ""
    citation_count: int = 0
    openalex_id: str = ""






@dataclass
class DocumentChunk:
    """一段可追溯的原文内容。"""

    index: int
    text: str

    # 在完整文档文本中的字符位置
    start_char: int
    end_char: int

    # PDF 页码从 1 开始；网页没有页码
    page_number: int | None

    # 启发式识别出的章节标题
    section_hint: str | None = None


@dataclass
class PaperDocument:
    """从论文 PDF 或网页中解析出的文档。"""

    source_url: str
    source_type: str
    parser: str

    text: str

    chunks: list[DocumentChunk] = field(
        default_factory=list
    )

    page_count: int | None = None
    # 新切片窗口允许的最大重叠字符数；旧的手工文档默认仍禁止重叠。
    chunk_overlap: int = 0

    def validate(self) -> None:
        """检查全文、片段位置及页码，保存前拒绝无效证据。"""
        if not self.text.strip() or not self.chunks:
            raise ValueError("文档正文或片段为空")
        if self.source_type not in {"pdf", "web"}:
            raise ValueError("source_type 必须为 pdf 或 web")
        if self.source_type == "pdf" and (
            not isinstance(self.page_count, int) or self.page_count < 1
        ):
            raise ValueError("PDF 必须记录实际页数")
        if not isinstance(self.chunk_overlap, int) or self.chunk_overlap < 0:
            raise ValueError('片段重叠上限必须为非负整数')
        previous_start = -1
        previous_end = 0
        previous_page = 0
        for index, chunk in enumerate(self.chunks):
            if chunk.index != index:
                raise ValueError("片段编号必须从 0 开始连续递增")
            if not (
                0 <= chunk.start_char < chunk.end_char <= len(self.text)
                and chunk.start_char > previous_start and chunk.end_char > previous_end
            ):
                raise ValueError("片段字符范围越界或窗口没有向前推进")
            if chunk.start_char < previous_end:
                if previous_end - chunk.start_char > self.chunk_overlap:
                    raise ValueError('片段重叠超过声明的上限')
                if self.source_type == 'pdf' and chunk.page_number != previous_page:
                    raise ValueError('重叠窗口不能跨越 PDF 页码')
            if self.text[chunk.start_char:chunk.end_char] != chunk.text:
                raise ValueError("证据片段与正文字符位置不一致")
            if self.text[previous_end:chunk.start_char].strip():
                raise ValueError("片段之间遗漏正文")
            if self.source_type == "pdf":
                if not isinstance(chunk.page_number, int) or not (
                    max(1, previous_page) <= chunk.page_number <= self.page_count
                ):
                    raise ValueError("PDF 片段页码无效或顺序错误")
                previous_page = chunk.page_number
            elif chunk.page_number is not None:
                raise ValueError("网页片段不能设置 PDF 页码")
            previous_start, previous_end = chunk.start_char, chunk.end_char
        if self.text[previous_end:].strip():
            raise ValueError("最后一个片段遗漏正文")

    @property
    def content_sha256(self) -> str:
        """计算全文哈希，辅助去重和版本识别。"""

        return sha256(
            self.text.encode("utf-8")
        ).hexdigest()
