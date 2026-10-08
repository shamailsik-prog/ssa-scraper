"""Statutes read against their own table of contents (PakistanCode texts, 7 October 2026), and the
migration that clears the old PakistanCode statutes for a verified re-scrape."""

from __future__ import annotations

import importlib
from pathlib import Path

from sqlalchemy import func, select

from scraper.database import engine
from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute
from scraper.models import CrawlFrontier, Instrument, InstrumentRelation, ScraperSource, Statute, StatuteSection, StatuteSectionVersion, StatutesStaging
from scraper.parsers.statute_contents import clean_title, read_statute, title_matches

FIX = Path(__file__).parent / "fixtures_statutes"


def _text(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_short_act_is_read_and_verified_against_its_contents():
    r = read_statute(_text("whipping_act_1996.txt"), "Abolition of the Punishment of Whipping Act, 1996")
    v = r["verification"]
    assert v["method"] == "contents" and v["verified"] is True
    assert [s["section_number"] for s in r["sections"]] == ["1", "2", "3", "4"]
    repeal = r["sections"][-1]
    assert repeal["section_title"].startswith("Repeal")
    # the preamble that follows the contents list is not glued onto the last heading
    assert "WHEREAS" not in repeal["section_title"]
    assert "Whipping Act, 1909" in repeal["section_text"]
    assert "Page " not in repeal["section_text"]


def test_lettered_section_spaced_in_the_body_is_found():
    r = read_statute(_text("area_study_centres_act_1975.txt"), "Area Study centres Act, 1975")
    assert r["verification"]["verified"] is True
    nine_a = next(s for s in r["sections"] if s["section_number"] == "9A")  # printed "1[9 A." in the body
    assert "Federal Government may" in nine_a["section_text"]


def test_contents_list_without_a_contents_heading():
    r = read_statute(_text("asaan_karobar_act_2025.txt"), "Asaan Karobar Act, 2025")
    assert r["verification"]["verified"] is True and r["verification"]["found"] == 25


def test_no_contents_list_and_a_lost_heading_is_held_not_merged():
    """Section 10's heading is missing from the PDF's text: sections 11-24 are still read, and the
    statute fails the check instead of folding them into section 9."""
    r = read_statute(_text("alternative_energy_board_act_2010.txt"), "Alternative Energy Development Board Act, 2010")
    v = r["verification"]
    assert v["method"] == "sequence" and v["verified"] is False
    assert v["missing"] == ["10"] and "skips section(s) 10" in v["reason"]
    numbers = [s["section_number"] for s in r["sections"]]
    assert numbers[:9] == [str(n) for n in range(1, 10)] and numbers[-1] == "24"
    assert "11" in numbers  # "11." was once read as an amendment marker "1" + section "1."


def test_titles_status_notes_and_scans():
    assert clean_title("Anti-Dumping Duties Ordinance, 2000 (Repealed)") == "Anti-Dumping Duties Ordinance, 2000"
    assert title_matches("Anti-Dumping Duties Ordinance, 2000 (Repealed)", "THE ANTI-DUMPING DUTIES ORDINANCE, 2000\nCONTENTS")
    assert not title_matches("Companies Act, 2017", "THE ADMINISTRATOR GENERAL'S ACT, 1913")
    scan = read_statute("   \n ", "Agricultural Development Bank Ordinance 1961")
    assert scan["verification"]["verified"] is False and "OCR" in scan["verification"]["reason"]


def test_pakistancode_extraction_names_from_the_listing_and_records_the_verdict():
    out = extract_statute_deterministic(
        html=None,
        text=_text("whipping_act_1996.txt"),
        source_meta={"source_name": "PakistanCode", "act_title": "Abolition of the Punishment of Whipping Act, 1996"},
    )
    assert out["statute_name"] == "Abolition of the Punishment of Whipping Act, 1996"
    assert out["field_evidence"]["contents_check"].startswith("verified (contents): 4 of 4")
    assert len(out["sections"]) == 4 and out["extractor_confidence"] >= 0.9


migration = importlib.import_module("migrations.013_rescrape_pakistancode_statutes")
BACKUPS = ", ".join(
    f"backup_20261007_{t}" for t in ("statute", "statute_section", "statute_section_version", "instrument_relation", "instrument_section_relation")
)


async def test_migration_backs_up_clears_and_requeues_only_pakistancode(db):
    pc = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    na = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NationalAssembly"))).scalars().first()
    old = Statute(name="This Act may be called the Court fees Act 1870", source_name="PakistanCode", jurisdiction="Federal")
    keep = Statute(name="Some Bill Act, 2020", source_name="NationalAssembly", jurisdiction="Federal")
    db.add_all([old, keep])
    await db.flush()
    sec = StatuteSection(statute_id=old.id, section_number="1")
    db.add(sec)
    await db.flush()
    db.add(StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="Short title", text_hash="h1"))
    # an instrument linked only to the PakistanCode statute: the link cannot be left pointing nowhere
    # (ck_instrument_relation_target_present stopped the first run of 013 on the server, 7 October 2026)
    inst = Instrument(type="act", title="Laws (Continuance in Force) Order, 1977", source_name="PakistanCode")
    other = Instrument(type="act", title="Some Order", source_name="GazetteOfPakistan")
    db.add_all([inst, other])
    await db.flush()
    edge = dict(relation_phrase="read with", target_mention_raw="Court fees Act", target_mention_normalized="court fees act", span_start=0, span_end=5, evidence_snippet="read with")
    db.add(InstrumentRelation(source_instrument_id=inst.id, target_statute_id=old.id, relation_type="read_with", **edge))
    db.add(InstrumentRelation(source_instrument_id=inst.id, target_instrument_id=other.id, target_statute_id=old.id, relation_type="amended_by", **edge))
    prov = await record_provenance(db, source=pc, url="https://pakistancode.gov.pk/pdffiles/x.pdf", content=b"%PDF-1.4 x", content_kind="pdf")
    await stage_statute(db, source=pc, prov=prov, raw_html=None, raw_text="x", url="https://pakistancode.gov.pk/pdffiles/x.pdf", kind="statute")
    db.add(CrawlFrontier(source_name="PakistanCode", tier=0, query_key="doc:x", query_json={"kind": "document"}, cursor_json={}, status="done", attempts=2, last_error="old"))
    db.add(CrawlFrontier(source_name="NationalAssembly", tier=0, query_key="doc:y", query_json={"kind": "document"}, cursor_json={}, status="done"))
    await db.commit()
    assert na is not None

    async with engine.begin() as conn:
        # the test database ran every migration at start-up; drop its (empty) backups to run 013 afresh
        await conn.exec_driver_sql(f"DROP TABLE IF EXISTS {BACKUPS}")
        await migration.upgrade(conn)
    db.expire_all()

    names = set((await db.execute(select(Statute.name))).scalars().all())
    assert names == {"Some Bill Act, 2020"}  # the PakistanCode statute is gone, the other source's stays
    assert (await db.execute(select(func.count()).select_from(StatuteSectionVersion))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(StatutesStaging).where(StatutesStaging.source_name == "PakistanCode"))).scalar() == 0
    fr = {f.source_name: f for f in (await db.execute(select(CrawlFrontier))).scalars().all()}
    assert (fr["PakistanCode"].status, fr["PakistanCode"].attempts, fr["PakistanCode"].last_error) == ("pending", 0, None)
    assert fr["NationalAssembly"].status == "done"
    async with engine.begin() as conn:
        backed = (await conn.exec_driver_sql("SELECT name FROM backup_20261007_statute")).scalars().all()
        backed_text = (await conn.exec_driver_sql("SELECT section_text FROM backup_20261007_statute_section_version")).scalars().all()
        backed_links = (await conn.exec_driver_sql("SELECT relation_type FROM backup_20261007_instrument_relation ORDER BY 1")).scalars().all()
        await conn.exec_driver_sql(f"DROP TABLE {BACKUPS}")
    assert backed_links == ["amended_by", "read_with"]
    links = (await db.execute(select(InstrumentRelation))).scalars().all()
    assert [(r.relation_type, r.target_statute_id) for r in links] == [("amended_by", None)]  # the link to another instrument stays
    assert backed == ["This Act may be called the Court fees Act 1870"] and backed_text == ["Short title"]
