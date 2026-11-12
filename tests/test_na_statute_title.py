"""NA and Senate statute names come from the instrument, not OCR or listing chrome."""

from __future__ import annotations

import csv
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

from scraper.extractors.deterministic import extract_statute_deterministic
from scraper.fetchers import record_provenance, stage_statute, statute_staging_identity_url
from scraper.models import QuarantineQueue, ScraperSource, Statute
from scraper.statute_quality.assembly_statute_title import (
    _short_title,
    _short_title_supersedes,
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
    assert out["year_enacted"] in _years_printed(title)
    assert not (out.get("field_evidence") or {}).get("name_unknown")
    assert not is_rejected_assembly_statute_name(title)
    if listing == "good":
        assert _short_title(body) == title


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
