"""AJ&K CaseName widening must not conceal retryable, incomplete captures."""

import hashlib

import pytest

from scraper.config import settings
from scraper.models import ScraperStaging, SourceProvenance
from scraper.pls_search_harvest_core import CASE_KEY_PREFIX, citation_keys_for_row
from scraper.tasks.pls_search_harvest import load_known_citation_keys


@pytest.mark.asyncio
async def test_ajk_case_names_and_citations_do_not_hide_retryable_stubs(db, monkeypatch):
    monkeypatch.setattr(settings, "PLS_CITATION_GRID_SKIP_STAGED", True)
    cases = (
        # AJ&K cases formerly missed by the short CaseName regex.
        ("2023HCAJK1", "PLD 2023 Aj&K 1", "quarantined", "login_stub: body missing", "Access denied", None, False),
        ("2023SCAJK2", "PLD 2023 SC (AJ&K) 2", "quarantined", None, "Error Code:700", None, False),
        ("2023HCAJK3", "PLD 2023 Aj&K 3", "pending", None, "", None, False),
        ("2023SCAJK4", "PLD 2023 SC (AJ&K) 4", "quarantined", "citation conflict: human review", "Real judgment", None, True),
        ("2023HCAJK5", "PLD 2023 Aj&K 5", "promoted", None, "Real judgment", None, True),
        ("2023SCAJK6", "PLD 2023 SC (AJ&K) 6", "quarantined", "login_stub: body missing", "Access denied", {"refetch_count": 3}, True),
    )
    for case, citation, status, reason, raw, route, _expected in cases:
        url = f"https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName={case}&Row=0"
        prov = SourceProvenance(
            source_name="PakistanLawSite",
            access_method="login_session",
            source_url=url,
            content_hash=hashlib.sha256(url.encode()).hexdigest(),
            content_kind="html",
        )
        db.add(prov)
        await db.flush()
        db.add(ScraperStaging(
            source_name="PakistanLawSite",
            access_method="login_session",
            source_url=url,
            provenance_id=prov.id,
            content_hash=hashlib.sha256((url + "-stage").encode()).hexdigest(),
            status=status,
            quarantine_reason=reason,
            raw_text=raw,
            route_json=route,
            extracted_citation=citation,
        ))
    await db.flush()

    keys = set()
    for case, citation, *_ in cases:
        keys |= citation_keys_for_row({
            "citation": citation,
            "detail_url": f"https://www.pakistanlawsite.com/?CaseName={case}&Row=7",
        })
    known = await load_known_citation_keys(db, keys)
    for case, citation, _status, _reason, _raw, _route, expected in cases:
        assert (CASE_KEY_PREFIX + case in known) is expected
        assert (citation in known) is expected
    await db.rollback()
