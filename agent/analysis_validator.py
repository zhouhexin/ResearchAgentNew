
from dataclasses import dataclass
import logging
import re

from agent.analysis_schema import PaperAnalysis

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceSource:
    evidence_id: str
    text: str
    source_url: str
    page_number: int | None
    section_hint: str | None


@dataclass(frozen=True)
class PaperContext:
    paper_id: int
    paper_title: str
    doi: str | None

    document_id: int
    document_sha256: str
    source_url: str

    total_chunks: int
    sources: dict[str, EvidenceSource]
    reading_selection: dict | None = None


CATEGORIES = (
    "research_questions",
    "methodologies",
    "contributions",
    "experiments",
    "limitations",
)


class EvidenceValidationError(ValueError):
    """模型返回的结论或引用未通过证据检查。"""

    def __init__(self, message, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def evidence_diagnostics(analysis: PaperAnalysis, ctx: PaperContext) -> dict:
    issues = []
    for category in CATEGORIES:
        for index, finding in enumerate(getattr(analysis, category)):
            for ref in finding.evidence:
                source = ctx.sources.get(ref.evidence_id)
                if source is None or not ref.quote.strip() or ref.quote not in source.text:
                    issues.append({
                        "category": category, "finding_index": index,
                        "statement": finding.statement,
                        "evidence_id": ref.evidence_id,
                        "candidate_quote": ref.quote,
                        "source_text": source.text if source else None,
                        "page_number": source.page_number if source else None,
                        "reason": "unknown_evidence" if source is None else "quote_mismatch",
                    })
    return {"status": "failed_evidence_validation", "paper_id": ctx.paper_id,
            "document_id": ctx.document_id, "content_sha256": ctx.document_sha256,
            "issues": issues, "candidate_analysis": analysis.model_dump(mode="json")}


def _canonical_text(text: str):
    """仅统一空白；保留原始字符位置，不改标点、数字或文字。"""
    def cjk(char):
        return "\u3400" <= char <= "\u9fff"

    chars, positions = [], []
    i = 0
    while i < len(text):
        if not text[i].isspace():
            chars.append(text[i])
            positions.append(i)
            i += 1
            continue
        end = i + 1
        while end < len(text) and text[end].isspace():
            end += 1
        # 中文 PDF 常在连续汉字间插入换行；英文单词、数字间保留分隔。
        if i > 0 and end < len(text) and not (cjk(text[i - 1]) and cjk(text[end])):
            chars.append(" ")
            positions.append(i)
        i = end
    return "".join(chars), positions


def _quote_variants(quote: str):
    """最多撤销一层常见 JSON 转义；不解释 Unicode、十六进制或任意转义。"""
    replacements = {'n': '\n', 'r': '\r', 't': '\t', '"': '"', '\\': '\\'}
    decoded = re.sub(r'\\([nrt"\\])', lambda match: replacements[match[1]], quote)
    return (quote,) if decoded == quote else (quote, decoded)


def suggest_original_quote(quote: str, text: str) -> str | None:
    """只给重试模型提供原文候选，不用于自动通过引用核验。

    对照 PDF 行尾断词和最多两层转义定位，建议值必须是唯一连续原文。
    数字、普通连字符、大小写、文字变化仍不匹配；不跨证据片段搜索。
    """
    if not quote.strip() or not text:return None
    canonical, offsets = _canonical_text(text)
    # 仅忽略英文字母间的行尾断词；保留到源文本的逐字符映射。
    omitted=set()
    line_spaces=set()
    for match in re.finditer(r'(?<=[A-Za-z])-\r?\n[ \t]*(?=[a-z])', text):
        omitted.update(range(match.start(),match.end()))
        line_spaces.update(range(match.start()+1,match.end()))
    sources=[]
    for removed in (omitted,line_spaces):
        sources.append((''.join(char for char,pos in zip(canonical,offsets) if pos not in removed),
                        [pos for pos in offsets if pos not in removed]))
    variants={quote}
    for _ in range(2):variants.update(v for q in tuple(variants) for v in _quote_variants(q))
    matches=set()
    for variant in variants:
        needle,_=_canonical_text(variant)
        if len(needle)<24:continue
        for compact,positions in sources:
            start=compact.find(needle)
            while start>=0:
                matches.add(text[positions[start]:positions[start+len(needle)-1]+1])
                if len(matches)>1:return None
                start=compact.find(needle,start+1)
    return next(iter(matches)) if matches else None


def _matching_original_quotes(variants, text, canonical_source):
    """只返回源片段的连续子串；发现两个不同结果即可判定有歧义。"""
    canonical_text, positions = canonical_source
    matches = set()
    for quote in variants:
        canonical_quote, _ = _canonical_text(quote)
        if not canonical_quote:
            continue
        start = canonical_text.find(canonical_quote)
        while start >= 0:
            end = start + len(canonical_quote) - 1
            matches.add(text[positions[start]:positions[end] + 1])
            if len(matches) > 1:
                return matches
            start = canonical_text.find(canonical_quote, start + 1)
    return matches


def restore_original_quotes(analysis: PaperAnalysis, ctx: PaperContext) -> PaperAnalysis:
    """恢复原文空白/一层转义；合法编号指错片段时，只允许本批唯一匹配纠错。

    指定片段可匹配时优先保留原编号。未知编号、歧义、改词、改数字及跨片段
    拼接都不自动修复，仍交由 validate_analysis 拒绝。不修改输入或原文。
    """
    restored = analysis.model_copy(deep=True)
    canonical_sources = {key: _canonical_text(source.text) for key, source in ctx.sources.items()}
    for category in CATEGORIES:
        for finding in getattr(restored, category):
            for ref in finding.evidence:
                source = ctx.sources.get(ref.evidence_id)
                if source is None or not ref.quote.strip() or ref.quote in source.text:
                    continue
                variants = _quote_variants(ref.quote)
                matches = _matching_original_quotes(variants, source.text, canonical_sources[ref.evidence_id])
                if matches:
                    if len(matches) == 1:
                        ref.quote = next(iter(matches))
                    continue
                candidates = []
                for key, other in ctx.sources.items():
                    if key == ref.evidence_id:
                        continue
                    candidates.extend((key, quote) for quote in
                                      _matching_original_quotes(variants, other.text, canonical_sources[key]))
                    if len(candidates) > 1:
                        break
                if len(candidates) == 1:
                    key, quote = candidates[0]
                    logger.info('引用编号按本批唯一原文匹配修正：paper=%s document=%s from=%s to=%s',
                                ctx.paper_id, ctx.document_id, ref.evidence_id, key)
                    ref.evidence_id, ref.quote = key, quote
    return restored


def validate_analysis(
    analysis: PaperAnalysis,
    ctx: PaperContext,
) -> None:
    """验证每条结论的证据编号和原文。"""

    for category in CATEGORIES:
        findings = getattr(analysis, category)

        for finding in findings:
            if not finding.statement.strip():
                raise EvidenceValidationError(
                    f"{category}: 结论为空"
                )

            if not finding.evidence:
                raise EvidenceValidationError(
                    f"{category}: 结论缺少证据"
                )

            for cited in finding.evidence:
                source = ctx.sources.get(
                    cited.evidence_id
                )

                if source is None:
                    raise EvidenceValidationError(
                        "非法或越界证据编号："
                        f"{cited.evidence_id}"
                    )

                if (
                    not cited.quote.strip()
                    or cited.quote not in source.text
                ):
                    raise EvidenceValidationError(
                        f"{category}: 证据原文不匹配：{cited.evidence_id}。"
                        "quote 必须逐字复制该编号片段中的连续原文，不能改写、翻译或拼接。"
                    )


def export_analysis(
    analysis: PaperAnalysis,
    ctx: PaperContext,
) -> dict:
    """生成包含溯源信息的分析结果。"""

    validate_analysis(analysis, ctx)

    cited_ids = {
        ref.evidence_id
        for category in CATEGORIES
        for finding in getattr(analysis, category)
        for ref in finding.evidence
    }

    return {
        **({'reading_selection':ctx.reading_selection} if ctx.reading_selection else {}),
        "paper": {
            "id": ctx.paper_id,
            "title": ctx.paper_title,
            "doi": ctx.doi,
        },
        "document": {
            "id": ctx.document_id,
            "source_url": ctx.source_url,
            "content_sha256": ctx.document_sha256,
            "selected_chunks": len(ctx.sources),
            "total_chunks": ctx.total_chunks,
        },
        "analysis": analysis.model_dump(
            mode="json"
        ),
        "evidence_index": {
            evidence_id: {
                "source_url": ctx.sources[
                    evidence_id
                ].source_url,
                "page_number": ctx.sources[
                    evidence_id
                ].page_number,
                "section_hint": ctx.sources[
                    evidence_id
                ].section_hint,
                "text": ctx.sources[
                    evidence_id
                ].text,
            }
            for evidence_id in sorted(cited_ids) if evidence_id in ctx.sources
        },
    }
