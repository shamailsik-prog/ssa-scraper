"""Detect statutes whose stored section numbers are OCR page or line numbers.

A scanned gazette sometimes keeps page numbers (3, 8, 10, 11) as
``section_number`` and leaves the real ``N. Title.—`` headings inside one of
those rows. This module only classifies stored text. It does not renumber
sections and it does not treat a bare page or line number as a section.

Senate-sourced rows are out of scope. Their partial OCR garble is a different
defect and stays on the Senate integrity sweep.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from scraper.parsers.statute_parser import _parse_numeric_section_number
from scraper.statute_quality.senate_integrity import is_senate_sourced

VERDICT_MATCH = "MATCH"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_SUSPECT = "SUSPECT"
VERDICT_OUT_OF_SCOPE = "OUT_OF_SCOPE"

ACTION_NONE = "none"
ACTION_QUARANTINE = "quarantine"
ACTION_NEEDS_REEXTRACT = "needs_reextract"

REASON_HEADINGS_ABOVE_STORED = "ocr_section_mash:headings_above_stored"
REASON_FEW_SECTIONS_SCANNED = "ocr_section_mash:few_sections_scanned_source"

# Stored section numbers at or under this count are "very few".
FEW_STORED_SECTIONS = 5
# A heading this far above the row's own number is not that row's section.
HEADING_GAP = 8
# One quoted section is not a mash. Two higher headings are.
MIN_HIGHER_HEADINGS = 2
# Distinct headings in an unnumbered row, beyond the stored section count.
WIDE_COUNT_MARGIN = 8

_PAGE_OR_LINE_RE = re.compile(
    r"(?i)^(?:"
    r"page\s+\d{1,4}(?:\s+of\s+\d{1,4})?"
    r"|-\s*\d{1,4}\s*-"
    r"|\d{1,4}"
    r"|\d{1,4}\s*\."
    r"|\d{1,4}\s+of\s+\d{1,4}"
    r")$"
)
_HEADING_RE = re.compile(
    r"^(?:(?P<page>\d{1,4})[ \t]+)?"
    r"(?:(?:[Ss]ection|[Ss]ec\.)[ \t]+)?"
    r"(?P<num>\d{1,3})(?P<suffix>[A-Z])?"
    r"[ \t]*\.[ \t]*"
    r"(?P<rest>\S.*?)$"
)
_OF_PAGE_RE = re.compile(r"(?i)^of\s+\d")
_DASH_RE = re.compile(r"(?:—|–|\.—|\.-|--)")
_SENTENCE_START_RE = re.compile(
    r"(?i)^(this|in|subject|whereas|provided|if|where|it|any|every|"
    r"notwithstanding|on|under|for|no|the|a|an)\b"
)
_QUOTE_CHARS = "\"'“”‘’«»"
# Inserted or substituted text is the other Act's sections, not this row's numbering.
_AMENDMENT_INTRO_RE = re.compile(
    r"(?i)(?:"
    r"\bnamely\b"
    r"|\bfollowing\b[^.\n]{0,180}?\b(?:inserted|substituted|added)\b"
    r"|\b(?:shall\s+be|hereby)\s+(?:inserted|substituted|added)\b"
    r")"
)


@dataclass(frozen=True)
class OcrSectionMashVerdict:
    verdict: str
    action: str
    reason: str
    reasons: tuple[str, ...]
    signals: tuple[str, ...]
    stored_count: int
    stored_max: Optional[int]
    embedded_max: Optional[int]
    embedded_headings: tuple[int, ...]


def pdf_lacks_text_layer(pdf_bytes: bytes) -> bool:
    """True when preserved PDF bytes have no usable text layer.

    Uses ``pdf_text_layer_info``, which never OCRs. A scanned-page hint or a
    thin text layer is the existing no-text-layer signal. This helper does not
    read section numbers off the page.
    """
    from scraper.corpus_record import text_is_thin
    from scraper.fetchers import pdf_text_layer_info

    if not pdf_bytes:
        return False
    text, scanned = pdf_text_layer_info(pdf_bytes)
    return bool(scanned) or text_is_thin(text)


def _prepare_line(line: str) -> str:
    return (line or "").strip()


def _is_quoted_line(line: str) -> bool:
    text = (line or "").lstrip()
    return bool(text) and text[0] in _QUOTE_CHARS


def _heading_on_line(line: str) -> Optional[int]:
    match = _HEADING_RE.match(line)
    if match is None:
        return None
    return _accepted_heading_number(match.group("num"), match.group("rest") or "")


def _accepted_heading_number(num_text: str, rest: str) -> Optional[int]:
    if _OF_PAGE_RE.match(rest.strip()):
        return None
    try:
        number = int(num_text)
    except ValueError:
        return None
    if number <= 0 or number > 999:
        return None
    title = re.split(r"\.{3,}", rest.strip(), maxsplit=1)[0].strip()
    if len(re.findall(r"[A-Za-z]", title)) < 3:
        return None
    parts = _DASH_RE.split(title, maxsplit=1)
    head = parts[0].strip(" .")
    has_dash = len(parts) > 1
    if len(re.findall(r"[A-Za-z]", head)) < 3:
        return None
    if not has_dash and (len(head) > 90 or _SENTENCE_START_RE.match(head)):
        return None
    return number


def embedded_section_headings(text: str) -> tuple[tuple[int, ...], bool]:
    """Return operative ``N.`` / ``Section N.`` headings and whether page noise was skipped.

    A line that is only a page number, ``Page N of M``, ``- N -``, or ``N.``
    is not a section heading. Quoted lines, and headings after amendment
    scaffolding (``namely``, ``following … inserted|substituted|added``), are
    the text being inserted into another Act. They are not headings of this row.
    ``statute_contents`` strips a section's own ``N.`` heading before storage,
    so a missing own heading is not itself a mash.
    """
    found: list[int] = []
    ignored = False
    in_amendment = False
    for raw_line in (text or "").splitlines():
        line = _prepare_line(raw_line)
        if not line:
            continue
        if _PAGE_OR_LINE_RE.match(line):
            ignored = True
            continue
        if _is_quoted_line(line):
            if _AMENDMENT_INTRO_RE.search(line):
                in_amendment = True
            continue
        cue = _AMENDMENT_INTRO_RE.search(line)
        if cue is not None:
            prefix = line[: cue.start()].strip()
            if prefix:
                number = _heading_on_line(prefix)
                if number is not None:
                    found.append(number)
            in_amendment = True
            continue
        if in_amendment:
            continue
        match = _HEADING_RE.match(line)
        if match is None:
            continue
        if match.group("page"):
            ignored = True
        number = _accepted_heading_number(match.group("num"), match.group("rest") or "")
        if number is not None:
            found.append(number)
    return tuple(found), ignored


def _section_number(section: dict) -> Optional[int]:
    raw = str(section.get("section_number") or "").strip()
    number = _parse_numeric_section_number(raw)
    if number is None:
        return None
    if re.fullmatch(r"\d{4}", raw) and 1800 <= number <= 2100:
        return None
    return number


def classify_ocr_section_mash(
    sections: Sequence[dict],
    *,
    source_url: str = "",
    source_name: str = "",
    scanned_source: Optional[bool] = None,
) -> OcrSectionMashVerdict:
    """Classify one statute from its stored sections.

    ``scanned_source`` is True only when a preserved PDF has no text layer
    (or the caller already knows that). ``None`` means unknown and does not
    raise the few-sections reason. Senate sources return ``OUT_OF_SCOPE``.
    """
    if is_senate_sourced(source_url, source_name):
        return OcrSectionMashVerdict(
            verdict=VERDICT_OUT_OF_SCOPE,
            action=ACTION_NONE,
            reason="",
            reasons=(),
            signals=("senate_ocr_garble_not_this_class",),
            stored_count=0,
            stored_max=None,
            embedded_max=None,
            embedded_headings=(),
        )

    stored_numbers: list[int] = []
    all_headings: list[int] = []
    orphan_headings: list[int] = []
    ignored = False
    mashed = False
    usable = 0
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        text = section.get("section_text") or ""
        number_raw = str(section.get("section_number") or "").strip()
        if not str(text).strip() and not number_raw:
            continue
        usable += 1
        own = _section_number(section)
        if own is not None:
            stored_numbers.append(own)
        headings, saw_page = embedded_section_headings(str(text))
        if saw_page:
            ignored = True
        all_headings.extend(headings)
        # A stored section is a mash when operative headings (not amendment
        # insertions) sit far above its number. The section's own heading may
        # already have been stripped; its absence is not the signal.
        foreign = [item for item in headings if own is None or item != own]
        orphan_headings.extend(foreign)
        if own is None:
            continue
        higher = sorted({item for item in foreign if item > own})
        if len(higher) >= MIN_HIGHER_HEADINGS and higher[-1] >= own + HEADING_GAP:
            mashed = True

    stored_max = max(stored_numbers) if stored_numbers else None
    distinct_orphan = set(orphan_headings)
    if (
        usable
        and distinct_orphan
        and len(distinct_orphan) >= usable + WIDE_COUNT_MARGIN
        and (stored_max is None or max(distinct_orphan) >= stored_max + HEADING_GAP)
    ):
        mashed = True

    unique = tuple(sorted(set(all_headings)))
    embedded_max = unique[-1] if unique else None
    few = 0 < usable <= FEW_STORED_SECTIONS and scanned_source is True
    reasons: list[str] = []
    if mashed:
        reasons.append(REASON_HEADINGS_ABOVE_STORED)
    if few:
        reasons.append(REASON_FEW_SECTIONS_SCANNED)

    signals: list[str] = []
    if ignored:
        signals.append("ignored_page_or_line_numbers")
    if mashed:
        signals.append("headings_above_stored_number")
    if few:
        signals.append("few_sections_scanned_or_no_text_layer")

    if mashed:
        verdict, action, reason = VERDICT_MISMATCH, ACTION_QUARANTINE, REASON_HEADINGS_ABOVE_STORED
    elif few:
        verdict, action, reason = VERDICT_SUSPECT, ACTION_NEEDS_REEXTRACT, REASON_FEW_SECTIONS_SCANNED
    else:
        verdict, action, reason = VERDICT_MATCH, ACTION_NONE, ""

    return OcrSectionMashVerdict(
        verdict=verdict,
        action=action,
        reason=reason,
        reasons=tuple(reasons),
        signals=tuple(signals),
        stored_count=usable,
        stored_max=stored_max,
        embedded_max=embedded_max,
        embedded_headings=unique,
    )
