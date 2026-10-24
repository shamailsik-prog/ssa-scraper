"""A cached or malformed grid total must never certify a completed citation lap.

Pure cursor-state checks: no licensed source is accessed.
"""
import pytest

from scraper.tasks.pakistanlawsite import (
    _citation_grid_total_verified,
    _citation_grid_verified_wrap,
)


@pytest.mark.parametrize(
    "page_total,expected",
    [
        (None, False),
        ("", False),
        ("not available", False),
        ("0", False),
        ("-1", False),
        ("500", True),
        (500, True),
        (float("inf"), False),
    ],
)
def test_only_current_positive_parseable_page_total_is_verified(page_total, expected):
    assert _citation_grid_total_verified(page_total) is expected


@pytest.mark.parametrize(
    "page_total,previous_total,start_offset,next_offset,expected",
    [
        # A cached total of 500, but no total in the current page, is not proof
        # that rows 450-499 reached the end of the real journal.
        (None, 500, 450, 0, False),
        # Invalid current-page metadata must not launder the cached total.
        ("unknown", 500, 450, 0, False),
        ("0", 500, 450, 0, False),
        # A verified current-page total permits a real cursor wrap.
        (500, None, 450, 0, True),
        # No decreasing offset: no wrap, even with a verified total.
        (500, None, 250, 450, False),
        # A compact grid can still be recognized separately by reached_end;
        # it cannot assert a decreasing-cursor wrap without a trusted total.
        (None, None, 0, 0, False),
    ],
)
def test_unverified_grid_total_never_claims_wrap(
    page_total, previous_total, start_offset, next_offset, expected
):
    # previous_total is deliberately unused as a verification source:
    # it may have come from a prior session with a different grid size.
    assert _citation_grid_verified_wrap(
        start_offset,
        next_offset,
        total_verified=_citation_grid_total_verified(page_total),
    ) is expected


def test_false_wrap_would_have_cleared_multi_journal_recovery():
    # Reproduce the real state transition that used to credit one journal
    # despite absent current-page total_rows: 50 rows from cached 500.
    cached_total, start_offset, processed = 500, 450, 50
    next_offset = (start_offset + processed) % cached_total
    assert next_offset < start_offset  # old implementation: wrapped=True
    verified = _citation_grid_total_verified(None)
    wrapped = _citation_grid_verified_wrap(
        start_offset, next_offset, total_verified=verified
    )
    reached_end = bool(
        start_offset + processed >= cached_total and verified
    )
    assert not wrapped and not reached_end
