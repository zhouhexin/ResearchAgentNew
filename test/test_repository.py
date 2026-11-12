
import pytest

from models import Paper
from storage.repository import (
    PaperRepository,
    IdentityConflictError,
    normalize_doi,
    normalize_openalex_id,
)


def test_normalize_identifiers():
    assert normalize_doi(
        "https://doi.org/10.1234/ABC"
    ) == "10.1234/abc"

    assert normalize_openalex_id(
        "W123"
    ) == "https://openalex.org/W123"


@pytest.fixture
def repo(isolated_database_url):
    """每个测试在独立 schema 内运行。"""
    with PaperRepository(isolated_database_url) as repository:
        yield repository


def test_insert_and_query(repo):
    repo.save(Paper(
        title="中文论文",
        doi="https://doi.org/10.1234/ABC",
        authors=["张三"],
    ))

    paper = repo.get_by_doi("doi:10.1234/abc")

    assert paper is not None
    assert paper.title == "中文论文"
    assert paper.authors == ["张三"]


def test_doi_dedup(repo):
    repo.save(Paper(
        title="Paper A",
        doi="10.1234/A",
    ))

    repo.save(Paper(
        title="Paper A",
        doi="https://doi.org/10.1234/a",
        citation_count=6,
    ))

    assert repo.count() == 1

    assert repo.get_by_doi(
        "10.1234/a"
    ).citation_count == 6


def test_openalex_dedup(repo):
    repo.save(Paper(
        title="Paper A",
        openalex_id="W123",
    ))

    repo.save(Paper(
        title="Paper A",
        openalex_id="https://openalex.org/W123",
        doi="10.2222/a",
    ))

    assert repo.count() == 1
    assert repo.get_by_doi("10.2222/a") is not None


def test_batch_rollback(repo):
    repo.save(Paper(
        title="A",
        doi="10.1234/a",
        openalex_id="W1",
    ))

    repo.save(Paper(
        title="B",
        doi="10.1234/b",
        openalex_id="W2",
    ))

    with pytest.raises(IdentityConflictError):
        repo.save_many([
            Paper(
                title="Temporary",
                doi="10.1234/temp",
            ),
            Paper(
                title="Ambiguous",
                doi="10.1234/a",
                openalex_id="W2",
            ),
        ])

    assert repo.count() == 2
    assert repo.get_by_doi("10.1234/temp") is None


def test_missing_identity(repo):
    with pytest.raises(ValueError):
        repo.save(Paper(title="No IDs"))
