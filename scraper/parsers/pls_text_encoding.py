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
    if all(_line_is_mojibake_or_urdu_only(ln) for ln in tail):
        return "headnote"
    return "body"


def classify_mojibake(text: Optional[str]) -> MojibakeKind:
    """Auditor 2026-10-18: 6,708 rows have ???? runs; in-sample they were only the Urdu headnote after the signature.

    Re-fetch only when the English body is damaged. Headnote-only damage is strip/flag, not a new fetch.
    Without a signature line, ???? is headnote-only when every damaged line sits in a trailing block that is
    mostly question marks or Urdu; Latin text plus ???? elsewhere is body damage.
    """
    if not has_mojibake_run(text):
        return "none"
    lines = (text or "").replace("\r\n", "\n").split("\n")
    sig = _signature_line_index(lines)
    if sig < 0:
        return _classify_without_signature(lines)
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
        # No signature: strip trailing mojibake-only / Urdu-only lines only.
        cut = len(lines)
        while cut > 0 and _line_is_mojibake_or_urdu_only(lines[cut - 1]):
            cut -= 1
        return "\n".join(lines[:cut]).rstrip() + ("\n" if text.endswith("\n") else "")
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
