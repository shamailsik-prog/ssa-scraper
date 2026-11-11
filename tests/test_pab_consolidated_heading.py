"""pabalochistan consolidated reprints store the base Act heading.

The Chief Minister salaries PDF (sha256 a8144f0707e0…fc30) is a consolidated
base Act. Its first page is headed

    THE BALOCHISTAN CHIEF MINISTER AND PROVINCIAL MINISTERS
    (SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975.
    (Amended upto 9th October, 2014)

The acts listing, and the filename ``act282014``, label that file as the
Amendment Act No. XXVIII of 2014. The stored title and year follow the
base heading. The cutoff and the listing or filename label stay metadata.
The cutoff is not part of the name.

Fixtures are pdfplumber ``extract_text(x_tolerance=2, y_tolerance=2)`` of
each PDF's first page, the same settings as ``_pdfplumber_extract``.
"""

import re
from pathlib import Path

from scraper.extractors.deterministic import extract_statute_deterministic

_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "statute_quality"
CM_PAGE = _FIXTURES / "pab_cm_privileges_act_1975_amended_upto_2014_page1.txt"
SPEAKER_PAGE = _FIXTURES / "pab_speaker_privileges_act_1975_amended_upto_2014_page1.txt"
AMENDMENT_PAGE = _FIXTURES / "pab_judicial_academy_amendment_2025_page1.txt"

CM_URL = "https://pabalochistan.gov.pk/storage/3973/2018-10-23-14_07_58act282014.pdf"
CM_LISTING = (
    "The Balochistan Chief Minister and Provincial Ministers "
    "(Salaries , Allowances and Privileges) (Amendment) Act No. XXVIII of 2014"
)
CM_HEADING = (
    "THE BALOCHISTAN CHIEF MINISTER AND PROVINCIAL MINISTERS "
    "(SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975"
)
CM_CUTOFF = "Amended upto 9th October, 2014"
SPEAKER_URL = "https://pabalochistan.gov.pk/storage/3972/2018-10-23-14_06_39act292014.pdf"
SPEAKER_LISTING = (
    "The Balochistan Speaker's and Deputy Speaker's "
    "(Salaries, Allowances, and Privileges) (Amendment) Act No. XXIX Of 2014"
)
SPEAKER_HEADING = (
    "THE BALOCHISTAN SPEAKER’S AND DEPUTY SPEAKER’S "
    "(SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975"
)
SPEAKER_CUTOFF = "Amended up to 9th October, 2014"
_CUTOFF_IN_NAME_RE = re.compile(r"(?i)\(\s*(?:amended\s+up\s*to|as\s+amended)\b")
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
    assert _CUTOFF_IN_NAME_RE.search(extracted["statute_name"]) is None
    assert extracted["field_evidence"]["pab_amended_upto"] == CM_CUTOFF
    assert extracted["field_evidence"]["pab_amendment_label"] == CM_LISTING
    assert all(section["year_enacted"] == 1975 for section in extracted["sections"])
    assert all(section["statute_name"] == CM_HEADING for section in extracted["sections"])


def test_filename_act_number_is_metadata_not_the_title():
    text = CM_PAGE.read_text(encoding="utf-8")
    extracted = _extract(text, url=CM_URL, act_title=None)

    assert extracted["statute_name"] == CM_HEADING
    assert extracted["year_enacted"] == 1975
    assert _CUTOFF_IN_NAME_RE.search(extracted["statute_name"]) is None
    assert extracted["field_evidence"]["pab_amended_upto"] == CM_CUTOFF
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

    assert extracted["statute_name"] == "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010"
    assert extracted["year_enacted"] == 2010
    assert "VI of 2023" not in extracted["statute_name"]
    assert _CUTOFF_IN_NAME_RE.search(extracted["statute_name"]) is None
    assert extracted["field_evidence"]["pab_amended_upto"] == "as amended"
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


def test_wrapped_cutoff_year_stays_in_evidence_not_the_name():
    listing = "The Balochistan Local Government (Amendment) Act No. VI of 2023"

    def wrapped(year: str) -> str:
        return (
            "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010.\n"
            "(Amended upto 9th October,\n"
            f"{year}).\n"
            "An Act to consolidate the law relating to local government.\n"
        )

    earlier = _extract(
        wrapped("2014"),
        url="https://pabalochistan.gov.pk/storage/5001/local-government-upto-2014.pdf",
        act_title=listing,
        act_year="2023",
        act_no="VI of 2023",
    )
    later = _extract(
        wrapped("2015"),
        url="https://pabalochistan.gov.pk/storage/5002/local-government-upto-2015.pdf",
        act_title=listing,
        act_year="2023",
        act_no="VI of 2023",
    )

    base = "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010"
    assert earlier["statute_name"] == base
    assert later["statute_name"] == base
    assert earlier["year_enacted"] == 2010
    assert later["year_enacted"] == 2010
    assert earlier["field_evidence"]["pab_amended_upto"] == "Amended upto 9th October, 2014"
    assert later["field_evidence"]["pab_amended_upto"] == "Amended upto 9th October, 2015"
    assert "VI of 2023" not in earlier["statute_name"]
    assert "VI of 2023" not in later["statute_name"]


def test_unclosed_cutoff_does_not_drop_the_year_into_the_title():
    text = (
        "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010.\n"
        "(Amended upto 9th October,\n"
        "An Act to consolidate the law relating to local government.\n"
    )
    listing = "The Balochistan Local Government (Amendment) Act No. VI of 2023"
    extracted = _extract(
        text,
        url="https://pabalochistan.gov.pk/storage/5003/local-government-open-cutoff.pdf",
        act_title=listing,
        act_year="2023",
        act_no="VI of 2023",
    )

    assert extracted["statute_name"] == listing
    assert extracted["year_enacted"] == 2023
    assert "pab_amendment_label" not in (extracted.get("field_evidence") or {})


def test_preamble_parenthetical_as_amended_keeps_the_amendment_title():
    text = (
        "THE BALOCHISTAN JUDICIAL ACADEMY (AMENDMENT) ACT, 2025.\n"
        "An Act further to amend the Balochistan Judicial Academy Act, 2010 (as amended).\n"
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
    assert "2010" not in extracted["statute_name"]
    assert "pab_amendment_label" not in (extracted.get("field_evidence") or {})


def test_wrapped_as_amended_under_a_preamble_keeps_the_amendment_title():
    text = (
        "THE BALOCHISTAN JUDICIAL ACADEMY (AMENDMENT) ACT, 2025.\n"
        "An Act further to amend the Balochistan Judicial Academy Act, 2010\n"
        "(as amended).\n"
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
    assert "2010" not in extracted["statute_name"]
    assert "pab_amendment_label" not in (extracted.get("field_evidence") or {})


def test_speaker_reprint_skips_the_act_number_line():
    text = SPEAKER_PAGE.read_text(encoding="utf-8")
    extracted = _extract(
        text,
        url=SPEAKER_URL,
        act_title=SPEAKER_LISTING,
        act_year="2014",
        act_no="XXIX of 2014",
    )

    assert extracted["statute_name"] == SPEAKER_HEADING
    assert extracted["year_enacted"] == 1975
    assert "XXIX" not in extracted["statute_name"]
    assert "(Amendment)" not in extracted["statute_name"]
    assert _CUTOFF_IN_NAME_RE.search(extracted["statute_name"]) is None
    assert extracted["field_evidence"]["pab_amended_upto"] == SPEAKER_CUTOFF
    assert extracted["field_evidence"]["pab_amendment_label"] == SPEAKER_LISTING

    with_gaps = (
        "THE BALOCHISTAN SPEAKER'S AND DEPUTY SPEAKER'S\n"
        "(SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975.\n"
        "\n"
        ".....\n"
        "(BALOCHISTAN ACT NO. XXIX OF 2014)\n"
        "(Amended up to 9th October, 2014).\n"
        "An Act to provide for the salaries of the Speaker.\n"
    )
    gapped = _extract(
        with_gaps,
        url=SPEAKER_URL,
        act_title=SPEAKER_LISTING,
        act_year="2014",
        act_no="XXIX of 2014",
    )
    assert gapped["statute_name"] == (
        "THE BALOCHISTAN SPEAKER'S AND DEPUTY SPEAKER'S "
        "(SALARIES, ALLOWANCES AND PRIVILEGES) ACT, 1975"
    )
    assert gapped["year_enacted"] == 1975
    assert "XXIX" not in gapped["statute_name"]
    assert gapped["field_evidence"]["pab_amended_upto"] == SPEAKER_CUTOFF


def test_no_produced_name_carries_a_cutoff_suffix():
    produced = [
        _extract(
            CM_PAGE.read_text(encoding="utf-8"),
            url=CM_URL,
            act_title=CM_LISTING,
            act_year="2014",
            act_no="XXVIII of 2014",
        ),
        _extract(CM_PAGE.read_text(encoding="utf-8"), url=CM_URL, act_title=None),
        _extract(
            "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010.\n(as amended)\n"
            "An Act to consolidate the law relating to local government.\n",
            url="https://pabalochistan.gov.pk/storage/5000/local-government-act.pdf",
            act_title="The Balochistan Local Government (Amendment) Act No. VI of 2023",
        ),
        _extract(
            "THE BALOCHISTAN LOCAL GOVERNMENT ACT, 2010.\n"
            "(Amended upto 9th October,\n2014).\n"
            "An Act to consolidate the law relating to local government.\n",
            url="https://pabalochistan.gov.pk/storage/5001/local-government-upto-2014.pdf",
            act_title="The Balochistan Local Government (Amendment) Act No. VI of 2023",
        ),
        _extract(
            SPEAKER_PAGE.read_text(encoding="utf-8"),
            url=SPEAKER_URL,
            act_title=SPEAKER_LISTING,
            act_year="2014",
            act_no="XXIX of 2014",
        ),
        _extract(
            AMENDMENT_PAGE.read_text(encoding="utf-8"),
            url=AMENDMENT_URL,
            act_title=AMENDMENT_LISTING,
            act_year="2026",
            act_no="VII of 2026",
        ),
    ]
    for extracted in produced:
        assert _CUTOFF_IN_NAME_RE.search(extracted["statute_name"] or "") is None


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
