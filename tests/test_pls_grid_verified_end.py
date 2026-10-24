"""End-of-grid must be established by current-page metadata, not snapshot size.

No licensed service is contacted. These checks prevent journal recovery from
being credited when a page total is absent and the cursor is merely cached.
"""
import pytest

from scraper.tasks.pakistanlawsite import (
    _citation_grid_total_verified,
    _citation_grid_verified_end,
)


@pytest.mark.parametrize(
    "page_total,cached_total,start,rows,processed,expected",
    [
        # PR #323's missed case: wrap blocked but compact fallback still credited end.
        (None, 500, 450, 50, 50, False),
        # A short first snapshot does not prove there are no further pages.
        (None, None, 0, 50, 50, False),
        (None, 500, 0, 50, 50, False),
        # An invalid total is not evidence of completion.
        ("unknown", 500, 450, 50, 50, False),
        ("0", 500, 450, 50, 50, False),
        # Current-page positive total establishes a real completed boundary.
        (500, None, 450, 50, 50, True),
        ("500", None, 450, 50, 50, True),
        (50, None, 0, 50, 50, True),
        # Incomplete or zero-progress windows never establish a lap.
        (500, None, 400, 50, 50, False),
        (500, None, 450, 50, 0, False),
    ],
)
def test_unverified_snapshot_cannot_certify_grid_end(
    page_total, cached_total, start, rows, processed, expected
):
    # Replicate the cursor's total fallback; it can guide traversal but
    # must not independently certify completeness.
    verified = _citation_grid_total_verified(page_total)
    total = int(page_total) if verified else (cached_total or rows)
    assert _citation_grid_verified_end(
        start, processed, total, total_verified=verified
    ) is expected
