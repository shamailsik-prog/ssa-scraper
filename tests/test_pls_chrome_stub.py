"""PLS Case Description chrome-shell detection, modal wait flag, refetch marking."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from scraper.config import settings
from scraper.extractors.judgment_guards import (
    CHROME_STUB_REFETCH_ROUTE_KEY,
    detect_pls_case_description_chrome_stub,
    guard_reason,
    judgment_is_full_ready,
)
from scraper.models import Judgment, ScraperStaging
from scraper.tasks.pakistanlawsite import quarantined_capture_retryable
from scraper.tasks.pls_chrome_stub_refetch import find_chrome_stub_promoted, mark_chrome_stub_refetch_eligible

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "pls_chrome_stub"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _chrome_shell_11k() -> str:
    base = _read("scmr_chrome_shell_base.txt")
    pad = "Footer navigation padding line for SCMR chrome capture length.\n"
    text = base
    while len(text) < 11000:
        text += pad
    return text[:11000]


def test_chrome_shell_11k_detected_as_stub():
    text = _chrome_shell_11k()
    assert len(text) >= 11000
    signal = detect_pls_case_description_chrome_stub(text)
    assert signal is not None
    assert signal.signal == "case_description_chrome_shell"
    reason = guard_reason(signal)
    assert quarantined_capture_retryable(reason, len(text), {}, text[:400])
    assert not judgment_is_full_ready(text, ["Some Judge"])


def test_full_judgment_fixture_not_chrome_stub():
    text = _read("scmr_717_1999_full_judgment_shape.txt")
    if len(text) < 5000:
        text = text + "\n" + ("x" * (5200 - len(text)))
    assert detect_pls_case_description_chrome_stub(text) is None
    assert judgment_is_full_ready(text, ["Munir A. Sheikh"])


def test_modal_wait_gated_on_env_flag():
    import inspect

    from scraper.auth import session_manager

    source = inspect.getsource(session_manager.PlaywrightBrowser._capture_case_description_modal)
    assert "SSA_PLS_CASE_MODAL_WAIT_FOR_BODY" in source
    assert "waitForBody" in source
    assert "SSA_PLS_HEADNOTE_BODY_GUARD" not in source


@pytest.mark.asyncio
async def test_mark_chrome_stub_refetch_dry_run_idempotent(db, login_source, monkeypatch):
    from scraper.fetchers import record_provenance
    from scraper.tasks.promotion import canonical_text_hash

    monkeypatch.setattr(settings, "SSA_PLS_CHROME_STUB_REFETCH_MARK", True)
    text = _chrome_shell_11k()
    jid = uuid4()
    prov = await record_provenance(
        db,
        source=login_source,
        url="https://www.pakistanlawsite.com/test/chrome-mark",
        content=text.encode("utf-8"),
        content_kind="text",
    )
    st = ScraperStaging(
        id=uuid4(),
        source_name="PakistanLawSite",
        access_method="rendered_copy",
        provenance_id=prov.id,
        content_hash=canonical_text_hash(text),
        status="promoted",
        confidence_score=0.9,
        promoted_to_id=jid,
        raw_text=text,
    )
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="Supreme Court",
            canonical_citation="1991 SCMR 1162",
            case_title="Chrome shell test",
            full_text=text,
            full_text_hash=canonical_text_hash(text),
            judge_names=["Bench"],
            access_method="rendered_copy",
        )
    )
    db.add(st)
    await db.commit()

    report = await find_chrome_stub_promoted(db, limit=50)
    assert report["count"] >= 1
    dry = await mark_chrome_stub_refetch_eligible(db, report["fixes"], apply=False)
    assert dry["dry_run"] is True
    assert dry["would_mark"] >= 1

    mark = await mark_chrome_stub_refetch_eligible(db, report["fixes"], apply=True)
    await db.commit()
    assert mark["marked"] == 1

    route = dict((await db.get(ScraperStaging, st.id)).route_json or {})
    assert route.get(CHROME_STUB_REFETCH_ROUTE_KEY) is True

    second = await mark_chrome_stub_refetch_eligible(db, report["fixes"], apply=True)
    assert second["marked"] == 0
