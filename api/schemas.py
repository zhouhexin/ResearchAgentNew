from typing import Literal

from pydantic import BaseModel, Field, model_validator
from tools.venue_search.catalog import VenueSlug


class PaperToSave(BaseModel):
    """网页提交的一篇待收藏论文。"""

    source: Literal["arxiv", "openalex"]
    venue_slug: VenueSlug | None = None

    title: str = Field(min_length=1, max_length=2000)
    authors: list[str] = Field(default_factory=list)
    abstract: str = ""

    year: int | None = Field(
        default=None,
        ge=1000,
        le=2100,
    )

    doi: str | None = None
    openalex_id: str | None = None
    url: str | None = None

    citation_count: int | None = Field(default=None, ge=0)

    # 在整个模型实例构造完成、所有字段基础校验全部通过之后才执行的校验器。
    @model_validator(mode="after")
    def check_identity(self):
        self.title = self.title.strip()
        self.doi = (self.doi or "").strip() or None
        self.openalex_id = (
                (self.openalex_id or "").strip() or None
        )

        if not self.title:
            raise ValueError("论文标题不能为空")

        if not self.doi and not self.openalex_id:
            raise ValueError(
                "论文至少需要 DOI 或 OpenAlex ID"
            )

        return self


class PaperBatchRequest(BaseModel):
    """一次收藏的论文列表。"""

    papers: list[PaperToSave] = Field(
        min_length=1,
        max_length=100,
    )
