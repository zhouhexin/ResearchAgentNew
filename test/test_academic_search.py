
import pytest

from tools.academic_search import (
    restore_abstract,
    search_google_scholar,
    search_openalex,
)


class FakeResponse:
    """模拟 requests 返回的响应。"""

    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


def test_restore_abstract():
    index = {
        "Deep": [0],
        "learning": [1, 3],
        "for": [2],
    }

    assert restore_abstract(index) == (
        "Deep learning for learning"
    )
    assert restore_abstract(None) == ""


def test_openalex_parsing(monkeypatch):
    def fake_get(url, params, timeout):
        assert params["search"] == "segmentation"
        assert params["sort"] == "cited_by_count:desc"

        return FakeResponse({
            "results": [{
                "id": "https://openalex.org/W123",
                "title": "Sample Paper",
                "doi": "https://doi.org/10.1000/demo",
                "publication_year": 2024,
                "authorships": [
                    {"author": {"display_name": "Alice"}}
                ],
                "abstract_inverted_index": {
                    "Hello": [0],
                    "world": [1],
                },
                "cited_by_count": 5,
                "primary_location": {
                    "landing_page_url":
                        "https://example.org/paper"
                },
            }]
        })

    monkeypatch.delenv(
        "OPENALEX_API_KEY", raising=False
    )
    monkeypatch.setattr(
        "tools.academic_search.requests.get",
        fake_get,
    )

    papers = search_openalex(
        "segmentation", sort="cited"
    )

    assert len(papers) == 1
    assert papers[0].title == "Sample Paper"
    assert papers[0].authors == ["Alice"]
    assert papers[0].doi == "10.1000/demo"
    assert papers[0].abstract == "Hello world"


def test_scholar_parsing(monkeypatch):
    def fake_get(url, params, timeout):
        assert params["engine"] == "google_scholar"
        assert params["as_ylo"] == 2022

        return FakeResponse({
            "organic_results": [{
                "title": "Some study",
                "link": "https://example.org/study",
                "snippet": "A study",
            }]
        })

    monkeypatch.setenv("SERPAPI_KEY", "test-key")
    monkeypatch.setattr(
        "tools.academic_search.requests.get",
        fake_get,
    )

    results = search_google_scholar(
        "segmentation", start_year=2022
    )

    assert results[0].title == "Some study"
    assert results[0].url == "https://example.org/study"


def test_invalid_year_range():
    with pytest.raises(ValueError):
        search_openalex(
            "ai",
            start_year=2026,
            end_year=2022,
        )


