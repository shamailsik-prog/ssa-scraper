import pytest

from scraper.tasks.archive_audit import summarize_archive_health


@pytest.mark.asyncio
async def test_archive_audit_returns_shape(db):
    out = await summarize_archive_health()
    assert "targets" in out
    assert "pls_judgments_without_archive_object" in out
