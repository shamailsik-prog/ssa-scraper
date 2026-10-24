"""Case-ID walk must not hide legitimate judgments behind failed/stub staging rows."""

import hashlib

import pytest

from scraper.models import ScraperStaging, SourceProvenance
from scraper.tasks.pls_caseid_walk import load_known


@pytest.mark.asyncio
async def test_caseid_walk_keeps_retryable_stubs_discoverable(db):
    cases = [
        # (CaseName, status, quarantine reason, raw text, route, held)
        ("2023HCAJK1", "pending", None, "", None, False),
        ("2023SCAJK2", "quarantined", "login_stub: body missing", "Access denied", None, False),
        ("2024LAT-SINDH3", "failed", None, "Service unavailable", None, False),
        ("2024HC-A4", "quarantined", None, "Error Code:700", None, False),
        ("2023HCAJK5", "quarantined", "citation conflict: human review", "Real judgment", None, True),
        ("2023SCAJK6", "quarantined", "login_stub: body missing", "Access denied", {"refetch_count": 3}, True),
        ("2023HCAJK7", "extracted", None, "Judgment body", None, True),
        ("2023HCAJK8", "promoted", None, "Judgment body", None, True),
        ("2023HCAJK9", "duplicate", None, "Judgment body", None, True),
    ]
    for code, status, reason, raw, route, _held in cases:
        url = f"https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName={code}&Row=0"
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
            content_hash=hashlib.sha256((url + status).encode()).hexdigest(),
            status=status,
            quarantine_reason=reason,
            raw_text=raw,
            route_json=route,
        ))
    await db.flush()
    known = await load_known(db)
    for code, _status, _reason, _raw, _route, held in cases:
        # CaseName is YYYY + court token + serial; use the original code's
        # numeric suffix rather than guessing where hyphenated tokens end.
        year = code[:4]
        serial = int(code[-1])
        group = code[:-1]
        assert (serial in known.get(group, set())) is held, (code, held, known)
    await db.rollback()
