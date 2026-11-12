import pytest

from review.synthesis import ReviewDraft, ReviewPoint, validate_review


def draft(refs):
    return ReviewDraft(
        title="引用核验", background=[ReviewPoint(claim="测试结论", evidence_refs=refs)],
        common_methods=[], comparisons=[], limitations=[], tentative_directions=[],
    )


@pytest.mark.parametrize("refs", [
    ["P2:E21", "P3:E46"],
    ["P12:E1", "P30:E68"],
])
def test_accept_single_and_multiple_digit_paper_ids(refs):
    validate_review(draft(refs), set(refs))


@pytest.mark.parametrize("bad", ["P0:E21", "P02:E21", "P2:E0", "P2:E021", "P2:E999"])
def test_reject_invalid_or_unprovided_refs(bad):
    with pytest.raises(ValueError, match="不存在或未提供"):
        validate_review(draft([bad, "P3:E46"]), {"P2:E21", "P3:E46"})


def test_still_requires_two_papers():
    with pytest.raises(ValueError, match="至少两篇"):
        validate_review(draft(["P2:E21"]), {"P2:E21"})
