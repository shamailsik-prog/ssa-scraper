"""Court-website judgments without a law-report citation stage as extracted for the unreported promotion path."""

from __future__ import annotations

from sqlalchemy import select

from scraper.extractors import deterministic as det
from scraper.extractors.validation import reconcile_judgment
from scraper.fetchers import record_provenance, stage_judgment
from scraper.models import ScraperStaging
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks.promotion import NO_CITATION, promote_judgment_staging, release_unreported
from tests.test_validation_and_promotion import COURTS

SHC_BODY = (
    "IN THE HIGH COURT OF SINDH AT KARACHI\nCriminal Bail Application No. 674 of 2022\n"
    "Muhammad Ali versus The State\nBefore Mr. Justice Ahmed Khan\nDate of decision: 1st May, 2024\n"
    "ORDER\n"
    + (
        "The applicant seeks post-arrest bail. Learned counsel submits that the offence does not fall within "
        "the prohibitory clause of section 497 Cr.P.C. and that the applicant has been in custody for four months. "
        "The learned Deputy Prosecutor General opposes the application. Having heard the parties and perused the record, "
        "the case calls for further inquiry. The application is allowed subject to furnishing solvent surety."
    )
)


def test_shc_like_unreported_judgment_without_citation_is_not_quarantined_at_validation():
    dj = det.extract_judgment_deterministic(html=None, text=SHC_BODY, source_meta={})
    out = reconcile_judgment(
        deterministic=dj,
        ai=None,
        raw_text=SHC_BODY,
        court_directory=COURTS,
        min_confidence=0.85,
        source_name="SindhHighCourt",
    )
    assert out.quarantine is False
    assert out.data.get("citations") == []


def test_short_shc_capture_stays_quarantined_with_unreported_reason():
    short = "IN THE HIGH COURT OF SINDH\nCr. Bail 674/2022\nAdjourned."
    dj = det.extract_judgment_deterministic(html=None, text=short, source_meta={})
    out = reconcile_judgment(
        deterministic=dj,
        ai=None,
        raw_text=short,
        court_directory=COURTS,
        min_confidence=0.85,
        source_name="SindhHighCourt",
    )
    assert out.quarantine is True
    assert "not taken as unreported: text too short" in (out.quarantine_reason or "")


async def test_shc_staged_without_citation_promotes_as_unreported(db, source):
    html = f"<html><body><p>{SHC_BODY}</p></body></html>"
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/shc/unreported-gate",
        content=html.encode(),
        content_kind="html",
    )
    st = await stage_judgment(
        db,
        source=source,
        prov=prov,
        raw_html=html,
        raw_text=clean_html(html),
        url=prov.source_url,
    )
    st.source_name = "SindhHighCourt"
    dj = det.extract_judgment_deterministic(html=html, text=st.raw_text, source_meta={})
    from scraper.extractors.validation import reconcile_judgment as rj

    out = rj(
        deterministic=dj,
        ai=None,
        raw_text=st.raw_text,
        court_directory=COURTS,
        min_confidence=0.85,
        source_name="SindhHighCourt",
    )
    st.reconciled_json, st.confidence_score = out.data, out.confidence
    st.status = "quarantined" if out.quarantine else "extracted"
    st.quarantine_reason = out.quarantine_reason
    await db.commit()
    assert st.status == "extracted"
    assert await promote_judgment_staging(db, st) == "promoted"
    await db.commit()
    assert st.promoted_to_id is not None


async def test_release_unreported_honours_source_pin(db, source):
    from scraper.tasks.promotion import _quarantine

    html = f"<html><body><p>{SHC_BODY}</p></body></html>"
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/shc/pin-a", content=html.encode(), content_kind="html")
    shc = await stage_judgment(db, source=source, prov=prov, raw_html=html, raw_text=clean_html(html), url=prov.source_url)
    shc.source_name = "SindhHighCourt"
    shc.reconciled_json = {"citations": [], "court": "High Court of Sindh", "year": 2024, "full_text_candidate": shc.raw_text}
    await _quarantine(db, shc, NO_CITATION, "judgment")

    prov2 = await record_provenance(db, source=source, url="http://127.0.0.1/sc/other", content=html.encode(), content_kind="html")
    other = await stage_judgment(db, source=source, prov=prov2, raw_html=html, raw_text=clean_html(html), url=prov2.source_url)
    other.source_name = "SupremeCourt"
    other.reconciled_json = shc.reconciled_json
    await _quarantine(db, other, NO_CITATION, "judgment")
    await db.commit()

    assert await release_unreported(db, limit=10, source_name="SindhHighCourt") == 1
    await db.refresh(shc)
    await db.refresh(other)
    assert shc.status == "extracted"
    assert other.status == "quarantined"
