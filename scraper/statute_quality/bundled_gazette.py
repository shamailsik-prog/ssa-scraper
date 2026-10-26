"""Isolate one act inside a gazette PDF that prints several ACT No. headings.

Senate uploads, and the same layout on law.gok.pk or the National Assembly,
sometimes bind three Acts into one PDF. ``split_into_sections`` numbers every
``1.`` / ``2.`` line in that file, so the middle act keeps its neighbours'
sections and loses its own where the numbers collide. This module cuts the
text on ``ACT No.`` boundaries and keeps the segment that matches the pipe
label or the target title.

A pipe label and a title that name two different segments are unresolved.
Unresolved text is not split: storing a mixture is worse than storing nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Sequence

from scraper.statute_quality.senate_integrity import (
    act_segments,
    is_coherent_act_title,
    parse_act_number,
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
    pipe_act = parse_act_number(pipe_label, pipe_label=True) if pipe_label else None
    segments = act_segments(raw, pipe_act=pipe_act)
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
