"""Isolate one act inside a gazette PDF that prints several ACT No. headings.

Senate uploads, and the same layout on law.gok.pk or the National Assembly,
sometimes bind three Acts into one PDF. ``split_into_sections`` numbers every
``1.`` / ``2.`` line in that file, so the middle act keeps its neighbours'
sections and loses its own where the numbers collide. This module cuts the
text on ``ACT No.`` boundaries and keeps the segment that matches the pipe
label or the target title.

A pipe label and a title that name two different segments are unresolved.
Unresolved text is not split: storing a mixture is worse than storing nothing.

An ``ACT No.`` glued into the middle of a section is a boundary when ``An Act``
follows it, including when that next act's section 1 was not read. A citation
tail (``Act No. ….- In the``) stays with the amending act.

A scanned heading still counts when ``ACT`` is ``AC_l`` or ``{-CT``, ``of`` is
``on``, ``or``, or missing, ``T`` in the roman numeral is ``I``,
or the preamble is ``.4 Act``. ``Acr No. xt`` is not a heading. See
``docs/BUNDLED_GAZETTE_ACTS.md``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlsplit

from scraper.statute_quality.senate_integrity import (
    _ACT_NO_HEADING_RE,
    _PDF_WORD_RE,
    _is_act_boundary,
    _years,
    act_segments,
    called_titles_in,
    is_coherent_act_title,
    is_gazette_non_act_title,
    name_s1_mismatch,
    parse_act_number,
    section_embeds_next_act,
    split_statute_source_url,
    title_relation,
)

REASON_BLEED = "bundled_gazette_act_bleed"
REASON_WRONG_SEGMENT = "bundled_gazette_act_wrong_segment"

VERDICT_MATCH = "MATCH"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_NOT_BUNDLED = "NOT_BUNDLED"
VERDICT_FETCH_FAIL = "FETCH_FAIL"
VERDICT_SKIPPED_EMPTY = "SKIPPED_EMPTY"

ACTION_NONE = "none"
ACTION_QUARANTINE = "quarantine"

_WINDOW = 80
_FOLD_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class BundleIsolation:
    status: str
    text: str
    act_label: Optional[str] = None
    title: Optional[str] = None
    act_year: Optional[int] = None
    segment_count: int = 0
    # The URL names an act number and that number is not a heading in this file.
    marker_absent: bool = False


@dataclass(frozen=True)
class BundledGazetteVerdict:
    verdict: str
    action: str
    reason: Optional[str]
    signals: tuple[str, ...]
    segment_count: int = 0


def _fold(text: str) -> str:
    return _FOLD_RE.sub(" ", (text or "").lower()).strip()


def _title_matches(segments: Sequence, name: str) -> list:
    if not name or not is_coherent_act_title(name):
        return []
    found = []
    for segment in segments:
        if segment.title and title_relation(name, segment.title) == "same":
            found.append(segment)
    return found


def select_target_segment(segments: Sequence, *, target_name: str, pipe_label: Optional[str]):
    """The one segment this statute is. None when the bundle does not say."""
    if len(segments) < 2:
        return None
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    numbered = [segment for segment in segments if pipe_act and segment.act_number == pipe_act]
    if len(numbered) > 1:
        clean = [segment for segment in numbered if segment.number_clean]
        # A collapsed ``)O(`` can repair to the same number as the real heading.
        numbered = clean if len(clean) == 1 else []
    titled = _title_matches(segments, target_name)
    if len(numbered) == 1 and len(titled) == 1 and numbered[0] is not titled[0]:
        return None
    if len(numbered) == 1:
        return numbered[0]
    if len(numbered) > 1:
        return None
    if len(titled) == 1:
        return titled[0]
    if pipe_label and pipe_act is None:
        labeled = _title_matches(segments, pipe_label)
        if len(labeled) == 1:
            return labeled[0]
    return None


def _act_label(segment, pipe_label: Optional[str]) -> str:
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    if pipe_label and pipe_act and segment.act_number == pipe_act:
        return pipe_label.strip()
    if segment.act_number:
        number, year = segment.act_number
        return f"{number} of {year}"
    return (segment.title or "").strip() or "matched"


def isolate_bundled_act(text: str, *, target_name: str = "", source_url: str = "") -> BundleIsolation:
    """Return the target act's text when the document prints two or more ACT No. lines.

    ``single`` leaves the text unchanged. ``isolated`` is one matching segment.
    ``unresolved`` means the segments disagree or none match; ``text`` is empty
    so a caller does not split the mixture.
    """
    raw = text or ""
    _fetch_url, pipe_label = split_statute_source_url(source_url)
    if pipe_label and is_nav_chrome_text(pipe_label):
        # ``|Share Print … Filter by`` is page chrome, not an act to select.
        pipe_label = None
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    segments = act_segments(raw, pipe_act=pipe_act)
    if pipe_act and not any(segment.act_number == pipe_act for segment in segments):
        # The file at this URL is not the marked Act. Do not emit a different one.
        return BundleIsolation(
            status="unresolved",
            text="",
            segment_count=len(segments),
            marker_absent=True,
        )
    if len(segments) < 2:
        return BundleIsolation(status="single", text=raw, segment_count=len(segments))
    chosen = select_target_segment(segments, target_name=target_name or "", pipe_label=pipe_label)
    if chosen is None:
        return BundleIsolation(status="unresolved", text="", segment_count=len(segments))
    year = chosen.act_number[1] if chosen.act_number else None
    return BundleIsolation(
        status="isolated",
        text=chosen.text,
        act_label=_act_label(chosen, pipe_label),
        title=chosen.title,
        act_year=year,
        segment_count=len(segments),
    )


def _windows(text: str) -> list[str]:
    folded = _fold(text)
    if len(folded) < _WINDOW:
        return []
    windows = [folded[:_WINDOW]]
    if len(folded) >= _WINDOW * 2:
        middle = (len(folded) - _WINDOW) // 2
        windows.append(folded[middle : middle + _WINDOW])
        windows.append(folded[-_WINDOW:])
    unique: list[str] = []
    for window in windows:
        if window not in unique:
            unique.append(window)
    return unique


def _segment_signal(segment, index: int) -> str:
    if segment.act_number:
        number, year = segment.act_number
        return f"segment:{number}:{year}"
    return f"segment:{index}"


def classify_bundled_gazette(
    *,
    stored_name: str,
    source_url: Optional[str],
    sections: Sequence[dict],
    pdf_text: Optional[str],
    pdf_available: bool,
) -> BundledGazetteVerdict:
    """Say whether stored sections were taken from more than one act in the PDF.

    A PDF with fewer than two ``ACT No.`` headings is ``NOT_BUNDLED``. Sections
    that sit in two segments are bleed. Sections that all sit in a different
    segment from the pipe label or stored title are a wrong-segment mismatch.
    Short section text is ignored so a footer or a citation cannot force a hit.
    """
    if not pdf_available:
        return BundledGazetteVerdict(VERDICT_FETCH_FAIL, ACTION_NONE, None, ("pdf_unavailable",), 0)
    _fetch_url, pipe_label = split_statute_source_url(source_url)
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    segments = act_segments(pdf_text or "", pipe_act=pipe_act)
    if len(segments) < 2:
        return BundledGazetteVerdict(VERDICT_NOT_BUNDLED, ACTION_NONE, None, ("not_bundled",), len(segments))
    folded_segments = [_fold(segment.text) for segment in segments]
    located: set[int] = set()
    for section in sections or []:
        for window in _windows(str(section.get("section_text") or "")):
            hits = [index for index, folded in enumerate(folded_segments) if window in folded]
            if len(hits) == 1:
                located.add(hits[0])
    chosen = select_target_segment(segments, target_name=stored_name or "", pipe_label=pipe_label)
    target_index = next((index for index, segment in enumerate(segments) if segment is chosen), None)
    signals = tuple(_segment_signal(segments[index], index) for index in sorted(located))
    if len(located) >= 2:
        return BundledGazetteVerdict(
            VERDICT_MISMATCH,
            ACTION_QUARANTINE,
            REASON_BLEED,
            signals + ("spans_acts",),
            len(segments),
        )
    if len(located) == 1 and target_index is not None and target_index not in located:
        return BundledGazetteVerdict(
            VERDICT_MISMATCH,
            ACTION_QUARANTINE,
            REASON_WRONG_SEGMENT,
            signals + ("sections_outside_target",),
            len(segments),
        )
    extra: tuple[str, ...] = ()
    if chosen is None:
        extra = ("target_unresolved",)
    return BundledGazetteVerdict(VERDICT_MATCH, ACTION_NONE, None, signals + extra, len(segments))


_GAZETTE_HOSTS = frozenset(
    {
        "senate.gov.pk",
        "www.senate.gov.pk",
        "na.gov.pk",
        "www.na.gov.pk",
        "pcp.gov.pk",
        "www.pcp.gov.pk",
    }
)
_GAZETTE_SOURCE_NAMES = frozenset({"Senate", "NationalAssembly", "GazetteOfPakistan"})
_NAV_CHROME_RE = re.compile(r"(?i)share\s+print\b[\s\S]{0,80}\bfilter\s+by\b")
# Printer's price line and the ``[2940 Ex. Gaz.]`` imprint. OCR drops the i in Price.
_GAZETTE_IMPRINT_RE = re.compile(
    r"(?i)(?:\bpr[il1]ce\s+(?:ps|rs)\.?\s*\d|\[[^\]]{0,24}\bex\.?\s*gaz\b|\bex\.?\s*gaz\b)"
)


def is_nav_chrome_text(text: str) -> bool:
    """True for the National Assembly listing suffix ``Share Print … Filter by``."""
    return bool(_NAV_CHROME_RE.search(text or ""))


def is_federal_gazette_source(source_url: Optional[str], source_name: Optional[str] = None) -> bool:
    fetch_url, _label = split_statute_source_url(source_url)
    host = ""
    if fetch_url:
        try:
            host = (urlsplit(fetch_url).hostname or "").lower()
        except ValueError:
            host = ""
    if host in _GAZETTE_HOSTS:
        return True
    return (source_name or "") in _GAZETTE_SOURCE_NAMES


def clip_section_text(text: str) -> str:
    """Cut a section at the next Act header or drop a gazette price footer.

    A citation tail (``Act No. ….- In the``) is not a header. The words before
    the header have to be a real section, the same test ``section_embeds_next_act``
    uses, so a section that opens with its own ``ACT No.`` line is left intact.
    """
    raw = text or ""
    cut = len(raw)
    for match in _ACT_NO_HEADING_RE.finditer(raw):
        if not _is_act_boundary(raw, match):
            continue
        if len(_PDF_WORD_RE.findall(raw[: match.start()])) >= 8:
            cut = match.start()
            break
    kept: list[str] = []
    for line in raw[:cut].splitlines():
        if _GAZETTE_IMPRINT_RE.search(line) and len(_PDF_WORD_RE.findall(line)) < 14:
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def strip_gazette_imprint_lines(text: str) -> str:
    """Remove price / Ex. Gaz. lines from a gazette page without joining Acts."""
    kept: list[str] = []
    for line in (text or "").splitlines():
        if _GAZETTE_IMPRINT_RE.search(line) and len(_PDF_WORD_RE.findall(line)) < 14:
            continue
        kept.append(line)
    return "\n".join(kept)


def section_is_footer_or_chrome_title(section_number: str, section_text: str) -> bool:
    """True when the stored section is a price footer or a gazette running title."""
    from scraper.parsers.statute_parser import _strip_section_prefix

    body = _strip_section_prefix(section_text or "")
    flat = re.sub(r"\s+", " ", body).strip(" .")
    if not flat:
        return True
    if re.fullmatch(r"(?i)(?:the\s+)?following\s+act", flat):
        return True
    if is_gazette_non_act_title(flat):
        return True
    if _GAZETTE_IMPRINT_RE.search(body):
        residue = _GAZETTE_IMPRINT_RE.sub(" ", body)
        residue = re.sub(r"(?i)\b(?:gazette\s+of\s+pakistan|extraordinary|part)\b", " ", residue)
        residue = re.sub(r"\d+", " ", residue)
        if len(_PDF_WORD_RE.findall(residue)) < 8:
            return True
    _ = section_number
    return False


def prepare_gazette_sections(sections: Sequence[dict]) -> list[dict]:
    """Drop bleed past the next Act and sections that are only a footer or a title."""
    prepared: list[dict] = []
    for section in sections or []:
        if not isinstance(section, dict):
            continue
        text = clip_section_text(str(section.get("section_text") or ""))
        number = str(section.get("section_number") or "")
        if not text or section_is_footer_or_chrome_title(number, text):
            continue
        updated = dict(section)
        updated["section_text"] = text
        prepared.append(updated)
    return prepared


def _marker_s1_disagrees(statute_name: str, source_url: Optional[str], sections: Sequence[dict]) -> bool:
    """Section 1's short title is not the Act named by the ``|XXVII of 1973`` marker."""
    _fetch, pipe_label = split_statute_source_url(source_url)
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    if not pipe_act:
        return False
    if name_s1_mismatch(statute_name, sections):
        return True
    marker_year = pipe_act[1]
    for section in sections or []:
        if str(section.get("section_number") or "").strip() not in {"1", "1."}:
            continue
        blob = "\n".join(
            part
            for part in (str(section.get("section_title") or ""), str(section.get("section_text") or ""))
            if part
        )
        titles = called_titles_in(blob)
        if not titles:
            continue
        for title in titles:
            years = _years(title)
            if years and marker_year not in years:
                return True
    return False


def gazette_bundle_hold_reason(
    *,
    statute_name: str,
    year_enacted: Optional[int],
    jurisdiction: Optional[str],
    source_url: Optional[str],
    source_name: Optional[str],
    sections: Sequence[dict],
    year_enacted_supplied: bool = False,
) -> Optional[str]:
    """Reject a senate/NA gazette extract that is chrome, yearless, or still bleeding.

    Returns a reason code, or None when this record is not that case. Callers
    hold the row and do not write it. A missing ``year_enacted`` key is not a
    null year: only a payload that sets the field to null is.
    """
    if not is_federal_gazette_source(source_url, source_name):
        return None
    _fetch, pipe_label = split_statute_source_url(source_url)
    if (
        is_gazette_non_act_title(statute_name)
        or is_nav_chrome_text(statute_name)
        or is_nav_chrome_text(pipe_label or "")
    ):
        return "gazette_chrome_title"
    jurisdiction_flat = (jurisdiction or "").strip().lower()
    federal = jurisdiction_flat in {"", "federal"}
    if federal and year_enacted_supplied and year_enacted is None:
        return "federal_null_year"
    if _marker_s1_disagrees(statute_name, source_url, sections):
        return "marker_s1_mismatch"
    if any(section_embeds_next_act(str(section.get("section_text") or "")) for section in sections or []):
        return "bundled_gazette_act_bleed"
    if any(
        section_is_footer_or_chrome_title(
            str(section.get("section_number") or ""),
            str(section.get("section_text") or ""),
        )
        for section in sections or []
    ):
        return "gazette_footer_or_title_as_section"
    return None
