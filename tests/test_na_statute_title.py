"""NA and Senate statute names come from the instrument, not OCR or listing chrome."""

from __future__ import annotations

import csv
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import pytest
from sqlalchemy import func, select

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute, statute_staging_identity_url
from scraper.models import QuarantineQueue, ScraperSource, Statute
from scraper.statute_quality.assembly_statute_title import (
    _amending_spans,
    _citation_prefix,
    _cited_upstream_name,
    _combining_suffix_at_end,
    _connector_between_years,
    _enacted_year,
    _listing_is_another_principal,
    _short_title,
    _short_title_supersedes,
    _title_core,
    _verbatim_good_listing,
    apply_rejected_assembly_title,
    is_rejected_assembly_statute_name,
    resolve_assembly_statute_title,
    strip_listing_chrome_source_url,
)
from scraper.tasks.promotion import promote_statute_staging

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "na_agri_trade_food_safety_authority_act_2026_p1_p2.txt"
RECORDED = Path(__file__).resolve().parent / "fixtures" / "na_agri_trade_food_safety_authority_act_2026_pdftotext.txt"
REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "na_statute_title_dry_run.py"
TITLE = "National Agri-trade and Food Safety Authority Act, 2026"
PDF = "https://na.gov.pk/uploads/documents/6965f5957687e_838.pdf"
CHROME = "|Share Print Acts of Parliament Filter by"
DIRTY_URL = PDF + CHROME

JUNK_NAMES = (
    "Itr this Act",
    "EL-TtG followiag Act",
    "The follorving Act",
    "The following Act",
    "REGTSTTRED No.-!4-:l!2",
    "REGISTERED No. M-302",
    "Share Print Acts of Parliament Filter by",
    "President's Orders and Regulations",
    "I / PART / Act",
    "Brcsident's Orders and Regulations",
    "AN / ACT / furlher to amend the Pakistan Air Force Act, 1953",
)

GOOD_NAMES = (
    "Finance Act, 2026",
    "Explosives Act, 1884",
    "Guardians and Wards Act, 1890",
    "Pakistan Penal Code, 1860",
    "Trust Act, 1882",
    "Societies Registration Act, 1860",
    "The National Command Authority (Amendment) Act, 2026",
)


def _fixture() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _extract(text: str, **meta):
    source_meta = {"source_name": "NationalAssembly"}
    source_meta.update(meta)
    return extract_statute_deterministic(html=None, text=text, source_meta=source_meta)


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_agri_pdf_text_yields_short_title_and_year(source_name):
    out = _extract(
        _fixture(),
        source_name=source_name,
        act_title=CHROME,
        url=DIRTY_URL,
    )
    assert out["statute_name"] == TITLE
    assert out["year_enacted"] == 2026
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.parametrize("junk", JUNK_NAMES)
def test_junk_name_is_rejected(junk):
    assert is_rejected_assembly_statute_name(junk)


@pytest.mark.parametrize("junk", JUNK_NAMES)
def test_junk_body_is_quarantined_as_name_unknown(junk):
    body = (
        f"{junk}\n"
        "2. Definitions. Words in this instrument have the meaning given below.\n"
        "A board means the body constituted for that purpose.\n"
    )
    out = _extract(body)
    assert out["statute_name"] == "name_unknown"
    assert out["year_enacted"] != 1953
    assert (out.get("field_evidence") or {}).get("name_unknown")


def test_air_force_amendment_year_comes_from_the_short_title():
    text = """
ACT NO. XII OF 2025
AN ACT
further to amend the Pakistan Air Force Act, 1953
1. Short title and commencement.—(1) This Act shall be called the Pakistan Air Force (Amendment) Act, 2025.
(2) It shall come into force at once.
2. Amendment. In the said Act a new section is inserted.
"""
    out = _extract(text, act_title="AN / ACT / furlher to amend the Pakistan Air Force Act, 1953")
    assert out["statute_name"] == "Pakistan Air Force (Amendment) Act, 2025"
    assert out["year_enacted"] == 2025


def test_heading_supplies_the_title_when_the_short_title_line_is_missing():
    text = """
Acr No. lY OF 2026
AN
ACT
to esltblish rhe Notionul Agri-trade and Food Safety '4uthoriq)'
WHEREAS it is expedient to regulate exports.
2. Definitions. Words have the meaning given to them.
"""
    decision = resolve_assembly_statute_title(text)
    assert decision.source == "act_heading"
    assert decision.name == TITLE
    assert decision.year == 2026


def test_listing_title_is_used_when_the_pdf_has_no_heading():
    text = "2. Definitions. Words have the meaning given to them in the schedule.\n"
    decision = resolve_assembly_statute_title(text, listing_title="Finance Act, 2026")
    assert decision.source == "listing"
    assert decision.name == "Finance Act, 2026"
    assert decision.year == 2026


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_names_are_not_rejected(name):
    assert not is_rejected_assembly_statute_name(name)


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_good_listing_names_stay_unchanged(name):
    body = (
        "1. Short title. This Act may be called the Something Else Act, 1999.\n"
        "(2) It extends to the whole of Pakistan.\n"
        "2. Definitions. In this Act a word has its ordinary meaning.\n"
    )
    out = _extract(body, act_title=name)
    assert out["statute_name"] == name
    assert str(out["year_enacted"]) in name


def test_source_url_listing_chrome_is_stripped_and_real_labels_stay():
    assert strip_listing_chrome_source_url(DIRTY_URL) == PDF
    bills = "https://na.gov.pk/uploads/documents/abc.pdf|Share Print Government Bills Laid in NA"
    private = "https://na.gov.pk/uploads/documents/abc.pdf|Share Print Private Member's Bills Laid"
    assert strip_listing_chrome_source_url(bills) == "https://na.gov.pk/uploads/documents/abc.pdf"
    assert strip_listing_chrome_source_url(private) == "https://na.gov.pk/uploads/documents/abc.pdf"
    numbered = "https://na.gov.pk/uploads/documents/abc.pdf|XLVIII of 2026"
    titled = "https://na.gov.pk/uploads/documents/abc.pdf|Finance Act, 2026"
    assert strip_listing_chrome_source_url(numbered) == numbered
    assert strip_listing_chrome_source_url(titled) == titled
    assert statute_staging_identity_url(DIRTY_URL, {"act_title": "Finance Act, 2026"}) == (
        PDF + "|Finance Act, 2026"
    )
    assert statute_staging_identity_url(DIRTY_URL, {"act_no": "IV of 2026"}) == PDF + "|IV of 2026"
    assert statute_staging_identity_url(DIRTY_URL, {"act_title": CHROME}) is None


async def test_stage_statute_stores_the_pdf_url_without_listing_chrome(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NationalAssembly"))
    ).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url=PDF,
        content=b"agri trade food safety",
        content_kind="pdf",
    )
    staged = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=_fixture(),
        url=DIRTY_URL,
        identity_key=DIRTY_URL,
        kind="statute",
    )
    assert staged.source_url == PDF
    kept = "https://na.gov.pk/uploads/documents/kept.pdf|XLVIII of 2026"
    prov_kept = await record_provenance(
        db,
        source=source,
        url="https://na.gov.pk/uploads/documents/kept.pdf",
        content=b"another act",
        content_kind="pdf",
    )
    staged_kept = await stage_statute(
        db,
        source=source,
        prov=prov_kept,
        raw_html=None,
        raw_text="Finance Act, 2026",
        url=kept,
        identity_key=kept,
        kind="statute",
    )
    assert staged_kept.source_url == kept


async def test_name_unknown_is_quarantined_and_not_stored(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NationalAssembly"))
    ).scalars().first()
    assert source is not None
    prov = await record_provenance(
        db,
        source=source,
        url="http://127.0.0.1/na-name-unknown.txt",
        content=b"name unknown row",
        content_kind="text",
    )
    staged = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text="Itr this Act",
        url="http://127.0.0.1/na-name-unknown.txt",
        kind="statute",
    )
    staged.status = "extracted"
    staged.reconciled_json = {
        "statute_name": "name_unknown",
        "jurisdiction": "Federal",
        "statute_type": "act",
        "field_evidence": {"name_unknown": "no short title, act heading, or listing title"},
        "sections": [
            {
                "section_number": "2",
                "section_title": "Definitions",
                "section_text": "2. Definitions. Itr this Act, unless there is anything repugnant in the subject or context.",
            }
        ],
    }
    assert await promote_statute_staging(db, staged) == "quarantined"
    queued = (
        await db.execute(select(QuarantineQueue).where(QuarantineQueue.statutes_staging_id == staged.id))
    ).scalars().first()
    assert queued is not None
    assert (queued.reason or "").startswith("name_unknown:")
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


def test_dry_run_prints_the_title_and_skips_rows_without_local_text(tmp_path):
    text_dir = tmp_path / "texts"
    text_dir.mkdir()
    (text_dir / "agri-1.txt").write_text(_fixture(), encoding="utf-8")
    csv_path = tmp_path / "hits.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["id", "stored_name", "year_enacted", "source_url", "na_sha"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "id": "agri-1",
                "stored_name": "Itr this Act",
                "year_enacted": "",
                "source_url": DIRTY_URL,
                "na_sha": "abc",
            }
        )
        writer.writerow(
            {
                "id": "missing-2",
                "stored_name": "Finance Act, 2026",
                "year_enacted": "2026",
                "source_url": DIRTY_URL,
                "na_sha": "no-such-sha",
            }
        )
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--csv", str(csv_path), "--text-dir", str(text_dir)],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(REPO),
    )
    rows = list(csv.DictReader(completed.stdout.splitlines()))
    assert rows[0]["proposed_name"] == TITLE
    assert rows[0]["proposed_year"] == "2026"
    assert rows[0]["source"] == "short_title"
    assert rows[0]["cleaned_source_url"] == PDF
    assert rows[1]["source"] == "no_local_text"
    assert rows[1]["proposed_name"] == ""
    assert "Finance Act" not in rows[1]["proposed_name"]


H2_TITLES = (
    "Armed Forces Act, 2026",
    "Employees Provident Fund Act, 2026",
    "Federally Administered Tribal Areas Act, 2026",
    "Civil Services Act, 2026",
    "Election Commissioner Act, 2026",
    "Pakistani Citizenship Act, 2026",
    "Food Trades Act, 2026",
    "Pakistan Institutes Act, 2026",
    "Corporations Act, 2026",
)

H4_CLAUSES = (
    "Amendment of section 2, Act XXIX of 1976.- In the Seed Act",
    "XXI oF 2025 An Actfurther to amend the Extradition Act, 1972",
    "called the Anti-Dumping Duties (Amendment) Act, 2025",
    "No. F. 22(68)/2025-Lcgis.-Thc lb!lowing Act of llallis-e'Shoora",
    "therefore, the existing provisions in the Pakistan Penal Code, 1860",
    "IV of 1969).- In the Customs Act",
    "AN ACT furlher lo amend rhe lzgal Practitioners and Bar Councils Act, 1973",
)

AIR_FORCE_OCR = """1. Short title and commencement. This Act shall be called the
-(1)
Pakistan Air F-orce (Amendment) Act, 2025.
(80s)
Price. Rs.5.00
0(2025)/Ex. Gaz.l
"""

SHIPPING_OCR = """1. Short title and commercement.{l) This Act shall be called the
Pakistan National Shipping Corporation (Amendment) Acr, 2024.
( 1e5)
Price: Rs 10.00
caz.l
t800(202a)/Ex.
THE GAZE'I'l']] OI' PAKIS i A\\ EXI-RA. JU\\E 12.1024
(2)
ll comc rnlo force al onc(.
"""


def _called(title: str) -> str:
    return (
        f"1. Short title. This Act shall be called the {title}.\n"
        "(2) It extends to the whole of Pakistan.\n"
    )


def test_recorded_pdftotext_fixture_is_the_audit_file():
    digest = __import__("hashlib").sha256(RECORDED.read_bytes()).hexdigest()
    assert digest == "7d2e77aaafaf1f27f3dffeb22e78ebbcbd42357bfdca17fa065c35d402537b21"
    text = RECORDED.read_text(encoding="utf-8")
    assert "'*qZOZA" in text
    assert "No. F. 22(68)/2025" in text
    assert text.index("No. F. 22(68)/2025") < text.index("'*qZOZA")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_recorded_layout_full_extractor_recovers_agri_title(source_name):
    out = _extract(RECORDED.read_text(encoding="utf-8"), source_name=source_name, act_title=CHROME, url=DIRTY_URL)
    assert out["statute_name"] == TITLE
    assert out["year_enacted"] == 2026


@pytest.mark.parametrize("title", H2_TITLES)
def test_clean_short_titles_are_not_rewritten(title):
    decision = resolve_assembly_statute_title(_called(title))
    assert decision.name == title


def test_air_force_stored_ocr_drops_the_price_line():
    decision = resolve_assembly_statute_title(AIR_FORCE_OCR)
    assert decision.name == "Pakistan Air Force (Amendment) Act, 2025"
    assert decision.year == 2025
    assert "Price" not in decision.name
    assert "Gaz" not in decision.name


def test_shipping_stored_ocr_drops_the_price_line():
    decision = resolve_assembly_statute_title(SHIPPING_OCR)
    assert decision.name == "Pakistan National Shipping Corporation (Amendment) Act, 2024"
    assert decision.year == 2024
    assert "Price" not in decision.name
    assert "GAZE" not in decision.name


@pytest.mark.parametrize("clause", H4_CLAUSES)
def test_clause_like_names_are_rejected(clause):
    assert is_rejected_assembly_statute_name(clause)


def test_registered_no_inside_a_dated_act_is_rejected():
    assert is_rejected_assembly_statute_name("REGISTERED No. Finance Act, 2026")


def test_may_be_called_is_a_short_title():
    decision = resolve_assembly_statute_title(
        "1. Short title. This Act may be called the Explosives Act, 1884.\n"
    )
    assert decision.name == "Explosives Act, 1884"
    assert decision.year == 1884
    assert decision.source == "short_title"


def test_gazette_date_supplies_the_year_when_the_short_title_has_none():
    text = (
        "Islamabad, the 3rd March, 2024\n"
        "1. Short title. This Act shall be called the Sample Library Act.\n"
        "(2) It extends to the whole of Pakistan.\n"
    )
    decision = resolve_assembly_statute_title(text)
    assert decision.name == "Sample Library Act, 2024"
    assert decision.year == 2024


def test_file_number_year_is_not_the_act_year():
    text = (
        "No. F. 22(68)/2019-Legis.\n"
        "Islamabad, the 3rd March, 2024\n"
        "1. Short title. This Act shall be called the Sample Library Act.\n"
    )
    decision = resolve_assembly_statute_title(text)
    assert decision.year == 2024
    assert decision.year != 2019


def test_short_title_year_wins_over_an_incoming_year():
    text = (
        "ACT NO. XII OF 2025\n"
        "1. This Act shall be called the Pakistan Air Force (Amendment) Act, 2025.\n"
    )
    name, year, _detail = apply_rejected_assembly_title(
        name="Itr this Act",
        year=1999,
        text=text,
    )
    assert name == "Pakistan Air Force (Amendment) Act, 2025"
    assert year == 2025


def test_short_title_year_must_agree_with_the_act_number():
    text = (
        "ACT NO. IV OF 2026\n"
        "1. This Act shall be called the Wrong Year Act, 2024.\n"
        "The National Agri-trade and Food Safety Authority Act, 2026 is cited in the schedule.\n"
    )
    decision = resolve_assembly_statute_title(
        text,
        listing_title="National Agri-trade and Food Safety Authority Act, 2026",
    )
    assert decision.name == "National Agri-trade and Food Safety Authority Act, 2026"
    assert decision.year == 2026
    assert decision.source == "listing"


def test_impossible_year_is_not_kept_or_doubled():
    text = (
        "Acr No. IV OF 2026\n"
        "1. This Act shall be called the E Dilsiotr Act, 2602.\n"
    )
    decision = resolve_assembly_statute_title(text)
    assert decision.name == "name_unknown"
    assert "2602, 2026" not in decision.name
    assert not is_rejected_assembly_statute_name("Finance Act, 2026")
    assert is_rejected_assembly_statute_name("Something Act, 2099")
    assert is_rejected_assembly_statute_name("%PDF Finance Act, 2026")


def test_percent_encoded_chrome_is_stripped_and_query_pipes_stay():
    encoded = "https://na.gov.pk/uploads/documents/a.pdf%7CShare%20Print"
    assert strip_listing_chrome_source_url(encoded) == "https://na.gov.pk/uploads/documents/a.pdf"
    query = "https://na.gov.pk/x.pdf?a=1%7cb"
    assert strip_listing_chrome_source_url(query) == query


async def test_promoted_statute_source_url_drops_listing_chrome(db):
    source = (
        await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NationalAssembly"))
    ).scalars().first()
    assert source is not None
    section = (
        "1. Short title. This Act may be called the Chrome Strip Act, 2026 "
        "and it extends to the whole of Pakistan. It shall come into force at once."
    )
    prov = await record_provenance(
        db,
        source=source,
        url=PDF,
        content=section.encode("utf-8"),
        content_kind="text",
    )
    staged = await stage_statute(
        db,
        source=source,
        prov=prov,
        raw_html=None,
        raw_text=section,
        url=DIRTY_URL,
        identity_key=DIRTY_URL,
        kind="statute",
    )
    # Staging already stores a stripped URL. Put a chrome label back so this
    # asserts the promotion write. ``Filter by`` is listing chrome, and it is
    # not the ``Share Print … Filter by`` pair that validation quarantines first.
    staged.source_url = PDF + "|Filter by"
    staged.status = "extracted"
    staged.reconciled_json = {
        "statute_name": "Chrome Strip Act, 2026",
        "jurisdiction": "Federal",
        "year_enacted": 2026,
        "statute_type": "act",
        "sections": [
            {
                "section_number": "1",
                "section_title": "Short title",
                "section_text": section,
            }
        ],
    }
    assert await promote_statute_staging(db, staged) == "promoted"
    stored = (await db.execute(select(Statute).where(Statute.name == "Chrome Strip Act, 2026"))).scalars().first()
    assert stored is not None
    assert stored.source_url == PDF


N1_TITLES = (
    "The Chairman and the Speaker (Salaries, Allowances and Privileges) Act, 1975",
    "The Deputy Chairman and the Deputy Speaker (Salaries, Allowances and Privileges) Act, 1975",
    "Members of Senate and the National Assembly (Salaries) Act, 2024",
)

N2_TITLES = (
    "Pakistan Penal Code (Amendment) Act, 2024",
    "Code of Criminal Procedure (Amendment) Act, 2024",
    "Code of Civil Procedure (Amendment) Act, 2024",
    "Cantonments Order (Amendment) Act, 2024",
    "Qanun-e-Shahadat Order (Amendment) Act, 2024",
    "Income Tax Ordinance (Amendment) Act, 2024",
    "Pakistan Code of Ethics Act, 2024",
    "Order of Precedence Act, 2024",
    "Code of Conduct for Legislators Act, 2024",
    "Criminal Laws (Amendment) Act, 2024",
)

N2_PRINCIPAL = {
    "Pakistan Penal Code (Amendment) Act, 2024": "further to amend the Pakistan Penal Code, 1860",
    "Code of Criminal Procedure (Amendment) Act, 2024": "further to amend the Code of Criminal Procedure, 1898",
    "Code of Civil Procedure (Amendment) Act, 2024": "further to amend the Code of Civil Procedure, 1908",
    "Cantonments Order (Amendment) Act, 2024": "further to amend the Cantonments Order, 2002",
    "Qanun-e-Shahadat Order (Amendment) Act, 2024": "further to amend the Qanun-e-Shahadat Order, 1984",
    "Income Tax Ordinance (Amendment) Act, 2024": "further to amend the Income Tax Ordinance, 2001",
}

N3_TITLES = (
    "e-Commerce Act, 2024",
    "PTCL (Re-organization) Act, 2024",
    "5G Spectrum Act, 2024",
)

N4_TITLE = "Prime Minister (Salary, Allowances and Privileges) (Amendment) Act, 2024"


def _called_act(title: str) -> str:
    year = int(title[-4:])
    principal = N2_PRINCIPAL.get(title, "")
    heading = f"AN ACT\n{principal}\n" if principal else ""
    return (
        f"ACT NO. I OF {year}\n"
        f"{heading}"
        f"1. Short title.—(1) This Act shall be called the {title}.\n"
        "(2) It shall come into force at once.\n"
        "2. Definitions. In this Act a word has its ordinary meaning.\n"
    )


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome"])
@pytest.mark.parametrize("title", N1_TITLES)
def test_and_the_federal_acts_are_kept(source_name, listing, title):
    out = _extract(
        _called_act(title),
        source_name=source_name,
        act_title=title if listing == "good" else CHROME,
        url=DIRTY_URL,
    )
    assert out["statute_name"] == title
    assert "and the" in out["statute_name"]
    assert out["year_enacted"] == int(title[-4:])
    assert not (out.get("field_evidence") or {}).get("name_unknown")


def test_and_the_is_rejected_only_with_a_clause_marker():
    assert is_rejected_assembly_statute_name(
        "shall be called the Chairman and the Speaker Act, 1975"
    )
    assert is_rejected_assembly_statute_name(
        "Amendment of section 2, Act XXIX of 1976.- In the Seed Act"
    )
    for title in N1_TITLES:
        assert not is_rejected_assembly_statute_name(title)


@pytest.mark.parametrize("title", N2_TITLES)
def test_short_title_keeps_the_last_instrument_word(title):
    decision = resolve_assembly_statute_title(_called_act(title), listing_title=CHROME)
    assert decision.name == title
    assert decision.year == 2024
    assert decision.name != "name_unknown"


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome"])
@pytest.mark.parametrize("title", N2_TITLES)
def test_instrument_word_titles_round_trip(source_name, listing, title):
    out = _extract(
        _called_act(title),
        source_name=source_name,
        act_title=title if listing == "good" else CHROME,
        url=DIRTY_URL,
    )
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert out["statute_name"] != "Pakistan Penal Code, 1860"
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("title", N3_TITLES)
def test_acronym_and_digit_titles_are_accepted(source_name, title):
    out = _extract(
        _called_act(title),
        source_name=source_name,
        act_title=CHROME,
        url=DIRTY_URL,
    )
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert not is_rejected_assembly_statute_name(title)
    kept = _extract(
        _called_act(title),
        source_name=source_name,
        act_title=title,
        url=DIRTY_URL,
    )
    assert kept["statute_name"] == title
    assert not (kept.get("field_evidence") or {}).get("name_unknown")
    decision = resolve_assembly_statute_title(_called_act(title), listing_title=title)
    assert decision.name == title


N6_FINANCE = "Finance Act, 2024"
N6_ELECTIONS = "Elections (Amendment) Act, 2024"
N6_PPC = "Pakistan Penal Code (Amendment) Act, 2024"

# Single-sentence section 1. A bare ``2.`` is not ``(2)``, so the reader must
# stop at the sentence instead of rendering through section 2.
N6_BODIES = (
    (
        "finance-section-2",
        N6_FINANCE,
        "1. Short title.- This Act may be called the Finance Act, 2024.\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
    (
        "finance-subsection-control",
        N6_FINANCE,
        "1. Short title.- This Act may be called the Finance Act, 2024.\n"
        "(2) It shall come into force at once.\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
    (
        "elections-act",
        N6_ELECTIONS,
        "1. Short title.- This Act may be called the Elections (Amendment) Act, 2024.\n"
        "2. Amendment of the Elections Act, 2017.\n",
    ),
    (
        "elections-section",
        N6_ELECTIONS,
        "1. Short title.- This Act may be called the Elections (Amendment) Act, 2024.\n"
        "2. Amendment of section 2, Act XXXIII of 2017.- In the Elections Act, 2017,\n"
        "for the words, the following shall be substituted.\n",
    ),
    (
        "ppc-and-shall",
        N6_PPC,
        "1. Short title.- This Act may be called the Pakistan Penal Code (Amendment) Act, 2024, "
        "and shall come into force at once.\n"
        "2. Amendment of the Pakistan Penal Code, 1860.- In the said Code, after section 1.\n",
    ),
    (
        "finance-and-it-shall",
        N6_FINANCE,
        "1. Short title.- This Act may be called the Finance Act 2024 and it shall come into force at once.\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
)


def _with_listing(text: str, *, source_name: str, listing: str, title: str):
    meta = {"source_name": source_name, "url": DIRTY_URL}
    if listing == "good":
        meta["act_title"] = title
    elif listing == "chrome":
        meta["act_title"] = CHROME
    return _extract(text, **meta)


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("_case,title,body", N6_BODIES, ids=[item[0] for item in N6_BODIES])
def test_single_sentence_section_stops_before_section_two(source_name, listing, _case, title, body):
    out = _with_listing(body, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert "2." not in out["statute_name"]
    assert "shall" not in out["statute_name"].lower()
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_wrapped_penal_code_title_keeps_the_later_act(source_name):
    text = (
        "1. Short title.—(1) This Act may be called the Pakistan Penal Code\n"
        "(Amendment) Act, 2024.\n"
        "(2) It shall come into force at once.\n"
    )
    out = _extract(text, source_name=source_name, act_title=CHROME, url=DIRTY_URL)
    assert out["statute_name"] == N6_PPC
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


def test_supersede_refuses_a_section_run_on():
    junk = "Finance Act 2024. 2. Amendment of the Income Tax Ordinance, 2001"
    assert _short_title_supersedes("Pakistan Penal Code, 1860", "Pakistan Penal Code (Amendment) Act, 2024")
    assert not _short_title_supersedes(N6_FINANCE, junk)
    assert not _short_title_supersedes(N6_FINANCE, "Finance Act, 2024 and it shall come into force at once")
    assert not _short_title_supersedes(
        N6_ELECTIONS,
        "Elections (Amendment) Act, 2024. 2. Amendment of section 2.- In the Elections Act, 2017",
    )
    assert not _short_title_supersedes(N6_PPC, "Pakistan Penal Code (Amendment) Act, 2024 and 1860")
    # Two printed years, and the second is not the title year. Dropping the
    # second-year guard lets this replace the 1952 Act.
    assert not _short_title_supersedes(
        "Pakistan Army Act, 1952",
        "Pakistan Army Act 1952 (XXXIX of 1952) as amended",
    )


ACT_NO_HEADER = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n"
R4_BODIES = (
    (
        "shall-finance",
        N6_FINANCE,
        "1. Short title.- This Act shall be called the Finance Act, 2024.\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
    (
        "shall-elections",
        N6_ELECTIONS,
        "1. Short title.- This Act shall be called the Elections (Amendment) Act, 2024.\n"
        "2. Amendment of the Elections Act, 2017.\n",
    ),
    (
        "shall-ppc",
        N6_PPC,
        "1. Short title.- This Act shall be called the Pakistan Penal Code (Amendment) Act, 2024, "
        "and shall come into force at once.\n"
        "2. Amendment of the Pakistan Penal Code, 1860.\n",
    ),
    (
        "no-period",
        N6_FINANCE,
        "1. Short title.- This Act may be called the Finance Act, 2024\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
    (
        "glued-subsection",
        N6_ELECTIONS,
        "1. Short title.- This Act may be called the Elections (Amendment) Act, 2024.(2) "
        "It shall come into force at once.\n"
        "2. Amendment of the Elections Act, 2017.\n",
    ),
    (
        "which-shall",
        N6_FINANCE,
        "1. Short title.- This Act may be called the Finance Act, 2024, which shall come into force at once.\n"
        "2. Amendment of the Income Tax Ordinance, 2001.\n",
    ),
    (
        "ocr-penal-period",
        N6_PPC,
        "1. Short title.—(1) This Act may be called the Pakistan Penal Code.\n"
        "(Amendment) Act, 2024.\n"
        "(2) It shall come into force at once.\n",
    ),
    (
        "ocr-order-period",
        "Cantonments Order (Amendment) Act, 2024",
        "1. Short title.—(1) This Act may be called the Cantonments Order.\n"
        "(Amendment) Act, 2024.\n"
        "(2) It shall come into force at once.\n",
    ),
)


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("_case,title,body", R4_BODIES, ids=[item[0] for item in R4_BODIES])
def test_remaining_run_ons_store_the_exact_title(source_name, listing, layout, _case, title, body):
    text = body if layout == "bare" else ACT_NO_HEADER + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert "2." not in out["statute_name"]
    assert "shall" not in out["statute_name"].lower()
    assert "(2)" not in out["statute_name"]
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    if _case == "glued-subsection" and listing == "good":
        # The glued run-on still must not replace the listing if the reader regresses.
        # Dropping ``shall`` from the run-on guard makes this True.
        assert not _short_title_supersedes(
            title,
            "Elections (Amendment) Act 2024.(2) It shall come into force at once",
        )


def test_lowercase_sweep_fragment_is_not_a_title():
    fragment = "c Karachi Port TnEr Act, 1886"
    assert is_rejected_assembly_statute_name(fragment)
    decision = resolve_assembly_statute_title(
        "1. Short title.- This Act may be called the c Karachi Port TnEr Act, 1886.\n"
        "(2) It extends to the Port.\n",
        listing_title=fragment,
    )
    assert decision.name == "name_unknown"
    assert decision.name != fragment


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["chrome", "none"])
@pytest.mark.parametrize(
    "title,principal",
    [
        ("Pakistan Penal Code (Amendment) Act, 2024", "Pakistan Penal Code, 1860"),
        ("Code of Criminal Procedure (Amendment) Act, 2024", "Code of Criminal Procedure, 1898"),
    ],
)
def test_short_title_supersedes_an_upstream_principal(source_name, listing, title, principal):
    text = (
        f"1. Short title.—(1) This Act may be called the {title}.\n"
        "(2) It shall come into force at once.\n"
    )
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["statute_name"] != principal
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_may_be_called_keeps_a_printed_comma(source_name):
    text = (
        "ACT NO. IV OF 2024\n"
        "1. Short title.- This Act may be called the "
        "Prime Minister (Salary, Allowances and Privileges) (Amendment) Act, 2024.\n"
        "(2) It shall come into force at once.\n"
    )
    decision = resolve_assembly_statute_title(text, listing_title=CHROME)
    assert decision.name == N4_TITLE
    assert "(Salary, Allowances and Privileges)" in decision.name
    out = _extract(text, source_name=source_name, act_title=CHROME, url=DIRTY_URL)
    assert out["statute_name"] == N4_TITLE
    assert "(Salary, Allowances and Privileges)" in out["statute_name"]
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_caps_acronym_reaches_the_title_finder(source_name):
    text = (
        "ACT NO. IV OF 2024\n"
        "1. Short title.- This Act may be called the PTCL (Re-organization) Act, 2024.\n"
        "(2) It shall come into force at once.\n"
    )
    decision = resolve_assembly_statute_title(text, listing_title=CHROME)
    assert decision.name == "PTCL (Re-organization) Act, 2024"
    assert decision.source == "short_title"
    out = _extract(text, source_name=source_name, act_title=CHROME, url=DIRTY_URL)
    assert out["statute_name"] == "PTCL (Re-organization) Act, 2024"
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


def test_numbered_act_is_not_a_section_run_on():
    assert not is_rejected_assembly_statute_name("Finance (No. 2) Act, 2024")


def test_leading_instrument_and_year_is_not_a_title():
    fragment = "Code, 1860, and the Qanun-e-Shahadat Order"
    assert is_rejected_assembly_statute_name(fragment)
    for name in (
        "Pakistan Penal Code, 1860",
        "Code of Criminal Procedure, 1898",
        "Finance Act, 2024",
        "Qanun-e-Shahadat Order, 1984",
        "Trust Act, 1882",
    ):
        assert not is_rejected_assembly_statute_name(name)


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_printed_commas_inside_a_title_stay(source_name):
    out = _extract(
        _called_act(N4_TITLE),
        source_name=source_name,
        act_title=CHROME,
        url=DIRTY_URL,
    )
    assert out["statute_name"] == N4_TITLE
    assert "(Salary, Allowances and Privileges)" in out["statute_name"]


# A year glued to the next words, when that glue is not a 1–2 digit ``(N)``.
# The body ends at the commencement sentence, or a blank line then section 2.
YEAR_GLUED_TAILS = (
    ".It extends to the whole of Pakistan.",
    ".(Z) It extends to the whole of Pakistan.",
    ".(l) It extends to the whole of Pakistan.",
    ".(2 ) It extends to the whole of Pakistan.",
    ".(ii) It extends to the whole of Pakistan.",
    ".(a) It extends to the whole of Pakistan.",
    ".(2a) It extends to the whole of Pakistan.",
    ".(1A) It extends to the whole of Pakistan.",
)


def _finance_glued(verb: str, tail: str, section: str) -> str:
    sentence = (
        f"1. Short title and commencement.— This Act {verb} be called the Finance Act, 2024{tail}"
    )
    if section == "blank":
        return sentence + "\n\n2. Amendment of the Income Tax Ordinance, 2001.\n"
    return sentence + "\n"


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("tail", YEAR_GLUED_TAILS)
def test_year_glued_tail_stops_at_the_year(source_name, listing, layout, verb, section, tail):
    body = _finance_glued(verb, tail, section)
    text = body if layout == "bare" else ACT_NO_HEADER + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=N6_FINANCE)
    assert out["statute_name"] == N6_FINANCE
    assert out["year_enacted"] == 2024
    assert "extends" not in out["statute_name"].lower()
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    if listing == "good":
        # The run-on must not replace the listing. Dropping the glued-tail
        # guard makes this True even when the reader still stops at the year.
        assert not _short_title_supersedes(N6_FINANCE, f"Finance Act 2024{tail}")


@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize(
    "tail",
    [
        ".(2) It extends to the whole of Pakistan.",
        ".(3) It extends to the whole of Pakistan.",
        ".(2) It shall come into force at once.",
    ],
)
def test_clean_subsection_after_the_year_stays_exact(source_name, listing, layout, verb, tail):
    body = _finance_glued(verb, tail, "end")
    text = body if layout == "bare" else ACT_NO_HEADER + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=N6_FINANCE)
    assert out["statute_name"] == N6_FINANCE
    assert out["year_enacted"] == 2024
    assert "extends" not in out["statute_name"].lower()
    assert "shall" not in out["statute_name"].lower()
    assert not (out.get("field_evidence") or {}).get("name_unknown")


# A year, then a comma, space, or dash, then more words, with no full stop.
YEAR_THEN_WORDS_TITLES = (
    ("Finance Act, 2024", 2024),
    ("Pakistan Penal Code (Amendment) Act, 2024", 2024),
    ("Elections (Amendment) Act, 2025", 2025),
    ("Societies Registration Act, 1860", 1860),
    ("National Agri-trade and Food Safety Authority Act, 2026", 2026),
)
YEAR_THEN_WORDS_TAILS = (
    ", It extends to the whole of Pakistan.",
    " , It extends to the whole of Pakistan.",
    ", and it extends to the whole of Pakistan.",
    ", and comes into force at once.",
    ", lt extends to the whole of Pakistan.",
    " It extends to the whole of Pakistan.",
    "  It extends to the whole of Pakistan.",
    " - It extends to the whole of Pakistan.",
    " and extends to the whole of Pakistan.",
)


def _year_then_words_body(title: str, verb: str, tail: str, section: str) -> str:
    sentence = (
        f"1. Short title and commencement.— This Act {verb} be called the {title}{tail}"
    )
    if section == "blank":
        return sentence + "\n\n2. Amendment of the Income Tax Ordinance, 2001.\n"
    return sentence + "\n"


def _year_then_words_run_on(title: str, year: int, tail: str) -> str:
    core = title[: -len(str(year))].rstrip(" ,")
    return f"{core} {year}{tail}"


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("tail", YEAR_THEN_WORDS_TAILS)
@pytest.mark.parametrize("title,year", YEAR_THEN_WORDS_TITLES)
def test_words_after_the_year_stop_at_the_year(
    source_name, listing, layout, verb, section, tail, title, year
):
    body = _year_then_words_body(title, verb, tail, section)
    header = f"ACT NO. I OF {year}\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == year
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    run_on = _year_then_words_run_on(title, year, tail)
    assert is_rejected_assembly_statute_name(run_on)
    if listing == "good":
        # Dropping the words-after-year guard makes this True and the listing
        # would be replaced by the run-on.
        assert not _short_title_supersedes(title, run_on)


# A year, a space, then a bracket that is not part of the title, and no full stop.
PAREN_AFTER_YEAR_TITLES = YEAR_THEN_WORDS_TITLES
PAREN_AFTER_YEAR_TAILS = (
    " (Z) It extends to the whole of Pakistan.",
    " (z) It extends to the whole of Pakistan.",
    " (ii) It extends to the whole of Pakistan.",
    " (I) It extends to the whole of Pakistan.",
    " (l) It extends to the whole of Pakistan.",
    " (a) It extends to the whole of Pakistan.",
    " (b) It extends to the whole of Pakistan.",
    " (2A) It extends to the whole of Pakistan.",
    " ( 2) It extends to the whole of Pakistan.",
    " (It extends to the whole of Pakistan.)",
    " (hereinafter referred to as the said Act) It extends to the whole of Pakistan.",
    " (the Act) It extends to the whole of Pakistan.",
    " (Z)\nIt extends to the whole of Pakistan.",
)


def _paren_after_year_run_on(title: str, year: int, tail: str) -> str:
    core = title[: -len(str(year))].rstrip(" ,")
    return f"{core} {year}{tail}".replace("\n", " ")


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("tail", PAREN_AFTER_YEAR_TAILS)
@pytest.mark.parametrize("title,year", PAREN_AFTER_YEAR_TITLES)
def test_bracket_after_the_year_stops_at_the_year(
    source_name, listing, layout, verb, section, tail, title, year
):
    body = _year_then_words_body(title, verb, tail, section)
    header = f"ACT NO. I OF {year}\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == year
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    run_on = _paren_after_year_run_on(title, year, tail)
    assert is_rejected_assembly_statute_name(run_on)
    if listing == "good":
        # A blanket bracket exemption, or dropping the bracket guard, replaces
        # this listing with the run-on.
        assert not _short_title_supersedes(title, run_on)


INNER_YEAR_TITLES = (
    ("Pakistan Penal Code, 1860 and Code of Criminal Procedure, 1898 (Amendment) Act, 2024", 2024),
    ("Representation of the People Act, 1976 and Senate (Election) Act, 1975 (Amendment) Act, 1985", 1985),
    ("Federal Public Service Commission Ordinance, 1977 Validation Act, 1980", 1980),
    ("Income Tax Ordinance, 2001 Amendment Act, 2024", 2024),
    ("The Code of Criminal Procedure, 1898 Balochistan (Amendment) Act, 2024", 2024),
)
INNER_YEAR_TAILS = (
    ".",
    ". It extends to the whole of Pakistan.",
    ". It shall come into force at once.",
)
COMPANIES_AMENDMENT = "Companies Act, 2017 (Amendment) Act, 2024"


def _years_printed(title: str) -> set[int]:
    found = set()
    for piece in title.replace(",", " ").split():
        if piece.isdigit() and len(piece) == 4:
            found.add(int(piece))
    return found


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("tail", INNER_YEAR_TAILS)
@pytest.mark.parametrize("title,later", INNER_YEAR_TITLES)
def test_inner_year_title_is_kept(
    source_name, listing, layout, verb, section, tail, title, later
):
    body = _year_then_words_body(title, verb, tail, section)
    header = f"ACT NO. I OF {later}\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == later
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert not is_rejected_assembly_statute_name(title)
    if listing == "good":
        assert _short_title(body) == title


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
def test_companies_amendment_title_keeps_the_continuation(source_name, listing, layout):
    body = (
        "1. Short title and commencement.— This Act may be called the "
        f"{COMPANIES_AMENDMENT}.\n"
    )
    header = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=COMPANIES_AMENDMENT)
    assert out["statute_name"] == COMPANIES_AMENDMENT
    assert "(Amendment)" in out["statute_name"]
    assert not is_rejected_assembly_statute_name(COMPANIES_AMENDMENT)
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    # Dropping the bracket continuation stores only the 2017 Act.
    assert _short_title(body) == COMPANIES_AMENDMENT
    assert "(Amendment)" in (_short_title(body) or "")


def test_citation_and_numbered_act_stay_titles_and_act_no_run_on_does_not():
    assert not is_rejected_assembly_statute_name("Finance Act, 2024 (XVII of 2024)")
    assert not is_rejected_assembly_statute_name("Pakistan Army Act, 1952 (XXXIX of 1952)")
    assert not is_rejected_assembly_statute_name("Finance (No. 2) Act, 2024")
    assert is_rejected_assembly_statute_name("Finance Act, 2024, Act No. V of, 2024")
    assert not _short_title_supersedes(
        "Pakistan Army Act, 1952",
        "Pakistan Army Act 1952 (XXXIX of 1952) as amended",
    )


# A later instrument year is part of the title only when the words between are
# the name. A connector after the year is the next clause.
LATER_YEAR_TAILS = (
    " as amended by the Finance Act, 2023.",
    ", as amended by the Finance Act, 2023.",
    " and amends the Finance Act, 2023.",
    ", and amends the Finance Act, 2023.",
    " -- amends the Finance Act, 2023.",
    "\namends the Finance Act, 2023.",
    " - under the Finance Act, 2023.",
    " under the Finance Act, 2023.",
    " and Cantonments Order, 2023.",
    " and the Finance Act, 2023.",
    " in place of the Finance Act, 2023.",
    " of the Finance Act, 2023.",
    " read with the Income Tax Ordinance, 2001.",
    " repealing the Finance Act, 2023.",
    " replacing the Finance Act, 2023.",
    " made under the Finance Act, 2023.",
)


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("tail", LATER_YEAR_TAILS)
def test_connector_after_the_year_does_not_extend_the_title(
    source_name, listing, layout, verb, tail
):
    title = "Finance Act, 2024"
    body = _year_then_words_body(title, verb, tail, "end")
    header = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    if listing == "good":
        assert out["statute_name"] == title
        assert out["year_enacted"] == 2024
        assert not (out.get("field_evidence") or {}).get("name_unknown")
        # Continuing past the year, or failing to stop the called line, stores the tail.
        assert _short_title(body) == title
    else:
        assert out["statute_name"] in {title, "name_unknown"}
        assert "as amended" not in (out["statute_name"] or "").lower()
        assert "read with" not in (out["statute_name"] or "").lower()


@pytest.mark.no_db
def test_newline_before_a_later_act_does_not_extend_the_title():
    body = (
        "1. Short title.- This Act may be called the Finance Act, 2024 and Code\n"
        "(Amendment) Act, 2025.\n"
    )
    assert _short_title(body) == "Finance Act, 2024"


# Cited listings. The citation is not part of the called line, so a rejected
# listing is quarantined or replaced by the core name.
CITED_LISTINGS = (
    ("Pakistan Penal Code, 1860 (Act XLV of 1860)", "Pakistan Penal Code, 1860"),
    ("Code of Criminal Procedure, 1898 (Act V of 1898)", "Code of Criminal Procedure, 1898"),
    ("Finance Act, 2024 (Act XVII of 2024)", "Finance Act, 2024"),
    ("Income Tax Ordinance, 2001 (Ordinance No. XLIX of 2001)", "Income Tax Ordinance, 2001"),
    ("Income Tax Ordinance, 2001 (Ord. XLIX of 2001)", "Income Tax Ordinance, 2001"),
    ("Finance Act, 2024 (No. XVII of 2024)", "Finance Act, 2024"),
    ("Finance Act, 2024 [XVII of 2024]", "Finance Act, 2024"),
    ("Finance Act, 2024 (XVII of 2024).", "Finance Act, 2024"),
    ("Finance Act, 2024 (Act No XVII of 2024)", "Finance Act, 2024"),
    ("Pakistan Army Act, 1952 (Act XXXIX of 1952)", "Pakistan Army Act, 1952"),
    ("Finance Act, 2024 (P.O. No. 1 of 2024)", "Finance Act, 2024"),
    ("Finance Act, 2024 (No.XVII of 2024)", "Finance Act, 2024"),
)


def _cited_bodies(core: str) -> tuple[str, ...]:
    return (
        f"1. Short title.- This Act may be called the {core}.\n",
        f"1. Short title.- This Act shall be called the {core}.\n",
        "AN ACT to amend the law.\n",
        "This Act may be cited as the said Act.\n",
        "Be it enacted as follows.\n",
    )


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("body_index", [0, 1, 2, 3, 4])
@pytest.mark.parametrize("listing,core", CITED_LISTINGS)
def test_cited_listing_is_kept(source_name, layout, body_index, listing, core):
    assert not is_rejected_assembly_statute_name(listing)
    body = _cited_bodies(core)[body_index]
    title_year = next(iter(_years_printed(core)))
    header = f"ACT NO. I OF {title_year}\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    out = _with_listing(text, source_name=source_name, listing="good", title=listing)
    assert out["statute_name"] == listing
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.no_db
def test_real_cited_names_are_not_rejected():
    for name in (
        "Azad Jammu and Kashmir Legislative Assembly (Elections) Act, 1986 (ACT VI OF 1986)",
        "Balochistan Local Government Act, 2010 (Act V of 2010)",
        "Companies Act, 2017 (Act XXII Of 2017)",
        "The Bank of Azad Jammu and Kashmir Act, 2005 (ACT VI OF 2005)",
        "Finance Act, 2024 (President's Order No. 5 of 2024)",
        "Finance Act, 2024 (Regulation No. 3 of 2024)",
        "Income Tax Ordinance, 2001 (Ordinance No.XLIX of 2001)",
    ):
        assert not is_rejected_assembly_statute_name(name)


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
def test_ocr_year_does_not_overwrite_a_listing(source_name, listing):
    title = "Finance Act, 2024"
    body = "1. Short title.- This Act may be called the Finance Act, 2O24.\n"
    text = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n" + body
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    if listing == "good":
        assert out["statute_name"] == title
        assert out["year_enacted"] == 2024
        assert not (out.get("field_evidence") or {}).get("name_unknown")
    else:
        assert out["statute_name"] in {title, "name_unknown"}
        assert "2O24" not in (out["statute_name"] or "")
    assert not _short_title_supersedes(title, "Finance Act, 2O24, 2024")
    assert "2O24, 2024" not in (_short_title(body) or "")


@pytest.mark.no_db
def test_act_number_year_wins_when_the_title_names_an_older_act():
    title = "Income Tax Ordinance, 2001 Amendment Act, 2024"
    body = f"1. Short title.- This Act may be called the {title}.\n"
    text = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n" + body
    out = _with_listing(text, source_name="NationalAssembly", listing="chrome", title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024


@pytest.mark.no_db
def test_continuations_after_the_year_stay_in_the_short_title():
    second = (
        "1. Short title.- This Act may be called the Finance Act, 2024 "
        "(Second Amendment) Act, 2025.\n"
    )
    numbered = (
        "1. Short title.- This Act may be called the Finance Act, 2024 "
        "(No.2) Amendment Act, 2025.\n"
    )
    cited = "1. Short title.- This Act may be called the Pakistan Army Act, 1952 (XXXIX of 1952).\n"
    assert _short_title(second) == "Finance Act, 2024 (Second Amendment) Act, 2025"
    assert _short_title(numbered) == "Finance Act, 2024 (No.2) Amendment Act, 2025"
    assert _short_title(cited) == "Pakistan Army Act, 1952 (XXXIX of 1952)"


@pytest.mark.no_db
def test_glued_letter_and_nineteenth_century_tail_are_rejected():
    assert is_rejected_assembly_statute_name("Finance Act, 2024It extends to the whole of Pakistan")
    assert is_rejected_assembly_statute_name("Trusts Act, 1882It extends to the whole of Pakistan")
    assert not _short_title_supersedes(
        "Finance Act, 2024",
        "Finance Act 2024It extends to the whole of Pakistan",
    )


@pytest.mark.no_db
def test_bare_section_number_ends_the_short_title():
    text = "1. Short title.- This Act may be called the Finance Act 2. Amendment of section 4.\n"
    assert _short_title(text) == "Finance Act"


# Two principal Acts joined into one amending instrument.
# A newline may follow the first year, or fall in the middle of the second name.
COMBINED_AMENDMENTS = (
    (
        "Code of Criminal Procedure, 1898",
        " and ",
        "Pakistan Penal Code, 1860",
        "(Amendment) Act, 2016",
    ),
    (
        "Income Tax Ordinance, 2001",
        " and ",
        "Sales Tax Act, 1990",
        "(Amendment) Act, 2024",
    ),
    (
        "Anti-Terrorism Act, 1997",
        " and ",
        "Pakistan Penal Code, 1860",
        "(Amendment) Act, 2020",
    ),
    (
        "Control of Narcotic Substances Act, 1997",
        " and ",
        "Customs Act, 1969",
        "(Amendment) Act, 2022",
    ),
    (
        "Pakistan Penal Code, 1860",
        " and the ",
        "Code of Criminal Procedure, 1898",
        "(Amendment) Act, 2024",
    ),
    (
        "Pakistan Penal Code, 1860",
        ", and the ",
        "Qanun-e-Shahadat Order, 1984",
        "(Amendment) Act, 2024",
    ),
    (
        "Islamabad Capital Territory Trust Act, 2020",
        " and ",
        "Societies Registration Act, 1860",
        "(Amendment) Act, 2021",
    ),
)


def _combined_forms(left: str, join: str, right: str, suffix: str) -> tuple[str, str, str]:
    flat = f"{left}{join}{right} {suffix}"
    after_year = f"{left}\n{join.lstrip()}{right} {suffix}"
    instrument = re.search(r"\b(?:Act|Code|Ordinance|Order|Regulation),", right)
    if instrument:
        mid = f"{left}{join}{right[: instrument.start()]}\n{right[instrument.start() :]} {suffix}"
    else:
        mid = flat
    return flat, after_year, mid


_COMBINED_CASES = []
for _left, _join, _right, _suffix in COMBINED_AMENDMENTS:
    _flat, _after, _mid = _combined_forms(_left, _join, _right, _suffix)
    _COMBINED_CASES.append(("flat", _flat))
    _COMBINED_CASES.append(("after-year", _after))
    _COMBINED_CASES.append(("mid", _mid))


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("break_kind,rendered", _COMBINED_CASES)
def test_combined_amendment_title_is_kept(
    source_name, listing, layout, verb, section, break_kind, rendered
):
    title = re.sub(r"\s+", " ", rendered).strip()
    body = _year_then_words_body(rendered, verb, ".", section)
    later = int(re.findall(r"\b(?:1[89]\d{2}|20\d{2})\b", title)[-1])
    header = f"ACT NO. I OF {later}\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    assert not is_rejected_assembly_statute_name(title)
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == later
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert _short_title(body) == title


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
def test_shall_citation_on_chrome_stores_the_core_name(source_name, listing):
    cited = "Finance Act, 2024 (Act V of 2024)"
    body = f"1. Short title.- This Act shall be called the {cited}.\n"
    out = _with_listing(body, source_name=source_name, listing=listing, title=cited)
    if listing == "good":
        assert out["statute_name"] == cited
    else:
        assert out["statute_name"] == "Finance Act, 2024"
        assert "(Act" not in out["statute_name"]


@pytest.mark.no_db
@pytest.mark.parametrize(
    "name",
    [
        "Companies Act, 2017 (Act XXII Of",
        "Finance Act, 2024 (Passed under Cabi",
        "Elections Act, 2017 (Act",
        "Finance Act, 2024 (Cabinet Division",
        "Finance Act, 2024 (ord. XII",
    ],
)
def test_unclosed_citation_fragment_is_not_rejected(name):
    assert not is_rejected_assembly_statute_name(name)


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["chrome", "none"])
def test_year_period_on_the_same_line_stops_before_section_two(source_name, listing):
    body = (
        "1. Short title.- This Act may be called the Finance Act, 2024. "
        "2. Amendment of the Income Tax Ordinance, 2001.\n"
    )
    out = _with_listing(body, source_name=source_name, listing=listing, title="Finance Act, 2024")
    assert out["statute_name"] == "Finance Act, 2024"
    assert "2." not in out["statute_name"]
    assert _short_title(body) == "Finance Act, 2024"
    # A period on the year is what stops the title. The next instrument is not ``N.``.
    later = (
        "1. Short title.- This Act may be called the Finance Act, 2024. "
        "Income Tax Ordinance, 2001.\n"
    )
    later_out = _with_listing(
        later, source_name=source_name, listing=listing, title="Finance Act, 2024"
    )
    assert later_out["statute_name"] == "Finance Act, 2024"
    assert "Income Tax" not in later_out["statute_name"]
    assert _short_title(later) == "Finance Act, 2024"


@pytest.mark.no_db
def test_wrapped_amendment_after_the_year_stays_in_the_short_title():
    body = (
        "1. Short title.- This Act may be called the Income Tax Ordinance, 2001\n"
        "(Amendment) Act, 2024.\n"
    )
    assert _short_title(body) == "Income Tax Ordinance, 2001 (Amendment) Act, 2024"


@pytest.mark.no_db
def test_ocr_year_token_is_rejected():
    assert is_rejected_assembly_statute_name("Income Tax Ordinance, 2O24")
    assert is_rejected_assembly_statute_name("Finance Act, 2O24")


@pytest.mark.no_db
def test_truncate_skip_is_anchored_to_the_title_file():
    from tests.conftest import _skips_title_truncate

    class Other:
        path = Path("/tmp/elsewhere/test_na_statute_title.py")

        def get_closest_marker(self, _name):
            return None

    class Here:
        path = Path(__file__).resolve()

        def get_closest_marker(self, _name):
            return None

    assert not _skips_title_truncate(Other(), [])
    assert _skips_title_truncate(Here(), [])
    assert not _skips_title_truncate(Here(), ["db"])
    assert not _skips_title_truncate(Here(), ["source"])
    assert not _skips_title_truncate(Here(), ["login_source"])


@pytest.mark.no_db
async def test_skipped_truncate_guard_rejects_a_connection():
    from scraper.database import engine

    with pytest.raises(AssertionError, match="database connection"):
        async with engine.connect():
            pass


@pytest.mark.no_db
def test_no_db_with_a_database_fixture_is_a_collection_error(tmp_path):
    probe = REPO / "tests" / "_r9_no_db_probe.py"
    probe.write_text(
        "import pytest\n\n@pytest.mark.no_db\ndef test_bad(db):\n    pass\n",
        encoding="utf-8",
    )
    # An xdist worker has already rewritten DATABASE_URL to ``…_gwN``. A nested
    # pytest that still looks like that worker refuses the name before collection.
    env = os.environ.copy()
    for key in ("PYTEST_XDIST_WORKER", "PYTEST_XDIST_WORKER_COUNT", "PYTEST_XDIST_TESTRUNUID"):
        env.pop(key, None)
    base = env.get("_CORPUS_TEST_DATABASE_BASE")
    current = env.get("DATABASE_URL")
    if base and current:
        parsed = urlparse(current)
        env["DATABASE_URL"] = urlunparse(parsed._replace(path="/" + quote(base, safe="")))
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(probe),
                "--collect-only",
                "-q",
                "-p",
                "no:cacheprovider",
                "-p",
                "no:xdist",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    finally:
        probe.unlink(missing_ok=True)
    blob = completed.stdout + completed.stderr
    assert completed.returncode != 0
    assert "no_db" in blob
    assert "database fixture" in blob


# A year-bearing list joined by and / & / and the / , and the, then any capitalised
# close immediately before the final instrument. Clause connectors stay outside it.
CLOSING_SUFFIXES = (
    "(Third Amendment) Act, 2024",
    "(Fourth Amendment) Act, 2024",
    "(Fifth Amendment) Act, 2024",
    "(Second Amendment) Act, 2024",
    "(Validation) Act, 2024",
    "(Amendment and Validation) Act, 2024",
    "(Validation and Amendment) Act, 2024",
    "(Repeal) Act, 2024",
    "Repeal Act, 2024",
    "(Extension) Act, 2024",
    "(Removal of Difficulties) Act, 2024",
    "(Amendments) Act, 2024",
    "Amendments Act, 2024",
    "Validation Act, 2024",
    "Amendment Act, 2024",
)
CLOSING_JOINS = (" and ", " & ", " and the ", ", and the ")
_CLOSING_LEFT = "Income Tax Ordinance, 2001"
_CLOSING_RIGHT = "Sales Tax Act, 1990"


def _closing_title(join: str, suffix: str, left: str = _CLOSING_LEFT, right: str = _CLOSING_RIGHT) -> str:
    return f"{left}{join}{right} {suffix}"


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("section", ["end", "blank"])
@pytest.mark.parametrize("join", CLOSING_JOINS)
@pytest.mark.parametrize("suffix", CLOSING_SUFFIXES)
def test_any_capitalised_close_keeps_the_combined_title(
    source_name, listing, layout, verb, section, join, suffix
):
    title = _closing_title(join, suffix)
    body = _year_then_words_body(title, verb, ".", section)
    header = "ACT NO. I OF 2024\nAN ACT\nto amend the law\n"
    text = body if layout == "bare" else header + body
    assert not is_rejected_assembly_statute_name(title)
    out = _with_listing(text, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert _short_title(body) == title


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("suffix", CLOSING_SUFFIXES)
def test_newline_after_the_first_year_keeps_a_capitalised_close(source_name, listing, suffix):
    rendered = f"{_CLOSING_LEFT}\nand {_CLOSING_RIGHT} {suffix}"
    title = re.sub(r"\s+", " ", rendered).strip()
    body = _year_then_words_body(rendered, "may", ".", "end")
    out = _with_listing(body, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert _short_title(body) == title


CLAUSE_TAILS = (
    " as amended by the Finance Act, 2023",
    " and amends the Finance Act, 2023",
    " under the Finance Act, 2023",
    " of the Income Tax Ordinance, 2001",
    " read with the Finance Act, 2023",
    " in place of the Finance Act, 2023",
    " repealing the Finance Act, 2023",
    " made under the Finance Act, 2023",
    " and the Code of Criminal Procedure, 1898",
)


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("tail", CLAUSE_TAILS)
def test_clause_connector_still_stops_at_the_earlier_year(source_name, listing, verb, tail):
    core = "Finance Act, 2024"
    long = f"{core}{tail}"
    body = _year_then_words_body(core, verb, tail + ".", "end")
    # The long form is the listing. Keeping it means the connector check was dropped.
    assert is_rejected_assembly_statute_name(long)
    out = _with_listing(body, source_name=source_name, listing=listing, title=long)
    stored = out["statute_name"] or ""
    assert stored != long
    assert "Finance Act, 2023" not in stored
    if listing == "good":
        assert stored == core
    assert _short_title(body) == core


@pytest.mark.no_db
def test_newline_after_the_year_does_not_take_the_next_clause():
    body = (
        "1. Short title.- This Act may be called the Finance Act, 2024\n"
        "amends the Finance Act, 2023.\n"
    )
    assert _short_title(body) == "Finance Act, 2024"
    out = _with_listing(body, source_name="NationalAssembly", listing="chrome", title="Finance Act, 2024")
    assert out["statute_name"] == "Finance Act, 2024"
    assert "2023" not in out["statute_name"]


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
def test_bare_section_number_is_not_stored(source_name, listing):
    body = "1. Short title.- This Act may be called the Finance Act 2. Amendment of section 4.\n"
    out = _with_listing(body, source_name=source_name, listing=listing, title="Finance Act")
    stored = out["statute_name"] or ""
    assert "Amendment of section" not in stored
    assert "section 4" not in stored
    assert stored in {"Finance Act", "name_unknown"}
    assert _short_title(body) == "Finance Act"


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize(
    "called,core",
    [
        ("Finance Act, 2024 (Act No", "Finance Act, 2024"),
        ("Finance Act, 2024 (Act V of 2", "Finance Act, 2024"),
        ("Finance Act, 2024 (Passed under Cabi", "Finance Act, 2024"),
        ("Companies Act, 2017 (Amendment) Act, 2024 (Act No. V of 2024)", "Companies Act, 2017 (Amendment) Act, 2024"),
        ("Companies Act, 2017 (Amendment) Act, 2024 (Act V of 2024)", "Companies Act, 2017 (Amendment) Act, 2024"),
        ("Companies Act, 2017 (Amendment) Act, 2024 (XLV of 2024)", "Companies Act, 2017 (Amendment) Act, 2024"),
        ("Finance Act, 2024 (Act No. V of 2024)", "Finance Act, 2024"),
        ("Finance Act, 2024 (XLV of 2024)", "Finance Act, 2024"),
    ],
)
def test_citation_tail_on_chrome_stores_the_core(source_name, listing, called, core):
    body = f"1. Short title.- This Act shall be called the {called}.\n"
    out = _with_listing(body, source_name=source_name, listing=listing, title=called)
    if listing == "good":
        assert out["statute_name"] == called
    else:
        assert out["statute_name"] == core
        assert "(Act" not in out["statute_name"]
        assert "(XLV" not in out["statute_name"]
    assert not is_rejected_assembly_statute_name(called)


# The good listing is the second Act. A junk close must not rename it.
# and / & / and the / , and the still combine when the close amends.
_SECOND_ACT = "Federal Excise Act, 2005"
_FIRST_ACT = "Finance Act, 2024"
_SIDE_BY_SIDE_JOINS = (", ", " ", "\n", " and ", " and the ", " & ")
_LIST_JOINS = (" and ", " & ", " and the ", ", and the ")
_GAP_JOINS = (", ", " ", "\n")
_JUNK_CLOSES = (
    " (Z) Act, 2025",
    " (a) Act, 2025",
    " (iv) Act, 2025",
    " (2) Act, 2025",
    " (Omitted) Act, 2025",
    " (Repealed) Act, 2025",
    " (Substituted) Act, 2025",
    " (Schedule) Act, 2025",
    " (First Schedule) Act, 2025",
    " (Second Schedule) Act, 2025",
    " (It extends to the whole of Pakistan) Act, 2025",
    " (hereinafter referred to as the said Act) Act, 2025",
    " (See section 2) Act, 2025",
    " (Amended by the Finance Act, 2023) Act, 2025",
    " (as amended) Act, 2025",
    " (Islamabad) Act, 2025",
    " (Federal Excise) Act, 2025",
    " Finance Act, 2025",
    " Repealed Act, 2025",
)
# These were accepted once a junk close sat on a real combined amendment.
_REJECTED_ON_COMBINED_BASE = (
    " (Omitted) Act, 2025",
    " (Z) Act, 2025",
    " (a) Act, 2025",
    " (iv) Act, 2025",
    " (2) Act, 2025",
    " (Repealed) Act, 2025",
    " (Substituted) Act, 2025",
    " (Schedule) Act, 2025",
    " (Second Schedule) Act, 2025",
    " (First Schedule) Act, 2025",
    " (Islamabad) Act, 2025",
    " (Federal Excise) Act, 2025",
    " (It extends to the whole of Pakistan) Act, 2025",
    " Finance Act, 2025",
    " Repealed Act, 2025",
)
_CITATION_WRAPPERS = (
    " (No. 2 Amendment) Act, 2025",
    " (Act Amendment) Act, 2025",
    " (V of Amendment) Act, 2025",
    " (2 Amendment) Act, 2025",
)
_COMBINED_BASE = "Income Tax Ordinance, 2001 and Sales Tax Act, 1990 (Amendment) Act, 2024"


def _side_by_side_body(title_line: str) -> str:
    return (
        "ACT NO. V OF 2024\n"
        "AN ACT\n"
        "further to amend certain laws\n"
        f"1. Short title and commencement.-(1) This Act may be called {title_line}\n"
        "(2) It shall come into force at once.\n"
        "\n"
        "2. Amendment.\n"
    )


def _side_by_side_line(join: str, close: str) -> str:
    return f"the {_FIRST_ACT}{join}{_SECOND_ACT}{close}."


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("join", _SIDE_BY_SIDE_JOINS)
@pytest.mark.parametrize("close", _JUNK_CLOSES)
def test_junk_close_does_not_rename_the_second_act(source_name, listing, join, close):
    line = _side_by_side_line(join, close)
    junk = re.sub(r"\s+", " ", line.removeprefix("the ")).rstrip(".")
    assert is_rejected_assembly_statute_name(junk)
    out = _with_listing(
        _side_by_side_body(line),
        source_name=source_name,
        listing=listing,
        title=_SECOND_ACT,
    )
    stored = out["statute_name"] or ""
    assert stored != junk
    assert "Act, 2025" not in stored
    if listing == "good":
        assert stored == _SECOND_ACT


@pytest.mark.no_db
@pytest.mark.parametrize("join", _LIST_JOINS)
def test_amending_close_on_a_list_joiner_still_replaces_the_second_act(join):
    second = "Sales Tax Act, 1990"
    full = f"Income Tax Ordinance, 2001{join}{second} (Amendment) Act, 2024"
    out = _with_listing(
        _side_by_side_body("the " + full + "."),
        source_name="NationalAssembly",
        listing="good",
        title=second,
    )
    assert out["statute_name"] == re.sub(r"\s+", " ", full).strip()
    assert out["year_enacted"] == 2024


@pytest.mark.no_db
@pytest.mark.parametrize("join", _GAP_JOINS)
def test_amending_close_without_a_list_joiner_keeps_the_second_act(join):
    full = f"{_FIRST_ACT}{join}{_SECOND_ACT} (Amendment) Act, 2025"
    out = _with_listing(
        _side_by_side_body("the " + full + "."),
        source_name="NationalAssembly",
        listing="good",
        title=_SECOND_ACT,
    )
    assert out["statute_name"] == _SECOND_ACT
    assert not _short_title_supersedes(_SECOND_ACT, re.sub(r"\s+", " ", full).strip())


@pytest.mark.no_db
def test_combined_base_with_a_junk_tail_is_rejected():
    assert not is_rejected_assembly_statute_name(_COMBINED_BASE)
    for tail in _REJECTED_ON_COMBINED_BASE:
        assert is_rejected_assembly_statute_name(_COMBINED_BASE + tail)
    for tail in _CITATION_WRAPPERS:
        assert is_rejected_assembly_statute_name(_COMBINED_BASE + tail)


@pytest.mark.no_db
def test_newline_does_not_hand_the_second_act_to_the_amendment():
    rendered = f"{_FIRST_ACT}\n{_SECOND_ACT} (Amendment) Act, 2025"
    assert not _short_title_supersedes(_SECOND_ACT, rendered)
    out = _with_listing(
        _side_by_side_body("the " + rendered + "."),
        source_name="NationalAssembly",
        listing="good",
        title=_SECOND_ACT,
    )
    assert out["statute_name"] == _SECOND_ACT


@pytest.mark.no_db
def test_ampersand_without_an_amending_close_stops():
    bare = (
        "1. Short title.- This Act may be called the Finance Act, 2024 "
        "& Federal Excise Act, 2005.\n"
    )
    assert _short_title(bare) == "Finance Act, 2024"
    buried = (
        "1. Short title.- This Act may be called the Finance Act, 2024 "
        "& see Validation Act, 2024.\n"
    )
    assert _short_title(buried) == "Finance Act, 2024"


@pytest.mark.no_db
def test_section_number_stops_before_the_later_ordinance():
    text = (
        "1. Short title.- This Act may be called the Finance Act "
        "2. Amendment of the Income Tax Ordinance, 2001.\n"
    )
    assert _short_title(text) == "Finance Act"
    assert "Ordinance" not in (_short_title(text) or "")


@pytest.mark.no_db
def test_enacted_year_ignores_a_later_year_that_does_not_close():
    assert _enacted_year("Notes 1860 and 1898", 1860) == 1860
    assert _enacted_year("Finance Act, 2024 (Act V of 1860)", None) == 2024


@pytest.mark.no_db
def test_cited_upstream_requires_a_longer_capture():
    assert _cited_upstream_name("Finance Act, 2024", "Finance Act, 2024", None) is False
    assert (
        _cited_upstream_name("Finance Act, 2024 (Act V of 1860)", "Finance Act, 2024", None)
        is True
    )


@pytest.mark.no_db
async def test_skipped_truncate_guard_rejects_asyncpg():
    import asyncpg

    with pytest.raises(AssertionError, match="database connection"):
        await asyncpg.connect("postgresql://legal:legal@localhost:5432/legal_scraper_test")


# Closes the R12 whitelist missed. A good listing of each one is the title.
_WIDE_CLOSES = (
    "(Eleventh Amendment) Act, 2024",
    "(Twelfth Amendment) Act, 2024",
    "(Twenty-sixth Amendment) Act, 2024",
    "(2nd Amendment) Act, 2024",
    "(3rd Amendment) Act, 2024",
    "(26th Amendment) Act, 2024",
    "(Amending) Act, 2024",
    "Amending Act, 2024",
    "(Removal of Difficulty) Act, 2024",
    "(Extension of Validity) Act, 2024",
    "(Validation of Laws) Act, 2024",
    "(Amendment, Validation and Repeal) Act, 2024",
    "(Extension and Amendment) Act, 2024",
    "(Amendment) (No. 2) Act, 2024",
    "(Amendment) Act No. 2, 2024",
)
# Not combining closes. A good listing that already uses one is kept as written.
_UNLISTED_WELL_FORMED_CLOSES = (
    "(Special Provisions)",
    "(Continuance)",
    "(Revival)",
    "(Abolition)",
)
_AND_PAIR_JUNK = (
    " (Amendment and Z) Act, 2025",
    " (Validation and Omitted) Act, 2025",
    " (Repeal and Schedule) Act, 2025",
)


def _collapsed_called(rendered: str) -> str:
    return re.sub(r"\s+", " ", rendered).strip()


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("join", _SIDE_BY_SIDE_JOINS)
@pytest.mark.parametrize("suffix", _WIDE_CLOSES)
def test_wide_amending_close_keeps_the_two_act_title(source_name, listing, join, suffix):
    rendered = f"{_CLOSING_LEFT}{join}{_CLOSING_RIGHT} {suffix}"
    title = _collapsed_called(rendered)
    body = _year_then_words_body(rendered, "may", ".", "end")
    assert not is_rejected_assembly_statute_name(title)
    assert _combining_suffix_at_end(title)
    out = _with_listing(body, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert _short_title(body) == title


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
@pytest.mark.parametrize("suffix", _WIDE_CLOSES)
def test_wide_amending_close_keeps_the_single_act_title(source_name, listing, suffix):
    title = f"Companies Act, 2017 {suffix}"
    body = _year_then_words_body(title, "may", ".", "end")
    assert not is_rejected_assembly_statute_name(title)
    out = _with_listing(body, source_name=source_name, listing=listing, title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert _short_title(body) == title


@pytest.mark.no_db
@pytest.mark.parametrize("close", _UNLISTED_WELL_FORMED_CLOSES)
def test_unlisted_well_formed_close_keeps_a_good_listing(close):
    title = f"Companies Act, 2017 {close} Act, 2024"
    assert not _combining_suffix_at_end(title)
    assert _verbatim_good_listing(title, _year_then_words_body(title, "may", ".", "end"))
    body = _year_then_words_body(title, "may", ".", "end")
    out = _with_listing(body, source_name="NationalAssembly", listing="good", title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    # The reader does not treat the close as combining, so it does not rename
    # a different Act that merely shares the called line.
    other = f"Finance Act, 2024 and Federal Excise Act, 2005 {close} Act, 2025"
    assert not _short_title_supersedes("Federal Excise Act, 2005", _collapsed_called(other))


@pytest.mark.no_db
@pytest.mark.parametrize("tail", _AND_PAIR_JUNK)
def test_and_pair_is_junk_unless_both_words_amend(tail):
    combined = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT}{tail}"
    assert is_rejected_assembly_statute_name(_collapsed_called(combined))
    assert not _combining_suffix_at_end(_collapsed_called(combined))


@pytest.mark.no_db
def test_second_instrument_after_a_closed_amendment_is_rejected():
    closed = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} (Amendment) Act, 2024"
    assert not is_rejected_assembly_statute_name(closed)
    assert is_rejected_assembly_statute_name(f"{closed} Repeal Act, 2025")
    assert is_rejected_assembly_statute_name(f"{closed} Amendment Act, 2025")
    assert not _combining_suffix_at_end(f"{closed} Repeal Act, 2025")
    # The bare word is still the close when it is the only one.
    assert not is_rejected_assembly_statute_name(f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} Repeal Act, 2024")
    assert not is_rejected_assembly_statute_name(f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} Amendment Act, 2024")


@pytest.mark.no_db
def test_citation_wrapper_is_not_an_amending_close():
    # ``(2nd Amendment)`` is an ordinal. ``(2 Amendment)`` is a citation.
    ordinal = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} (2nd Amendment) Act, 2024"
    cited = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} (2 Amendment) Act, 2025"
    assert not is_rejected_assembly_statute_name(ordinal)
    assert _combining_suffix_at_end(ordinal)
    assert is_rejected_assembly_statute_name(cited)
    assert not _combining_suffix_at_end(cited)
    for tail in _CITATION_WRAPPERS:
        assert is_rejected_assembly_statute_name(_COMBINED_BASE + tail)


@pytest.mark.no_db
def test_newline_gap_does_not_reach_a_later_amending_act():
    body = (
        "1. Short title.- This Act may be called the Finance Act, 2024 and Code\n"
        "(Amendment) Act, 2025.\n"
    )
    assert _short_title(body) == "Finance Act, 2024"
    out = _with_listing(body, source_name="NationalAssembly", listing="chrome", title="Finance Act, 2024")
    assert out["statute_name"] == "Finance Act, 2024"
    assert "2025" not in (out["statute_name"] or "")


@pytest.mark.no_db
def test_combining_suffix_has_to_be_at_the_end():
    closed = "Finance Act, 2024 (Amendment) Act, 2025"
    assert _combining_suffix_at_end(closed)
    assert not _combining_suffix_at_end(f"{closed} and more")
    assert not _combining_suffix_at_end(f"{closed} Repeal Act, 2026")


@pytest.mark.no_db
def test_listing_keep_guards():
    text = _side_by_side_body("the Finance Act, 2024, Federal Excise Act, 2005 (Z) Act, 2025.")
    assert _listing_is_another_principal("Federal Excise Act, 2005", "Finance Act, 2024", text)
    # Same instrument, including a leading "the".
    assert not _listing_is_another_principal("the Finance Act, 2024", "Finance Act, 2024", text)
    # A rejected resolution is not a reason to adopt the listing.
    assert not _listing_is_another_principal("Federal Excise Act, 2005", "name_unknown", text)
    assert not _listing_is_another_principal(
        "Federal Excise Act, 2005", "Finance Act, 2024 (Z) Act, 2025", text
    )
    # The core has to be long enough to be a name, not a short token in the line.
    short = _side_by_side_body("the Finance Act, 2024, Tax Act, 1999 (Z) Act, 2025.")
    assert not is_rejected_assembly_statute_name("Tax Act, 1999")
    assert len(_title_core("Tax Act, 1999")) < 8
    assert not _listing_is_another_principal("Tax Act, 1999", "Finance Act, 2024", short)
    # Nowhere in the text.
    absent = "1. Short title.- This Act may be called the Finance Act, 2024.\n"
    assert not _listing_is_another_principal("Customs Act, 1969", "Finance Act, 2024", absent)
    # In the document, outside the called clause.
    outside = "The Customs Act, 1969 is amended by a later instrument.\n"
    assert not _listing_is_another_principal("Customs Act, 1969", "Finance Act, 2024", outside)
    # Past the first 100 characters of the called clause, inside the 700-character window.
    late = _side_by_side_body(
        "the Finance Act, 2024, " + ("Word " * 40) + "Customs Act, 1969 (Z) Act, 2025."
    )
    assert _listing_is_another_principal("Customs Act, 1969", "Finance Act, 2024", late)


@pytest.mark.no_db
def test_listing_keep_uses_the_listing_year():
    text = _side_by_side_body("the Finance Act, 2024, Federal Excise Act, 2005 (Z) Act, 2025.")
    name, year, detail = apply_rejected_assembly_title(
        name="Itr this Act",
        year=2024,
        text=text,
        listing_title="Federal Excise Act, 2005",
    )
    assert name == "Federal Excise Act, 2005"
    assert year == 2005
    assert detail == ""


_SPOT14_TITLES = (
    "the Constitution (Twenty-sixth Amendment) Act, 2024",
    "the Pakistan Penal Code (2nd Amendment) Act, 2025",
    "the Criminal Laws (Amending) Act, 2023",
    "the Banks (Removal of Difficulty) Act, 2022",
    "the Ordinances (Validation of Laws) Act, 2021",
    "the Finance (Amendment) (No. 2) Act, 2024",
    "the Police (Extension of Validity) Act, 2020",
)


@pytest.mark.no_db
@pytest.mark.parametrize("title", _SPOT14_TITLES)
def test_spot14_amending_listing_is_kept(title):
    stripped = re.sub(r"(?i)^the\s+", "", title)
    assert not is_rejected_assembly_statute_name(stripped)
    single = _side_by_side_body(f"{title}.")
    two = _side_by_side_body(f"the Finance Act, 2024, {title[4:]}.")
    for body in (single, two):
        out = _with_listing(body, source_name="NationalAssembly", listing="good", title=title)
        assert out["statute_name"] == title
        assert out["year_enacted"] == int(title[-4:])
        assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert is_rejected_assembly_statute_name("the following Act")
    assert is_rejected_assembly_statute_name("the Federal Government may by notification Act, 2024")
    assert is_rejected_assembly_statute_name(
        "the Federal Government or the Provincial Government Act, 2024"
    )
    assert is_rejected_assembly_statute_name("the Prime Minister may Act, 2024")
    assert is_rejected_assembly_statute_name("the Government Act, 2024")
    assert is_rejected_assembly_statute_name("the Trusts Act, 1882")


# A well-formed close on a good listing is the called title. The amending
# whitelist does not have to name it. Chrome and a missing listing still repair.
_VERBATIM_CLOSES = (
    "(Special Provisions)",
    "(Continuance)",
    "(Revival)",
    "(Abolition)",
)
_VERBATIM_JOINS = (" and ", " and the ", ", and ", ", and the ", " and\n", " & ")
_SHAPE_JUNK_TAILS = (
    " (Punjab) Act, 2025",
    " (Sindh) Act, 2025",
    " (Customs) Act, 2025",
    " (Sales Tax) Act, 2025",
    " (Third Schedule) Act, 2025",
    " (1st Schedule) Act, 2025",
    " (Deleted) Act, 2025",
    " (Inserted) Act, 2025",
    " (Lapsed) Act, 2025",
    " (Draft) Act, 2025",
    " (Bill) Act, 2025",
    " (Urdu) Act, 2025",
    " (Notification) Act, 2025",
    " (Explanation) Act, 2025",
    " (Proviso) Act, 2025",
    " (Corrigendum) Act, 2025",
    " (2nd) Act, 2025",
    " (4th) Act, 2025",
    " (3rd Proviso) Act, 2025",
    " (21st Century) Act, 2025",
    " (Gazette of Pakistan) Act, 2025",
    " Customs Act, 2025",
    " Punjab Act, 2025",
    " Bill Act, 2025",
)
_LEADING_THE_CLAUSES = (
    "the Federal Government may by notification Act, 2024",
    "the Federal Government or the Provincial Government Act, 2024",
    "the Prime Minister may Act, 2024",
    "the Government Act, 2024",
    "the Trusts Act, 1882",
    "the Land ln provement, Loans Act, 1883",
    "the Sind lrrigetion Act, 1879",
)


def _with_act_number(body: str, layout: str) -> str:
    if layout == "bare":
        return body
    return "ACT NO. I OF 2024\nAN ACT\nto amend the law\n" + body


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("join", _VERBATIM_JOINS)
@pytest.mark.parametrize("close", _VERBATIM_CLOSES)
def test_verbatim_listing_keeps_a_well_formed_close_on_every_join(
    source_name, verb, layout, join, close
):
    rendered = f"{_CLOSING_LEFT}{join}{_CLOSING_RIGHT} {close} Act, 2024"
    title = _collapsed_called(rendered)
    body = _with_act_number(_year_then_words_body(rendered, verb, ".", "end"), layout)
    assert _verbatim_good_listing(title, body)
    assert not _combining_suffix_at_end(title)
    out = _with_listing(body, source_name=source_name, listing="good", title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("verb", ["may", "shall"])
@pytest.mark.parametrize("layout", ["bare", "act-no"])
@pytest.mark.parametrize("close", _VERBATIM_CLOSES)
def test_verbatim_listing_keeps_a_well_formed_principal_close(source_name, verb, layout, close):
    title = f"Companies Act, 2017 {close} Act, 2024"
    body = _with_act_number(_year_then_words_body(title, verb, ".", "end"), layout)
    assert _verbatim_good_listing(title, body)
    out = _with_listing(body, source_name=source_name, listing="good", title=title)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["good", "chrome", "none"])
def test_repeal_and_saving_stays_a_title(source_name, listing):
    single = "Companies Act, 2017 (Repeal and Saving) Act, 2024"
    joined = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} (Repeal and Saving) Act, 2024"
    for title in (single, joined):
        assert not is_rejected_assembly_statute_name(title)
        assert _combining_suffix_at_end(title)
        body = _year_then_words_body(title, "may", ".", "end")
        out = _with_listing(body, source_name=source_name, listing=listing, title=title)
        assert out["statute_name"] == title
        assert out["year_enacted"] == 2024
        assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.no_db
@pytest.mark.parametrize("name", _LEADING_THE_CLAUSES)
def test_leading_the_does_not_make_a_clause_a_title(name):
    assert is_rejected_assembly_statute_name(name)


@pytest.mark.no_db
@pytest.mark.parametrize("tail", _SHAPE_JUNK_TAILS)
def test_non_amending_close_is_junk_by_shape(tail):
    assert is_rejected_assembly_statute_name(f"Finance Act, 2024{tail}")


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["chrome", "none"])
def test_numbered_act_after_a_closed_title_does_not_join(source_name, listing):
    title = "Finance Act, 2024 (No. 2) Act, 2025"
    assert is_rejected_assembly_statute_name(title)
    body = (
        "ACT NO. I OF 2024\n"
        "1. Short title.- This Act may be called the Finance Act, 2024 (No. 2) Act, 2025.\n"
    )
    out = _with_listing(body, source_name=source_name, listing=listing, title=title)
    stored = out["statute_name"] or ""
    assert stored != title
    assert "(No. 2) Act, 2025" not in stored
    assert not is_rejected_assembly_statute_name("Finance (No. 2) Act, 2024")
    assert not is_rejected_assembly_statute_name("Companies Act, 2017 (Amendment) (No. 2) Act, 2024")


@pytest.mark.no_db
def test_citation_year_then_section_dash_stops():
    body = (
        "1. Short title.- This Act may be called the Finance (No. 2) Act, 2024 "
        "(V of 2024). 2.- Amendment of the Income Tax Ordinance, 2001.\n"
    )
    short = _short_title(body) or ""
    assert "Amendment of" not in short
    assert "Ordinance" not in short
    out = _with_listing(body, source_name="NationalAssembly", listing="chrome", title="Finance (No. 2) Act, 2024")
    stored = out["statute_name"] or ""
    assert "Amendment of" not in stored
    assert "2001" not in stored


@pytest.mark.no_db
def test_stacked_parentheses_are_junk():
    assert is_rejected_assembly_statute_name("Finance Act, 2024 (No. 2) (Z) Act, 2025")
    assert is_rejected_assembly_statute_name("Finance Act, 2024 (Amendment) (Amendment) Act, 2025")
    assert is_rejected_assembly_statute_name("Finance Act, 2024 (Amendment) (No. 2) (No. 3) Act, 2025")
    assert not is_rejected_assembly_statute_name("Finance Act, 2024 (Amendment) (No. 2) Act, 2025")


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
@pytest.mark.parametrize("listing", ["chrome", "none"])
def test_act_number_year_does_not_keep_the_penal_code(source_name, listing):
    body = (
        "ACT NO. V OF 2024\n"
        "AN ACT\n"
        "further to amend the Pakistan Penal Code\n"
        "1. Short title.- This Act may be called the Code.\n"
        "(Amendment) Act, 2024.\n"
    )
    out = _with_listing(body, source_name=source_name, listing=listing, title="Pakistan Penal Code, 1860")
    assert out["statute_name"] != "Pakistan Penal Code, 1860"


@pytest.mark.no_db
@pytest.mark.parametrize("listing", ["Sales Tax Act, 1990", "Tax Act, 1990"])
def test_principal_listing_on_an_amending_body_is_corrected(listing):
    title = "Sales Tax Act, 1990 (Amendment) Act, 2024"
    body = _year_then_words_body(title, "may", ".", "end")
    out = _with_listing(body, source_name="NationalAssembly", listing="good", title=listing)
    assert out["statute_name"] == title
    assert out["year_enacted"] == 2024
    assert not (out.get("field_evidence") or {}).get("name_unknown")


@pytest.mark.no_db
def test_citation_prefix_is_required_to_reject_a_wrapper():
    assert _citation_prefix("2 Amendment")
    assert _citation_prefix("No. 2 Amendment")
    assert _citation_prefix("Act Amendment")
    assert _citation_prefix("V of Amendment")
    wrapped = f"{_CLOSING_LEFT} and {_CLOSING_RIGHT} (2 Amendment) Act, 2025"
    assert is_rejected_assembly_statute_name(wrapped)
    assert not _combining_suffix_at_end(wrapped)


@pytest.mark.no_db
@pytest.mark.parametrize("source_name", ["NationalAssembly", "Senate"])
def test_second_amending_instrument_is_not_stored(source_name):
    # No list joiner, so a connector check cannot reject this. Two amending
    # spans are the only reason it is not a title.
    title = "Sales Tax Act, 1990 (Amendment) Act, 2024 (Repeal) Act, 2025"
    assert len(_amending_spans(title)) >= 2
    assert not _connector_between_years(title)
    assert is_rejected_assembly_statute_name(title)
    body = _year_then_words_body(title, "may", ".", "end")
    out = _with_listing(body, source_name=source_name, listing="chrome", title="Finance Act, 2024")
    stored = out["statute_name"] or ""
    assert stored != title
    assert "Repeal Act, 2025" not in stored


@pytest.mark.no_db
def test_title_repair_does_not_disable_tls():
    blobs = [
        (REPO / "scraper" / "statute_quality" / "assembly_statute_title.py").read_text(encoding="utf-8"),
        SCRIPT.read_text(encoding="utf-8"),
    ]
    for blob in blobs:
        assert "verify=False" not in blob
        assert "CERT_NONE" not in blob
    script = blobs[1]
    assert "httpx" not in script
    assert "sqlalchemy" not in script
    assert "requests" not in script
