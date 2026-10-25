"""Fail-closed reader grants on future schema additions (no database needed)."""

import pytest

from scraper.reader_grants import (
    READER_OPS_COLUMN_ALLOWLIST,
    reader_forbids_table_wide_select,
    reader_select_columns,
)


@pytest.mark.parametrize("table", ["judgment", "instrument", "source_provenance", "statute_section"])
def test_new_unreviewed_column_never_auto_granted(table):
    reviewed = READER_OPS_COLUMN_ALLOWLIST[table]
    live = (*reviewed, "future_private_body")
    assert reader_select_columns(table, live) == reviewed


def test_unknown_table_with_sensitive_column_fails_closed():
    assert reader_forbids_table_wide_select("future_table", ["id", "embedding"])
    with pytest.raises(ValueError, match="explicit reader column allow-list"):
        reader_select_columns("future_table", ["id", "embedding"])


def test_known_metadata_remains_readable_when_blocked_column_present():
    assert reader_select_columns("judgment", ["id", "canonical_citation", "full_text"]) == (
        "id",
        "canonical_citation",
    )


def test_no_reviewed_columns_fails_closed():
    with pytest.raises(ValueError, match="no reader-selectable"):
        reader_select_columns("judgment", ["future_private_body", "full_text"])
