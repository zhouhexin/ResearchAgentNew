"""模型只返回原文片段编号，引用文本由后端从固定输入中直接取出。"""
import json
import re
from typing import Literal, Annotated

from pydantic import Field, ValidationError, field_validator, create_model

from agent.claim_verifier import (CLAIM_REVIEW_PROMPT, ClaimReview, ReviewError,
                                  StrictModel, prepare_claim, validate_review_output)

QUOTE_PROTOCOL = 'references-v2'
SPAN_POLICY = 'complete-sentences-soft600-v1'
SCHEMA_POLICY = 'evidence-span-enums-v1'
REFERENCE_REVIEW_PROMPT = CLAIM_REVIEW_PROMPT.replace(
    '为每个输入 evidence_id 返回一条 evidence_assessments，逐字引用该证据上下文的关键连续原句。\n'
    'quoted_spans 优先使用短而有区分力的连续片段，保留真实换行和断词，不拼接或润色。',
    '为每个输入 evidence_id 返回一条 evidence_assessments，引用该证据下的 span_ids。\n'
    'spans 按原文顺序完整提供全部上下文，每个 span_id 对应后端固定的连续原文。联合阅读该证据的全部片段再作判断，不能只看原分析引句。\n'
    'span_ids 选出对判断有区分力的 1–3 个编号；需要前后文或冲突双方时一起选择。编号必须属于对应 evidence_id，不能重复或编造。\n'
    '不输出、改写或重抄引用文本；原文由后端按所选编号直接提取。')


class ReferenceReviewError(ReviewError):
    code = 'CLAIM_REFERENCE_INVALID'


class ReferenceAssessment(StrictModel):
    evidence_id: str = Field(min_length=1)
    relation: Literal['supports', 'contradicts', 'insufficient', 'ambiguous']
    reason: str = Field(min_length=1)
    span_ids: list[str] = Field(min_length=1, max_length=3)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('评审理由不能为空')
        return value


class ReferenceClaimReview(StrictModel):
    item_id: str = Field(min_length=1)
    verdict: Literal['supported', 'contradicted', 'insufficient', 'needs_review']
    reason: str = Field(min_length=1)
    evidence_assessments: list[ReferenceAssessment] = Field(min_length=1, max_length=12)

    @field_validator('reason')
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError('评审理由不能为空')
        return value


def source_spans(evidence):
    """按整句/段落组合，不截断长句；切片连续覆盖全部输入字符。"""
    text = evidence.context
    boundaries = [match.end() for match in re.finditer(r'\n[ \t]*\n+|(?<=[.!?。！？])\s+(?=\S)', text)]
    boundaries.append(len(text))
    ranges, start, previous = [], 0, 0
    for end in boundaries:
        if end - start > 600 and previous > start and text[start:previous].strip():
            ranges.append((start, previous))
            start = previous
        previous = end
    if start < len(text):
        if text[start:].strip():
            ranges.append((start, len(text)))
        elif ranges:
            ranges[-1] = (ranges[-1][0], len(text))
    spans = [{'span_id': f'{evidence.evidence_id}:S{i+1}', 'start_char': begin, 'end_char': end,
              'text': text[begin:end]} for i, (begin, end) in enumerate(ranges)]
    if not spans or ''.join(span['text'] for span in spans) != text:
        raise ReferenceReviewError('无法生成完整的原文片段目录')
    return spans


def reference_payload(claim, *, max_input_chars=24000):
    payload = {'sample_id': claim.sample_id, 'paper_title': claim.paper_title, 'statement': claim.statement,
               'evidence': [{'evidence_id': e.evidence_id, 'page_number': e.page_number,
                             'quote': e.quote, 'spans': source_spans(e)} for e in claim.evidence]}
    if len(json.dumps(payload, ensure_ascii=False)) > max_input_chars:
        from agent.claim_verifier import ReviewPrecheckError
        raise ReviewPrecheckError('含原文编号的评审材料超过输入预算，未截断或请求模型')
    return payload


def response_schema(payload):
    """将本次允许的编号写入枚举，限制严格结构化服务的输出范围。"""
    evidence_ids = tuple(e['evidence_id'] for e in payload['evidence'])
    span_ids = tuple(span['span_id'] for e in payload['evidence'] for span in e['spans'])
    def choices(values):
        def enum_schema(schema):
            schema.pop('const', None)
            schema['enum'] = list(values)
        return Annotated[Literal[values], Field(json_schema_extra=enum_schema)]
    assessment = create_model('ReferenceAssessment', __base__=ReferenceAssessment,
        evidence_id=(choices(evidence_ids), Field(description='只使用本次提供的证据编号')),
        span_ids=(list[choices(span_ids)], Field(min_length=1, max_length=3, description='仅选择对应证据下的原文编号')))
    return create_model('ReferenceClaimReview', __base__=ReferenceClaimReview,
        evidence_assessments=(list[assessment], Field(min_length=len(evidence_ids), max_length=len(evidence_ids))))


def parse_reference_review(response):
    if isinstance(response, dict) and {'raw', 'parsed'} <= response.keys():
        raw, result = response['raw'], response['parsed']
        metadata = getattr(raw, 'response_metadata', {}) or {}
        if metadata.get('finish_reason') in {'length', 'content_filter'} or (getattr(raw, 'additional_kwargs', {}) or {}).get('refusal'):
            raise ReferenceReviewError('编号评审响应被拒绝、截断或过滤')
        if result is None:
            calls = getattr(raw, 'tool_calls', []) or []
            if len(calls) == 1 and calls[0].get('name') == ReferenceClaimReview.__name__:
                result = calls[0].get('args')
            elif not calls:
                content = getattr(raw, 'content', '')
                if isinstance(content, str):
                    content = content.strip()
                    if content.startswith('```json\n') and content.endswith('```'):
                        content = content[8:-3].strip()
                    try:
                        result = json.loads(content)
                    except ValueError:
                        pass
    else:
        result = response
    try:
        return ReferenceClaimReview.model_validate(result)
    except (ValidationError, TypeError):
        raise ReferenceReviewError('模型未返回有效的原文编号评审；不接受模型生成的引用文本') from None


def materialize_review(response, claim):
    result = ReferenceClaimReview.model_validate(response)
    if result.item_id != claim.sample_id:
        raise ReferenceReviewError('模型返回了其他样本编号')
    evidence = {e.evidence_id: e for e in claim.evidence}
    ids = [a.evidence_id for a in result.evidence_assessments]
    if len(ids) != len(evidence) or set(ids) != set(evidence):
        raise ReferenceReviewError('评审证据编号存在遗漏、重复或越界')
    assessments = []
    for assessment in result.evidence_assessments:
        source = evidence[assessment.evidence_id]
        allowed = {span['span_id']: span for span in source_spans(source)}
        if len(set(assessment.span_ids)) != len(assessment.span_ids) or not set(assessment.span_ids) <= allowed.keys():
            raise ReferenceReviewError('评审原文编号重复、未知或不属于对应证据')
        quotes = []
        for identifier in assessment.span_ids:
            span = allowed[identifier]
            quote = source.context[span['start_char']:span['end_char']]
            if quote != span['text'] or not quote.strip():
                raise ReferenceReviewError('原文编号与固定上下文不一致')
            quotes.append(quote)
        assessments.append({'evidence_id': assessment.evidence_id, 'relation': assessment.relation,
                            'reason': assessment.reason, 'quoted_spans': quotes})
    review = ClaimReview(item_id=result.item_id, verdict=result.verdict, reason=result.reason, evidence_assessments=assessments)
    validate_review_output(review, claim)
    review._reference_response = result.model_dump(mode='json')
    return review


def review_claim_references(llm, item, *, method='json_schema', max_input_chars=24000):
    claim = prepare_claim(item, max_input_chars=max_input_chars)
    payload = reference_payload(claim, max_input_chars=max_input_chars)
    response = llm.with_structured_output(response_schema(payload), method=method, strict=method == 'json_schema', include_raw=True).invoke([
        ('system', REFERENCE_REVIEW_PROMPT), ('human', json.dumps(payload, ensure_ascii=False))])
    return materialize_review(parse_reference_review(response), claim)
