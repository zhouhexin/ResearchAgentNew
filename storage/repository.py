
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from models import Paper
from storage.database import Base, make_db
from storage.db_models import PaperRecord


class IdentityConflictError(ValueError):
    """DOI 与 OpenAlex ID 冲突，需要人工核验。"""


def normalize_doi(value: str | None) -> str | None:
    """将 DOI 转换为统一的小写标识符。"""

    if not value or not value.strip():
        return None

    val = value.strip()

    val = re.sub(
        r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)",
        "",
        value,
        flags=re.I,
    )

    return val.strip().lower() or None


def normalize_openalex_id(
    value: str | None
) -> str | None:
    """统一 OpenAlex ID 表示形式。"""

    if not value or not value.strip():
        return None

    val = value.strip().rstrip("/")

    match = re.fullmatch(
        r"(?:https?://openalex\.org/)?W(\d+)",
        val,
        re.I,
    )

    if not match:
        raise ValueError(f"无效的 OpenAlex ID：{val}")

    return f"https://openalex.org/W{match.group(1)}"



class PaperRepository:
    """论文数据库访问层。"""

    def __init__(self, database_url: str):
        # 创建数据库连接管理器
        self.engine, self.SessionLocal = make_db(
            database_url
        )

        # 初次运行时自动创建数据表
        Base.metadata.create_all(self.engine)

    @staticmethod
    def _to_paper(row: PaperRecord) -> Paper:
        """数据库模型转换为业务模型。"""

        return Paper(
            title=row.title,
            authors=list(row.authors),
            abstract=row.abstract,
            year=row.year,
            doi=row.doi,
            url=row.url,
            citation_count=row.citation_count,
            openalex_id=row.openalex_id or "",
        )

    def _save(
        self,
        session: Session,
        paper: Paper,
        *,
        with_status: bool = False,
    ) -> int | tuple[int, bool]:
        """插入新论文或更新已有论文。"""

        if not paper.title.strip():
            raise ValueError("论文标题不能为空")

        # 1. 标准化论文标识符
        doi = normalize_doi(paper.doi)

        openalex_id = normalize_openalex_id(
            paper.openalex_id
        )

        if not doi and not openalex_id:
            raise ValueError(
                "论文至少需要 DOI 或 OpenAlex ID"
            )

        # 2. 分别通过 DOI 和 OpenAlex ID 查询
        by_doi = (
            session.scalar(
                select(PaperRecord).where(
                    PaperRecord.doi == doi
                )
            )
            if doi else None
        )

        by_openalex = (
            session.scalar(
                select(PaperRecord).where(
                    PaperRecord.openalex_id == openalex_id
                )
            )
            if openalex_id else None
        )

        # 3. 两个标识符不能命中不同论文
        if (
            by_doi
            and by_openalex
            and by_doi.id != by_openalex.id
        ):
            raise IdentityConflictError(
                "DOI 和 OpenAlex ID 命中不同论文"
            )

        row = by_doi or by_openalex

        created = row is None

        # 4. 不存在：创建新论文
        if row is None:
            row = PaperRecord(
                title=paper.title.strip(),
                authors=list(paper.authors),
                abstract=paper.abstract,
                year=paper.year,
                doi=doi,
                url=paper.url,
                citation_count=paper.citation_count,
                openalex_id=openalex_id,
            )

            session.add(row)

        # 5. 已存在：检查并合并信息
        else:
            if row.doi and doi and row.doi != doi:
                raise IdentityConflictError(
                    "同一 OpenAlex ID 对应不同 DOI"
                )

            if (
                row.openalex_id
                and openalex_id
                and row.openalex_id != openalex_id
            ):
                raise IdentityConflictError(
                    "同一 DOI 对应不同 OpenAlex ID"
                )

            # 补充缺失的标识符
            row.doi = row.doi or doi
            row.openalex_id = (
                row.openalex_id or openalex_id
            )

            # 简单的 MVP 元数据合并策略
            if len(paper.authors) > len(row.authors):
                row.authors = list(paper.authors)

            if len(paper.abstract) > len(row.abstract):
                row.abstract = paper.abstract

            if row.year is None:
                row.year = paper.year

            row.url = row.url or paper.url

            row.citation_count = max(
                row.citation_count,
                paper.citation_count,
            )

        # 将变更发送给数据库，获取主键
        session.flush()

        if with_status:
            return row.id, created

        return row.id


    def save(self, paper: Paper) -> int:
        """保存一篇论文。"""

        with self.SessionLocal.begin() as session:
            return self._save(session, paper)

    def save_many(
        self,
        papers: list[Paper],
    ) -> list[int]:
        """批量保存；失败时回滚整个批次。"""

        with self.SessionLocal.begin() as session:
            return [
                self._save(session, paper)
                for paper in papers
            ]

    def get_by_doi(self, doi: str) -> Paper | None:
        """通过 DOI 获取论文。"""

        key = normalize_doi(doi)

        if not key:
            return None

        with self.SessionLocal() as session:
            row = session.scalar(
                select(PaperRecord).where(
                    PaperRecord.doi == key
                )
            )

            return self._to_paper(row) if row else None

    def list_all(self) -> list[Paper]:
        """获取数据库中的所有论文。"""

        with self.SessionLocal() as session:
            rows = session.scalars(
                select(PaperRecord).order_by(
                    PaperRecord.year.desc(),
                    PaperRecord.title,
                )
            ).all()

            return [
                self._to_paper(row)
                for row in rows
            ]

    def count(self) -> int:
        """统计论文总数。"""

        with self.SessionLocal() as session:
            return session.scalar(
                select(func.count()).select_from(
                    PaperRecord
                )
            ) or 0

    def close(self) -> None:
        """释放数据库引擎的连接资源。"""

        self.engine.dispose()

    def __enter__(self):
        return self

    def __exit__(
        self,
        exc_type,
        exc_value,
        traceback,
    ):
        self.close()
        return False

    def save_with_status(
            self,
            paper: Paper,
    ) -> int | tuple[int, bool]:
        """保存论文，返回：论文编号、是否新增。"""
        with self.SessionLocal.begin() as session:
            return self._save(
                session,
                paper,
                with_status=True,
            )


