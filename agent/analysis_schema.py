
from pydantic import BaseModel, ConfigDict, Field


class EvidenceQuote(BaseModel):
    """一条原文证据。"""

    # 1. forbid（最严格）：存在额外字段 → 报错 model_config = ConfigDict(extra="forbid")
    #
    # 2. ignore：直接丢掉多余字段，不报错 model_config = ConfigDict(extra="ignore")
    #
    # 3. allow：保留额外字段，存入模型实例 model_config = ConfigDict(extra="allow")
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(
        description="原文证据编号，例如 E135"
    )

    quote: str = Field(
        description="从原文片段逐字复制的短句"
    )


class SupportedFinding(BaseModel):
    """一条具有原文证据的研究结论。"""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(
        description="根据原文提出的中文研究结论"
    )

    evidence: list[EvidenceQuote] = Field(
        description="支持该结论的原文证据"
    )


class PaperAnalysis(BaseModel):
    """一篇论文的结构化分析结果。"""

    model_config = ConfigDict(extra="forbid")

    research_questions: list[SupportedFinding] = Field(
        description="论文解决的研究问题"
    )

    methodologies: list[SupportedFinding] = Field(
        description="论文使用的研究方法与技术路线"
    )

    contributions: list[SupportedFinding] = Field(
        description="论文的主要创新与贡献"
    )

    experiments: list[SupportedFinding] = Field(
        description="实验设计、数据集、指标与实验结果"
    )

    limitations: list[SupportedFinding] = Field(
        description="论文明确陈述的研究局限"
    )
