"""Detect Urdu mojibake (???? runs) and attempt conservative re-decode of raw bytes."""

from __future__ import annotations

import re
from typing import Optional, Tuple

_MOJIBAKE_RUN = re.compile(r"\?{4,}")
_CANDIDATE_ENCODINGS = ("utf-8", "cp1256", "windows-1256", "iso-8859-1")


def has_mojibake_run(text: Optional[str], *, min_run: int = 4) -> bool:
    if not text:
        return False
    return bool(re.search(r"\?{" + str(min_run) + r",}", text))


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
