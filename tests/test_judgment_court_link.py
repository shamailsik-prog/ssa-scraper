"""Link NULL court_id PLS judgments to court rows (#292 follow-up)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import func, select, text

from scraper.config import settings
from scraper.judgment_court_consistency import consistency_report
from scraper.judgment_court_link_registry import (
    AJK_HC_NAME,
    AJK_SC_NAME,
    election_tribunal_seed_spec,
    normalize_link_label_key,
    pls_court_label_key,
    resolve_ajk_court_from_header,
    resolve_null_court_id_link,
)
from scraper.models import Court, Judgment
from scraper.tasks.judgment_court_link import apply_court_id_links, find_court_id_links, run_judgment_court_link


@pytest.mark.parametrize(
    "citation,header,expected",
    [
        (
            "1984 CLC 526",
            "1984 CLC 526\n[S.C. (AJ&K)]\nBefore A and B, JJ\n"
            + ("mention High Court elsewhere " * 20),
            AJK_SC_NAME,
        ),
        (
            "1992 CLC 1861",
            "1992 CLC 1861\n[H.C. (AJ&K)]\nBefore X, J\n",
            AJK_HC_NAME,
        ),
        (
            "1989 CLC 411",
            "1989 CLC 411\n[SC (AJ&K)]\nBefore Coram, JJ\n",
            AJK_SC_NAME,
        ),
        (
            "1992 CLC 1844",
            "1992 CLC 1844\n[S C(AJ&K)]\nBefore Y, J\n",
            AJK_SC_NAME,
        ),
        (
            "1992 CLC 1966",
            "1992 CLC 1966\n[Supreme Court (AJ&K)]\nBefore Z, JJ\n",
            AJK_SC_NAME,
        ),
        (
            "1997 CLC 366",
            "1997 CLC 366\n[Azad J & K]\nBefore Bench, J\n",
            AJK_HC_NAME,
        ),
        (
            "1980 CLC 30",
            "1980 CLC 30\nPresent.\nJustice A\n",
            AJK_SC_NAME,
        ),
        (
            "1993 CLC 1042",
            "1993 CLC 1042\n[Azad J & K]\nBefore Khawaja Muhammad Sharif J\n",
            AJK_HC_NAME,
        ),
        (
            "1997 CLC 1925",
            "1997 CLC 1925\n[S.C. (AJ&K)]\nBefore Coram, JJ\n",
            AJK_SC_NAME,
        ),
        (
            "2017 CLC 234",
            "2017 CLC 234\nPresent:\nJustice A, Justice B\n",
            AJK_SC_NAME,
        ),
    ],
)
def test_ajk_header_split_clc_fixtures(citation, header, expected):
    res = resolve_ajk_court_from_header(header)
    assert res is not None and not res.manual_review
    assert res.target_court_name == expected
    link = resolve_null_court_id_link("SUPREME-COURT-AZAD-KASHMIR", header)
    assert link is not None and link.target_court_name == expected


def test_ajk_shariat_court_manual_review():
    text = "1995 CLC 1\n[Shariat Court of Azad Jammu and Kashmir]\n"
    res = resolve_null_court_id_link("SUPREME-COURT-AZAD-KASHMIR", text)
    assert res is not None and res.manual_review


def test_ajk_karachi_bracket_manual_review():
    text = "2001 CLC 9\n[Karachi]\nBefore someone, J\n"
    res = resolve_ajk_court_from_header(text)
    assert res is not None and res.manual_review


def test_ajk_no_bracket_heading_manual_review():
    text = "1990 CLC 148\nIN THE SUPREME COURT OF AZAD JAMMU & KASHMIR\nBefore A, JJ\n"
    res = resolve_ajk_court_from_header(text)
    assert res is not None and res.manual_review


def test_manual_review_supreme_court_slug():
    res = resolve_null_court_id_link("SUPREME-COURT", "PLD 2020 SC 1\n")
    assert res is not None and res.manual_review


def test_label_normalization_appellate_and_northern_areas():
    assert "appellate" in normalize_link_label_key("labour appelant tribunal nwfp")
    assert "appellate" in normalize_link_label_key("labour appellant tribunal sindh")
    assert "tribunal" in normalize_link_label_key("election tribanal punjab")
    assert "gilgit baltistan" in pls_court_label_key("CHIEF-COURT-NORTHERN-AREAS")


def test_sac_gb_northern_areas_court_of_appeals():
    res = resolve_null_court_id_link("NORTHERN-AREAS-COURT-OF-APPEALS", "x" * 50)
    assert res is not None
    assert res.target_court_name == "Supreme Appellate Court Gilgit-Baltistan"


def test_constitutional_court_south_africa_alias():
    res = resolve_null_court_id_link("CONSTITUTIONAL-COURT-OF-SOUTH-AFRICA", "x" * 50)
    assert res is not None
    assert res.target_court_name == "Constitutional Court of South Africa"


def test_shariat_appellate_jurisdiction_maps_to_sc_sab():
    res = resolve_null_court_id_link("SHARIAT-APPELLATE-JURISDICTION", "x" * 50)
    assert res is not None
    assert res.target_court_name == "Shariat Appellate Bench, Supreme Court"


def test_labour_nwfp_resolves_to_kpk_seed():
    res = resolve_null_court_id_link("LABOUR-APPELLATE-TRIBUNAL-NWFP", "x" * 100)
    assert res is not None
    assert res.target_court_name == "Khyber Pakhtunkhwa Labour Appellate Tribunal"
    assert not res.uncreatable


def test_labour_ajk_manual_review():
    res = resolve_null_court_id_link("LABOUR-APPELLATE-TRIBUNAL-AZAD-KASHMIR", "x" * 100)
    assert res is not None and res.manual_review


def test_election_tribunal_short_code_avoids_clash():
    a = election_tribunal_seed_spec("Election Tribunal Punjab")
    b = election_tribunal_seed_spec("Election Tribunal Punjab")
    assert a["short_code"] == b["short_code"]
    c = election_tribunal_seed_spec("Election Tribunal Sindh")
    assert c["short_code"] != a["short_code"]
    taken = election_tribunal_seed_spec(
        "Election Tribunal Punjab",
        extra_reserved={"ELECTION_TRIBUNAL_PUNJAB"},
    )
    assert taken["short_code"] != "ELECTION_TRIBUNAL_PUNJAB"


@pytest.mark.asyncio
async def test_link_dry_run_does_not_create_courts(db):
    before = (await db.execute(select(func.count()).select_from(Court))).scalar()
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="SUPREME-COURT-AZAD-KASHMIR",
            court_id=None,
            canonical_citation="1990 CLC 1",
            full_text="1990 CLC 1\n[Supreme Court (AJ&K)]\nBefore A and B, JJ\n" + ("x" * 400),
            full_text_hash="ajk_sc_link_dry",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    out = await find_court_id_links(db, create=False)
    after = (await db.execute(select(func.count()).select_from(Court))).scalar()
    assert after == before
    assert out["count"] == out["links_ready"] + out["would_create_count"]


@pytest.mark.asyncio
async def test_dry_run_and_apply_counts_align(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="LABOUR-APPELLATE-TRIBUNAL-NWFP",
            court_id=None,
            canonical_citation="2001 CLC 50",
            full_text="2001 CLC 50\n" + ("z" * 600),
            full_text_hash="lat_nwfp",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    dry = await find_court_id_links(db, create=False)
    wet = await find_court_id_links(db, create=True)
    assert dry["count"] == wet["count"]
    assert wet["would_create_count"] == 0
    assert len(wet["links"]) == dry["count"]


@pytest.mark.asyncio
async def test_link_apply_and_undo_restore_court_id(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    ajk_sc = (await db.execute(select(Court).where(Court.short_code == "AJK-SC"))).scalars().first()
    if ajk_sc is None:
        ajk_sc = Court(name=AJK_SC_NAME, short_code="AJK-SC", court_level="supreme_court")
        db.add(ajk_sc)
        await db.flush()
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="SUPREME-COURT-AZAD-KASHMIR",
            court_id=None,
            canonical_citation="1991 CLC 2",
            full_text="1991 CLC 2\n[Supreme Court (AJ&K)]\nBefore A and B, JJ\n" + ("y" * 400),
            full_text_hash="ajk_sc_link_apply",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    info = await find_court_id_links(db, create=True)
    link = next(x for x in info["links"] if x["id"] == str(jid))
    run_id = "linktest01"
    await apply_court_id_links(db, run_id, [link])
    await db.commit()
    j = await db.get(Judgment, jid)
    assert j.court_id == ajk_sc.id


@pytest.mark.asyncio
async def test_noop_apply_skips_undo_run(db, monkeypatch):
    monkeypatch.setattr(settings, "SSA_JUDGMENT_COURT_ID_BACKFILL", True)
    jid = uuid4()
    db.add(
        Judgment(
            id=jid,
            source_name="PakistanLawSite",
            court_name="SUPREME-COURT-AZAD-KASHMIR",
            court_id=None,
            canonical_citation="2010 CLC 9",
            full_text="2010 CLC 9\nPresent:\nA, B\n" + ("p" * 400),
            full_text_hash="noop_link",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    info = await find_court_id_links(db, create=True, limit=500)
    link = next((x for x in info["links"] if x["id"] == str(jid)), None)
    assert link is not None
    first = await apply_court_id_links(db, "runonce", [link])
    await db.commit()
    assert first["court_ids_updated"] == 1
    second = await apply_court_id_links(db, "runagain", [link])
    assert second["court_ids_updated"] == 0
    snap = (
        await db.execute(
            text("SELECT count(*) FROM corpus_metadata WHERE key = 'judgment_court_link_undo:runagain'")
        )
    ).scalar()
    assert snap == 0


@pytest.mark.asyncio
async def test_consistency_report_splits_null_and_mismatch(db):
    sc = (await db.execute(select(Court).where(Court.name == "Supreme Court of Pakistan"))).scalars().first()
    if sc is None:
        sc = Court(name="Supreme Court of Pakistan", short_code="SC_PAK", court_level="supreme")
        db.add(sc)
        await db.flush()
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="Federal Shariat Court",
            court_id=None,
            canonical_citation="2000 CLC 3",
            full_text="x" * 500,
            full_text_hash="null_court_id_row",
            access_method="rendered_copy",
        )
    )
    db.add(
        Judgment(
            id=uuid4(),
            source_name="PakistanLawSite",
            court_name="Supreme Court of Bangladesh",
            court_id=sc.id,
            canonical_citation="2000 CLC 4",
            full_text="x" * 500,
            full_text_hash="mismatch_row",
            access_method="rendered_copy",
        )
    )
    await db.commit()
    report = await consistency_report(db)
    assert report["null_court_id_count"] >= 1
    assert report["real_mismatch_count"] >= 1
