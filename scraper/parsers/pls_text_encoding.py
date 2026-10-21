"""Detect Urdu mojibake (???? runs) in PakistanLawSite judgment text."""

from __future__ import annotations

import re
from typing import Literal, Optional

_MOJIBAKE_RUN = re.compile(r"\?{4,}")
_SIGNATURE_LINE = re.compile(
    r"(?i)^\s*(?:\(sd/?-?\)|sd/?-?|announced\.?|pronounced\.?|j\.|judge\.|"
    r".{0,60}\b(?:chief\s+justice)\s*\.?)\s*$"
)
_LATIN_WORD = re.compile(r"[A-Za-z]{3,}")
_URDU_CHAR = re.compile(r"[\u0600-\u06FF]")
AUDITOR_2026_10_18_MOJIBAKE_ROWS = 6708

MojibakeKind = Literal["none", "headnote", "body"]


def has_mojibake_run(text: Optional[str], *, min_run: int = 4) -> bool:
    if not text:
        return False
    return bool(re.search(r"\?{" + str(min_run) + r",}", text))


def _signature_line_index(lines: list[str]) -> int:
    last = -1
    for i, line in enumerate(lines):
        if _SIGNATURE_LINE.match(line.strip()):
            last = i
    return last


def _line_is_mojibake_or_urdu_only(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return True
    if has_mojibake_run(stripped):
        return len(re.sub(r"[?\s]", "", stripped)) == 0 or bool(_URDU_CHAR.search(stripped))
    if _URDU_CHAR.search(stripped) and not _LATIN_WORD.search(stripped):
        return True
    return False


def _line_latin_plus_mojibake(line: str) -> bool:
    return has_mojibake_run(line) and len(_LATIN_WORD.findall(line)) >= 3


def _line_is_strippable_headnote_tail(line: str) -> bool:
    """Per-line rule shared by classify (headnote tail) and strip: drop mojibake unless Latin body damage."""
    if _line_latin_plus_mojibake(line):
        return False
    if has_mojibake_run(line):
        words = _LATIN_WORD.findall(line)
        if any(len(w) >= 7 for w in words):
            return False
        if len(re.sub(r"[?\s]", "", line)) > 15:
            return False
        return True
    return _line_is_mojibake_or_urdu_only(line)


def _classify_without_signature(lines: list[str]) -> MojibakeKind:
    mojibake_idxs = [i for i, ln in enumerate(lines) if has_mojibake_run(ln)]
    if not mojibake_idxs:
        return "none"
    last_english = -1
    for i, ln in enumerate(lines):
        if has_mojibake_run(ln):
            continue
        if _LATIN_WORD.search(ln):
            last_english = i
    if any(i <= last_english for i in mojibake_idxs):
        return "body"
    tail = lines[last_english + 1 :]
    if not tail:
        return "body"
    if all(_line_is_strippable_headnote_tail(ln) for ln in tail):
        return "headnote"
    return "body"


def classify_mojibake(text: Optional[str]) -> MojibakeKind:
    if not has_mojibake_run(text):
        return "none"
    lines = (text or "").replace("\r\n", "\n").split("\n")
    sig = _signature_line_index(lines)
    if sig < 0:
        return _classify_without_signature(lines)
    before = lines[: sig + 1]
    after = lines[sig + 1 :]
    if any(_line_latin_plus_mojibake(ln) for ln in before):
        return "body"
    if has_mojibake_run("\n".join(before)):
        return "body"
    for ln in after:
        if _line_latin_plus_mojibake(ln):
            return "body"
    if any(has_mojibake_run(ln) for ln in after):
        non_empty = [ln for ln in after if ln.strip()]
        if non_empty and all(_line_is_strippable_headnote_tail(ln) for ln in non_empty):
            return "headnote"
        return "body"
    return "body"


def strip_mojibake_headnote_lines(text: Optional[str]) -> str:
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    sig = _signature_line_index(lines)
    if sig < 0:
        cut = len(lines)
        while cut > 0 and _line_is_strippable_headnote_tail(lines[cut - 1]):
            cut -= 1
        return "\n".join(lines[:cut]).rstrip() + ("\n" if text.endswith("\n") else "")
    kept = lines[: sig + 1]
    for line in lines[sig + 1 :]:
        if _line_is_strippable_headnote_tail(line):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip() + ("\n" if text.endswith("\n") else "")
