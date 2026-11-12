
import pytest

from review.synthesis import (
    ReviewDraft,
    validate_review,
)


def make_draft(refs):
    return ReviewDraft(
        title="测试综述",
        background=[],
        common_methods=[],
        comparisons=[
            {
                "claim": "两篇论文采用不同方法",
                "evidence_refs": refs,
            }
        ],
        limitations=[],
        tentative_directions=[],
    )


def test_valid_cross_paper_review():
    allowed = {
        "P12:E101",
        "P15:E205",
    }

    draft = make_draft([
        "P12:E101",
        "P15:E205",
    ])

    validate_review(draft, allowed)


def test_reject_fake_reference():
    allowed = {
        "P12:E101",
        "P15:E205",
    }

    draft = make_draft([
        "P12:E101",
        "P15:E999",
    ])

    with pytest.raises(
        ValueError,
        match="不存在或未提供",
    ):
        validate_review(
            draft,
            allowed,
        )


def test_comparison_requires_two_papers():
    allowed = {
        "P12:E101",
        "P15:E205",
    }

    draft = make_draft([
        "P12:E101",
    ])

    with pytest.raises(
        ValueError,
        match="至少引用两篇",
    ):
        validate_review(
            draft,
            allowed,
        )
