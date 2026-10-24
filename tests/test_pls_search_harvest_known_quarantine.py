"""Search-harvest must not count retryable bad-page staging as a known judgment."""

import hashlib

import pytest

from scraper.config import settings
from scraper.models import ScraperStaging, SourceProvenance
from scraper.tasks.pls_search_harvest import load_known_citation_keys


@pytest.mark.asyncio
async def test_retryable_quarantine_not_known_by_case_name_or_citation(db, monkeypatch):
    monkeypatch.setattr(settings, "PLS_CITATION_GRID_SKIP_STAGED", True)
    cases = (
        ("2025L901", "PLD 2025 Lahore 901", "quarantined", "login_stub: body missing", "Access denied", None, False),
        ("2025L902", "PLD 2025 Lahore 902", "quarantined", None, "Error Code:700", None, False),
        ("2025L903", "PLD 2025 Lahore 903", "quarantined", "citation conflict: human review", "A real judgment", None, True),
        ("2025L904", "PLD 2025 Lahore 904", "pending", None, "", None, False),
        ("2025L905", "PLD 2025 Lahore 905", "promoted", None, "A real judgment", None, True),
        ("2025L906", "PLD 2025 Lahore 906", "quarantined", "login_stub: body missing", "Access denied", {"refetch_count": 3}, True),
    )
    for case, citation, status, reason, raw, route, _ in cases:
        url = f"https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName={case}&Row=0"
        prov = SourceProvenance(
            source_name="PakistanLawSite", access_method="login_session", source_url=url,
            content_hash=hashlib.sha256(url.encode()).hexdigest(), content_kind="html",
        )
        db.add(prov)
        await db.flush()
        db.add(ScraperStaging(
            source_name="PakistanLawSite", access_method="login_session", source_url=url,
            provenance_id=prov.id, content_hash=hashlib.sha256((url + "-stage").encode()).hexdigest(),
            status=status, quarantine_reason=reason, raw_text=raw,
            route_json=route, extracted_citation=citation,
        ))
    await db.flush()
    keys = {f"case:{case}" for case, *_ in cases} | {item[1] for item in cases}
    known = await load_known_citation_keys(db, keys)
    for case, citation, _status, _reason, _raw, _route, expected in cases:
        assert (f"case:{case}" in known) is expected
        assert (citation in known) is expected
    await db.rollback()
