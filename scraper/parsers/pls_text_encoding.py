"""Detect Urdu mojibake (???? runs) and attempt conservative re-decode of raw bytes."""

from __future__ import annotations

import re
from typing import Literal, Optional, Tuple

_MOJIBAKE_RUN = re.compile(r"\?{4,}")
_CANDIDATE_ENCODINGS = ("utf-8", "cp1256", "windows-1256", "iso-8859-1")
# Closing signature / announcement only — not a "Before X, J" bench caption.
_SIGNATURE_LINE = re.compile(
    r"(?i)^\s*(?:\(sd/?-?\)|sd/?-?|announced\.?|pronounced\.?|j\.|judge\.|"
    r".{0,60}\b(?:chief\s+justice)\s*\.?)\s*$"
)
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


def classify_mojibake(text: Optional[str]) -> MojibakeKind:
    """Auditor 2026-10-18: 6,708 rows have ???? runs; in-sample they were only the Urdu headnote after the signature.

    Re-fetch only when the English body is damaged. Headnote-only damage is strip/flag, not a new fetch.
    """
    if not has_mojibake_run(text):
        return "none"
    lines = (text or "").replace("\r\n", "\n").split("\n")
    sig = _signature_line_index(lines)
    if sig < 0:
        return "body"
    after = "\n".join(lines[sig + 1 :])
    before = "\n".join(lines[: sig + 1])
    if has_mojibake_run(before):
        return "body"
    if has_mojibake_run(after):
        return "headnote"
    return "body"


def strip_mojibake_headnote_lines(text: Optional[str]) -> str:
    """Drop post-signature lines that are ???? runs. Leaves the English body untouched."""
    if not text:
        return ""
    lines = text.replace("\r\n", "\n").split("\n")
    sig = _signature_line_index(lines)
    if sig < 0:
        return text
    kept = lines[: sig + 1]
    for line in lines[sig + 1 :]:
        if has_mojibake_run(line) and len(re.sub(r"[?\s]", "", line)) == 0:
            continue
        if has_mojibake_run(line):
            continue
        kept.append(line)
    return "\n".join(kept).rstrip() + ("\n" if text.endswith("\n") else "")


def try_redecode_text(raw: bytes) -> Tuple[Optional[str], str]:
    """Return (best_text, encoding_used). Never raises."""
    if not raw:
        return None, "empty"
    best: Optional[str] = None
    best_score = -1
    best_enc = "utf-8"
    for enc in _CANDIDATE_ENCODINGS:
        try:
            decoded = raw.decode(enc)
        except UnicodeDecodeError:
            continue
        score = decoded.count("؟") + decoded.count("ا")  # crude Urdu signal
        penalty = len(_MOJIBAKE_RUN.findall(decoded)) * 10
        score -= penalty
        if score > best_score:
            best_score, best, best_enc = score, decoded, enc
    if best is None:
        best = raw.decode("utf-8", errors="replace")
        best_enc = "utf-8-replace"
    return best, best_enc
