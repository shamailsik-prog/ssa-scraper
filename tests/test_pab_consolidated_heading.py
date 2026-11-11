"""pabalochistan consolidated reprints keep the base Act heading.

The Chief Minister salaries PDF (sha256 a8144f0707e0…fc30) is a consolidated
base Act. Its first page is headed

    THE BALOCHISTAN CHIEF MINISTER AND PROVINCIAL MINISTERS
    (SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975.
    (Amended upto 9th October, 2014)

The acts listing, and the filename ``act282014``, label that file as the
Amendment Act No. XXVIII of 2014. The stored title and year follow the
heading. The listing or filename label stays metadata and is not used to
merge this edition with any other.

Fixtures are pdfplumber ``extract_text(x_tolerance=2, y_tolerance=2)`` of
each PDF's first page, the same settings as ``_pdfplumber_extract``.
"""

from pathlib import Path

from scraper.extractors.deterministic import extract_statute_deterministic

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"
CM_PAGE = _FIXTURES / "pab_cm_privileges_act_1975_amended_upto_2014_page1.txt"
AMENDMENT_PAGE = _FIXTURES / "pab_judicial_academy_amendment_2025_page1.txt"

CM_URL = "https://pabalochistan.gov.pk/storage/3973/2018-10-23-14_07_58act282014.pdf"
CM_LISTING = (
    "The Balochistan Chief Minister and Provincial Ministers "
    "(Salaries , Allowances and Privileges) (Amendment) Act No. XXVIII of 2014"
)
CM_HEADING = (
    "THE BALOCHISTAN CHIEF MINISTER AND PROVINCIAL MINISTERS "
    "(SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975 "
    "(Amended upto 9th October, 2014)"
)
AMENDMENT_URL = (
    "https://pabalochistan.gov.pk/storage/9214/"
    "69f8926cee401_The-Balochistan-Judicial-Academy-(Amendment)-Act,-2025.pdf"
)
AMENDMENT_LISTING = (
    "THE BALOCHISTAN JUDICIAL ACADEMY (AMENDMENT) ACT, 2025. ACT NO. VII OF 2026"
)


def _extract(text: str, *, url: str, act_title: str | None, **extra):
    meta = {
        "source_name": "BalochistanAssembly",
        "url": url,
        "act_year": extra.pop("act_year", None),
        "act_no": extra.pop("act_no", None),
    }
    if act_title is not None:
        meta["act_title"] = act_title
    meta.update(extra)
    return extract_statute_deterministic(html=None, text=text, source_meta=meta)


def test_amended_upto_heading_replaces_listing_amendment_label():
    text = CM_PAGE.read_text(encoding="utf-8")
    extracted = _extract(
        text,
        url=CM_URL,
        act_title=CM_LISTING,
        act_year="2014",
        act_no="XXVIII of 2014",
    )

    assert extracted["statute_name"] == CM_HEADING
    assert extracted["year_enacted"] == 1975
    assert "XXVIII" not in extracted["statute_name"]
    assert "(Amendment)" not in extracted["statute_name"]
    assert extracted["field_evidence"]["pab_amendment_label"] == CM_LISTING
    assert all(section["year_enacted"] == 1975 for section in extracted["sections"])
    assert all(section["statute_name"] == CM_HEADING for section in extracted["sections"])


def test_filename_act_number_is_metadata_not_the_title():
    text = CM_PAGE.read_text(encoding="utf-8")
    extracted = _extract(text, url=CM_URL, act_title=None)

    assert extracted["statute_name"] == CM_HEADING
    assert extracted["year_enacted"] == 1975
    assert extracted["field_evidence"]["pab_amendment_label"] == "Act No. 28 of 2014"


def test_as_amended_marker_uses_the_base_act_heading():
    text = (
        "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010.\n"
        "(as amended)\n"
        "An Act to consolidate the law relating to local government.\n"
        "1. Short title. This Act may be called the Balochistan Local Government Act, 2010.\n"
    )
    listing = "The Balochistan Local Government (Amendment) Act No. VI of 2023"
    extracted = _extract(
        text,
        url="https://pabalochistan.gov.pk/storage/5000/local-government-act.pdf",
        act_title=listing,
        act_year="2023",
        act_no="VI of 2023",
    )

    assert extracted["statute_name"] == "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010 (as amended)"
    assert extracted["year_enacted"] == 2010
    assert "VI of 2023" not in extracted["statute_name"]
    assert extracted["field_evidence"]["pab_amendment_label"] == listing


def test_real_amendment_act_keeps_its_amendment_title():
    text = AMENDMENT_PAGE.read_text(encoding="utf-8")
    extracted = _extract(
        text,
        url=AMENDMENT_URL,
        act_title=AMENDMENT_LISTING,
        act_year="2026",
        act_no="VII of 2026",
    )

    assert extracted["statute_name"] == AMENDMENT_LISTING
    assert extracted["year_enacted"] == 2025
    assert "2010" not in extracted["statute_name"]
    assert "pab_amendment_label" not in (extracted.get("field_evidence") or {})


def test_sentence_as_amended_does_not_replace_an_amendment_title():
    text = (
        "THE BALOCHISTAN JUDICIAL ACADEMY (AMENDMENT) ACT, 2025.\n"
        "An Act further to amend the Balochistan Judicial Academy Act, 2010, as amended from time to time.\n"
        "1. Short title. This Act may be called the Balochistan Judicial Academy (Amendment) Act, 2025.\n"
    )
    extracted = _extract(
        text,
        url=AMENDMENT_URL,
        act_title=AMENDMENT_LISTING,
        act_year="2026",
        act_no="VII of 2026",
    )

    assert extracted["statute_name"] == AMENDMENT_LISTING
    assert extracted["year_enacted"] == 2025


def test_balochistan_code_host_keeps_the_listing_title():
    text = CM_PAGE.read_text(encoding="utf-8")
    extracted = _extract(
        text,
        url="https://balochistancode.gob.pk/Document.aspx?docc=1&docid=2&wise=download",
        act_title=CM_LISTING,
        act_year="2014",
        act_no="XXVIII of 2014",
    )

    assert extracted["statute_name"] == CM_LISTING
    assert extracted["year_enacted"] == 2014
