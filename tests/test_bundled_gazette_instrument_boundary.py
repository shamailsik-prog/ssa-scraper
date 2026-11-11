"""Next-instrument cut and the no-pin PDF tuple for bundled-gazette re-extract.

A multi-act gazette stops at the next Act or Ordinance heading. An in-body
repeal of the Emigration Act stays. Apply still requires a sha pin.
"""

from __future__ import annotations

import asyncio
from uuid import UUID

from scraper.extractors.deterministic import extract_statute_deterministic

ANTIQUITIES = "The Antiquities Act, 1976"
ANTIQUITIES_URL = "https://senate.gov.pk/uploads/documents/1660631634_471.pdf|VII of 1976"
MAIN_ID = UUID("a125355e-f78f-4421-81ef-b79fc16a4eb5")
FS_ID = UUID("2ceea487-53ab-46a6-b8b2-406cf1146a11")
PIN = "b6eee4aa83fcb53b7b7e07a33e25131f7c4e4cf98e1037b753e4ae71a915151c"
_EIGHT = {
    UUID("5636b724-6a47-42a7-93f5-4d78e07ddea8"),
    UUID("92aeaf99-7603-41d6-a541-f2c38fe27d04"),
    UUID("21f60373-b62a-4df8-9f0f-947dcf8a352e"),
    UUID("06a5058e-511e-4039-befa-947f1fcdfd68"),
    UUID("6c73a686-c038-4611-8f05-8a429168c8a7"),
    UUID("10cc6efa-f845-42e5-83ee-11eeabd0bc63"),
    UUID("0181ad58-1a79-479c-bdd8-dad584cfd683"),
    UUID("7f0a008c-6775-4a44-87b0-4e0e5c1b5b02"),
}


def _operative(number: int) -> str:
    return (
        f"{number}. Operative provision.—Section {number} of the Antiquities Act "
        "states a distinct rule about protected antiquities."
    )


def _antiquities_body() -> str:
    lines = [
        "ACT No. VII OF",
        "1976",
        "An Act to repeal and re-enact the law relating to antiquities.",
        "1. Short title.—This Act may be called the Antiquities Act, 1976.",
        "2. Definitions.—In this Act, antiquity means an ancient object and not an engineering council.",
    ]
    lines.extend(_operative(number) for number in range(3, 38))
    lines.append("38. Repeal.—The Antiquities Act, 1968 (XIV of 1968), is hereby repealed.")
    lines.extend(
        [
            "M. A. HAQ,",
            "Secretary.",
            "No. F. 24(1)/76-Pub.- The following Ordinance made by the President is hereby published for general information:-",
        ]
    )
    return "\n".join(lines)


def _extract(text: str, *, url: str = ANTIQUITIES_URL, title: str = ANTIQUITIES) -> dict:
    return extract_statute_deterministic(
        html=None,
        text=text,
        source_meta={
            "source_name": "Senate",
            "url": url,
            "act_title": title,
            "jurisdiction": "Federal",
        },
    )


def _numbers(extracted: dict) -> list[str]:
    return [str(section["section_number"]) for section in extracted["sections"]]


def _by_number(extracted: dict) -> dict[str, str]:
    return {str(section["section_number"]): section["section_text"] for section in extracted["sections"]}


def test_multi_act_gazette_stops_at_the_next_ordinance_heading():
    text = "\n".join(
        [
            "ACT No. V OF 1976",
            "An Act to regulate the engineering profession.",
            "1. Short title.—This Act may be called the Pakistan Engineering Council Act, 1976.",
            "2. Definitions.—In this Act, Council means the Pakistan Engineering Council constituted under section 3.",
            _antiquities_body(),
            "ORDINANCE No. II OF 1976",
            "AN",
            "ORDINANCE",
            "to repeal and, with certain modifications, re-enact the Emigration Act, 1922",
            "WHEREAS it is expedient to repeal the Emigration Act, 1922;",
            "1. Short title.—This Ordinance may be called the Emigration Ordinance, 1976.",
            "2. Definitions.—In this Ordinance, emigrant means a person who departs from Pakistan.",
            "2J. Finality of order.—An order made by the prescribed authority under this Ordinance shall be final.",
            "39. Saving.—Nothing in this Ordinance shall apply to a person who is not a citizen of Pakistan.",
            "40. Repeal.—The Emigration Act, 1922 (VII of 1922), is hereby repealed.",
            "1960. Crew.—A person engaged as crew under the Civil Aviation Ordinance, 1960, is outside this Ordinance.",
        ]
    )
    extracted = _extract(text)
    numbers = _numbers(extracted)
    bodies = _by_number(extracted)
    blob = "\n".join(bodies.values())
    assert numbers == [str(number) for number in range(1, 39)]
    assert "Antiquities Act, 1976" in bodies["1"]
    assert "ancient object" in bodies["2"]
    assert "Pakistan Engineering Council" not in bodies["2"]
    assert "emigrant means" not in bodies["2"]
    assert "is hereby repealed" in bodies["38"]
    assert "Secretary" in bodies["38"]
    assert "ORDINANCE No." not in bodies["38"]
    assert "following Ordinance" not in bodies["38"]
    assert "Emigration Ordinance" not in blob
    assert "Finality of order" not in blob
    assert "Civil Aviation" not in blob
    for banned in ("2J", "1960", "39", "40"):
        assert banned not in numbers


def test_ocr_or_for_of_and_a_title_line_also_end_the_act():
    ordinance_number = "\n".join(
        [
            _antiquities_body(),
            "ORDINANCE No. II or 1976",
            "An Ordinance to provide for emigration.",
            "1. Short title.—This Ordinance may be called the Emigration Ordinance, 1976.",
            "39. Saving.—Nothing in this Ordinance applies to crew members leaving Pakistan.",
        ]
    )
    title_line = "\n".join(
        [
            "ACT No. VII OF 1976",
            "An Act to preserve antiquities.",
            "1. Short title.—This Act may be called the Antiquities Act, 1976.",
            "2. Definitions.—In this Act, antiquity means an ancient object kept for study.",
            "38. Repeal.—The Antiquities Act, 1968, is hereby repealed.",
            "THE EMIGRATION ORDINANCE, 1976",
            "An Ordinance to repeal and re-enact the Emigration Act, 1922.",
            "1. Short title.—This Ordinance may be called the Emigration Ordinance, 1976.",
            "2. Definitions.—In this Ordinance, emigrant means a person who leaves Pakistan.",
        ]
    )
    for text in (ordinance_number, title_line):
        extracted = _extract(text)
        bodies = _by_number(extracted)
        blob = "\n".join(bodies.values())
        assert "38" in bodies
        assert "is hereby repealed" in bodies["38"]
        assert "Emigration Ordinance" not in blob
        assert "emigrant means" not in blob
        assert "39" not in _numbers(extracted)


def test_later_act_number_without_an_immediate_preamble_does_not_splice_section_2():
    text = "\n".join(
        [
            "ACT No. VII OF 1976",
            "An Act to preserve antiquities.",
            "1. Short title.—This Act may be called the Antiquities Act, 1976.",
            "2. Definitions.—In this Act, antiquity means an ancient object kept for study.",
            "38. Repeal.—The Antiquities Act, 1968, is hereby repealed.",
            "ACT No. VIII OF 1976",
            "Gazette notice of a later instrument without an immediate preamble line.",
            "The notice continues on this line and does not yet enact anything.",
            "It still has not said that this is an act to do anything in particular.",
            "A fourth line of the notice sits before any short title of the later act.",
            "1. Short title.—This Act may be called the Pakistan Engineering Council Act, 1976.",
            "2. Definitions.—Council means the Pakistan Engineering Council under this later Act.",
        ]
    )
    extracted = _extract(text)
    bodies = _by_number(extracted)
    assert "ancient object" in bodies["2"]
    assert "Pakistan Engineering Council" not in bodies["2"]
    assert "Pakistan Engineering Council" not in "\n".join(bodies.values())


def test_emigration_repeal_inside_a_section_is_not_an_instrument_boundary():
    text = "\n".join(
        [
            "ACT No. VII OF 1976",
            "An Act to preserve antiquities.",
            "1. Short title.—This Act may be called the Antiquities Act, 1976.",
            "2. Definitions.—In this Act, antiquity means an ancient object kept in Pakistan.",
            "12. Repeal of an earlier emigration law.—Subject to the savings in this section,",
            "the Emigration Act, 1922",
            "is hereby repealed and the rules made under that Act cease to apply to antiquities.",
            "13. Savings.—A licence issued under the repealed Act remains in force for an antiquity.",
        ]
    )
    extracted = _extract(text)
    bodies = _by_number(extracted)
    assert "is hereby repealed" in bodies["12"]
    assert "Emigration Act, 1922" in bodies["12"]
    assert "licence issued" in bodies["13"]
    assert _numbers(extracted) == ["1", "2", "12", "13"]


def test_no_pin_branch_returns_bytes_then_origin():
    from scraper.tasks.bundled_gazette_acts import PIN_UNTRUSTED, _resolve_pdf_text, accept_pinned_pdf

    blob = b"%PDF-1.4\nantiquities bytes"
    data, origin = accept_pinned_pdf((("recorded", blob),))
    assert data == blob
    assert origin == "recorded"
    assert isinstance(data, bytes)
    assert isinstance(origin, str)

    mismatched, mismatch_origin = accept_pinned_pdf(
        (("recorded", blob),),
        pinned_sha=PIN_UNTRUSTED,
    )
    assert mismatched is None
    assert mismatch_origin == "sha_mismatch"

    seen: dict = {}

    def extract_text(payload):
        seen["payload"] = payload
        return "1. Short title.—This Act may be called the Antiquities Act, 1976."

    text, resolved_origin, available = asyncio.run(
        _resolve_pdf_text(
            "https://senate.gov.pk/uploads/documents/1660631634_471.pdf",
            db=None,
            fetch_pdf=None,
            recorded_loader=lambda _url: blob,
            extract_text=extract_text,
            skip_fetch=True,
            pinned_sha=None,
        )
    )
    assert seen["payload"] == blob
    assert isinstance(seen["payload"], bytes)
    assert resolved_origin == "recorded"
    assert available is True
    assert "Antiquities Act" in text


def test_apply_still_requires_a_sha_pin(monkeypatch):
    import scraper.tasks.bundled_gazette_acts as acts
    from scraper.tasks.bundled_gazette_reextract import _approval

    assert _EIGHT <= acts.APPROVED_REEXTRACT_IDS
    for statute_id in (MAIN_ID, FS_ID):
        pin, reason = _approval(statute_id)
        assert reason == ""
        assert pin == PIN

    hidden = UUID("d7d3187e-9ebd-4782-aa9a-228b40d2dd96")
    hidden_pin, hidden_reason = _approval(hidden)
    assert hidden_pin is None
    assert hidden_reason == "not_approved"

    pins = dict(acts.CONFIRMED_PDF_SHA256)
    pins[MAIN_ID] = ""
    monkeypatch.setattr(acts, "CONFIRMED_PDF_SHA256", pins)
    cleared, cleared_reason = _approval(MAIN_ID)
    assert cleared is None
    assert cleared_reason == "unpinned"
