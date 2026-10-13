"""An unreported judgment from a court's website enters the corpus marked unreported instead of being held."""

from __future__ import annotations

from sqlalchemy import select

from scraper.config import settings
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.fetchers import record_provenance, stage_judgment
from scraper.models import Judgment, QuarantineQueue
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks.promotion import _quarantine, promote_judgment_staging, release_unreported

BODY = (
    "The applicant seeks post-arrest bail in a case registered under section 489-F of the Pakistan Penal Code. "
    "Learned counsel for the applicant submits that the offence does not fall within the prohibitory clause of "
    "section 497 of the Code of Criminal Procedure and that the applicant has been in custody for four months. "
    "The learned Deputy Prosecutor General opposes the application. Having heard the parties and perused the record, "
    "the case of the applicant calls for further inquiry. The application is allowed subject to his furnishing "
    "solvent surety in the sum of Rs.100,000 and a personal bond in the like amount to the satisfaction of the trial court."
)


def _html(case_no: str, decided: str, body: str = BODY) -> str:
    return (
        "<html><body><h1>IN THE HIGH COURT OF SINDH AT KARACHI</h1>"
        f"<p>{case_no}</p><p>Muhammad Ali versus The State</p>"
        f"<p>Before Mr. Justice Ahmed Khan</p><p>Date of decision: {decided}</p>"
        f"<p>ORDER</p><p>{body}</p></body></html>"
    )


async def _held(db, source, case_no="Criminal Bail Application No. 1234 of 2024", decided="1st May, 2024", body=BODY, route=None):
    """Stage and extract a court-website judgment that names no law report: it comes out held."""
    html = _html(case_no, decided, body)
    prov = await record_provenance(db, source=source, url=f"http://127.0.0.1/shc/{abs(hash((case_no, body)))}", content=html.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=html, raw_text=clean_html(html), url=prov.source_url, route=route)
    o = await HybridExtractor(db, source).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
    st.reconciled_json, st.confidence_score = o.data, o.confidence
    await _quarantine(db, st, "no citation supported by source", "judgment")
    await db.commit()
    return st


async def test_a_held_court_judgment_is_released_and_promoted_as_unreported(db, source):
    st = await _held(db, source)
    assert await release_unreported(db) == 1
    assert st.status == "extracted"
    assert await promote_judgment_staging(db, st) == "promoted"
    await db.commit()

    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    assert j.unreported is True and j.reporter is None
    assert j.proceeding_type == "bail"
    assert j.docket_number == "Criminal Bail Application No. 1234 of 2024"
    assert j.canonical_citation.startswith("UNREPORTED SC Criminal Bail Application No. 1234 of 2024")
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert item.reviewed is True and item.resolution == "released_unreported"


async def test_the_listing_rows_case_number_is_the_identity(db, source):
    st = await _held(db, source, case_no="Order sheet", route={"result_case_no": "C.P. No. D-5678 of 2023"})
    await release_unreported(db)
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    assert j.docket_number == "C.P. No. D-5678 of 2023" and j.proceeding_type == "constitutional_petition"


async def test_two_orders_in_the_same_case_on_the_same_day_both_enter(db, source):
    a = await _held(db, source)
    b = await _held(db, source, body=BODY.replace("four months", "six months") + " Interim order.")
    await release_unreported(db)
    assert await promote_judgment_staging(db, a) == "promoted"
    assert await promote_judgment_staging(db, b) == "promoted"
    ja, jb = [(await db.execute(select(Judgment).where(Judgment.id == s.promoted_to_id))).scalars().one() for s in (a, b)]
    assert ja.canonical_citation != jb.canonical_citation


async def test_the_same_judgment_captured_twice_is_a_duplicate(db, source):
    a = await _held(db, source)
    await release_unreported(db)
    assert await promote_judgment_staging(db, a) == "promoted"
    b = await _held(db, source)  # same page fetched again
    b.status = "extracted"
    assert await promote_judgment_staging(db, b) == "duplicate"


async def test_a_capture_too_short_to_be_a_judgment_stays_held_with_its_reason(db, source):
    st = await _held(db, source, body="Adjourned.")
    assert await release_unreported(db) == 0
    assert st.status == "quarantined"
    assert st.quarantine_reason == "no citation supported by source (not taken as unreported: text too short for a judgment)"
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert item.reviewed is False and item.reason == st.quarantine_reason  # still waits for a person, and says why
    assert await release_unreported(db) == 0  # never picked again


async def test_promotion_itself_still_refuses_a_short_capture(db, source):
    st = await _held(db, source, body="Adjourned.")
    st.status = "extracted"
    assert await promote_judgment_staging(db, st) == "quarantined"
    assert st.quarantine_reason == "unreported judgment held: text too short for a judgment"


async def test_pakistanlawsite_without_a_citation_is_still_held(db, source):
    """PakistanLawSite publishes only reported judgments: one without a citation is a bad capture."""
    st = await _held(db, source)
    st.source_name = "PakistanLawSite"
    await db.commit()
    assert await release_unreported(db) == 0
    st.status = "extracted"
    assert await promote_judgment_staging(db, st) == "quarantined"
    assert st.quarantine_reason == "no citation supported by source"


async def test_switching_the_setting_off_keeps_them_held(db, source, monkeypatch):
    monkeypatch.setattr(settings, "UNREPORTED_JUDGMENTS", False)
    st = await _held(db, source)
    assert await release_unreported(db) == 0
    st.status = "extracted"
    assert await promote_judgment_staging(db, st) == "quarantined"
    assert st.quarantine_reason == "no citation supported by source"


async def test_drive_keeps_unreported_judgments_by_court_and_proceeding(db, source):
    from scraper.corpus_record import judgment_record, validate_record
    from scraper.storage.archive import judgment_prefix

    st = await _held(db, source)
    await release_unreported(db)
    await promote_judgment_staging(db, st)
    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    prefix = judgment_prefix(j)
    assert prefix.startswith("Unreported/") and "/Bail/" in prefix
    rec = judgment_record(j, has_original_pdf=False)
    assert rec["unreported"] is True and rec["reported"] is False and rec["proceeding_type"] == "bail"
    assert validate_record(rec) == []


def test_the_viewer_lists_the_unreported_section_by_proceeding(client, admin_headers):
    from scraper.database import SessionLocal, run_async

    async def seed():
        async with SessionLocal() as db:
            db.add(Judgment(canonical_citation="UNREPORTED SHC Suit No. 9 of 2020 (2021-01-02)", full_text_hash="9" * 64, full_text=BODY, court_name="High Court of Sindh", source_name="SindhHighCourt", unreported=True, proceeding_type="civil_suit", docket_number="Suit No. 9 of 2020"))
            db.add(Judgment(canonical_citation="PLD 2020 SC 1", full_text_hash="8" * 64, full_text=BODY, court_name="Supreme Court of Pakistan", source_name="SupremeCourt", reporter="PLD"))
            await db.commit()

    run_async(seed())
    r = client.get("/admin/corpus/judgments?section=unreported", headers=admin_headers).json()
    assert [i["case_number"] for i in r["items"]] == ["Suit No. 9 of 2020"] and r["items"][0]["proceeding_type"] == "civil_suit"
    r = client.get("/admin/corpus/judgments?section=reported", headers=admin_headers).json()
    assert [i["citation"] for i in r["items"]] == ["PLD 2020 SC 1"]
    r = client.get("/admin/corpus/judgments?section=unreported&proceeding_type=bail", headers=admin_headers).json()
    assert r["total"] == 0
    s = client.get("/admin/corpus/unreported", headers=admin_headers).json()
    assert s["total"] == 1 and {p["key"]: p["count"] for p in s["proceeding_types"]}["civil_suit"] == 1


async def test_an_unreported_judgment_that_later_gets_its_citation_takes_it(db, source):
    from scraper.models import Citation

    st = await _held(db, source)
    await release_unreported(db)
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    old_identity = j.canonical_citation

    again = await _held(db, source)  # the same judgment, now with the citation its result row names
    again.status, again.quarantine_reason = "extracted", None
    again.reconciled_json = {**(again.reconciled_json or {}), "citations": ["2024 SCMR 77"]}
    assert await promote_judgment_staging(db, again) == "duplicate"
    await db.refresh(j)
    assert j.canonical_citation == "2024 SCMR 77" and j.unreported is False and j.reporter == "SCMR"
    aliases = {c.citation_string for c in (await db.execute(select(Citation).where(Citation.judgment_id == j.id))).scalars().all()}
    assert old_identity in aliases and "2024 SCMR 77" in aliases


async def test_an_older_hold_recorded_only_on_its_review_item_is_released(db, source):
    """Rows held before the reason was copied onto the staging row carry it only on the review item."""
    st = await _held(db, source)
    st.quarantine_reason = None
    await db.commit()
    assert await release_unreported(db) == 1
    assert await promote_judgment_staging(db, st) == "promoted"


async def test_the_court_is_the_websites_not_a_court_the_text_names(db, source):
    """A Sindh High Court order that discusses a Supreme Court precedent is still a Sindh High Court order."""
    st = await _held(db, source)
    st.source_name = "SindhHighCourt"
    st.reconciled_json = {**(st.reconciled_json or {}), "court": "Supreme Court of Pakistan", "court_canonical": "Supreme Court of Pakistan"}
    await db.commit()
    assert await release_unreported(db) == 1
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    assert j.court_name == "High Court of Sindh" and j.canonical_citation.startswith("UNREPORTED SHC ")


async def test_a_judgment_held_for_an_unknown_court_is_released_when_its_website_names_the_court(db, source):
    st = await _held(db, source)
    st.source_name = "SupremeAppellateCourtGB"
    st.quarantine_reason = "no citation supported by source (not taken as unreported: court unknown)"
    st.reconciled_json = {k: v for k, v in (st.reconciled_json or {}).items() if k not in ("court", "court_canonical")}
    await db.commit()
    assert await release_unreported(db) == 1
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment).where(Judgment.id == st.promoted_to_id))).scalars().one()
    assert j.court_name == "Supreme Appellate Court Gilgit-Baltistan"


async def test_migration_015_refiles_earlier_unreported_judgments_under_their_websites_court(db):
    import importlib

    from scraper.database import engine
    from scraper.models import Court

    sc = (await db.execute(select(Court).where(Court.short_code == "SC"))).scalars().one()
    db.add(Judgment(canonical_citation="UNREPORTED SHC C.P. No. D-1 of 2024 (2024-01-02)", full_text_hash="7" * 64, full_text=BODY, court_id=sc.id, court_name=sc.name, source_name="SindhHighCourt", unreported=True))
    db.add(Judgment(canonical_citation="PLD 2020 SC 9", full_text_hash="6" * 64, full_text=BODY, court_id=sc.id, court_name=sc.name, source_name="SindhHighCourt", reporter="PLD"))
    await db.commit()
    mig = importlib.import_module("migrations.015_unreported_court_from_source")
    async with engine.begin() as conn:
        await mig.upgrade(conn)
    db.expire_all()
    rows = {j.canonical_citation: j.court_name for j in (await db.execute(select(Judgment))).scalars().all()}
    assert rows["UNREPORTED SHC C.P. No. D-1 of 2024 (2024-01-02)"] == "High Court of Sindh"
    assert rows["PLD 2020 SC 9"] == "Supreme Court of Pakistan"  # a reported judgment is never touched
