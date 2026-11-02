"""Detect PakistanLawSite case-detail bodies that are site failure pages, not judgments.

The harvest path calls this on the case-detail body it is about to stage. A hit is a
fetch failure (Error Code:700 and the same short shells), not a headnote or a
low-confidence judgment. Real orders, including short ones, are left to the
existing gates.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Optional

# Staging sample, 41 chars: "Error Code:700 Sorry Something went wrong".
_ERROR_CODE_RE = re.compile(r"(?i)error\s*code\s*:?\s*(\d+)\b")
_SORRY_RE = re.compile(r"(?i)sorry,?\s+something went wrong")
_SHELL_RE = re.compile(r"(?i)(?:service unavailable|access denied|session (?:has )?expired)")
_SCRIPT_RE = re.compile(r"(?is)<script[^>]*>.*?</script>|<style[^>]*>.*?</style>")
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

# Text that remains after the failure signature is removed. A 700 page may keep
# a short title line. Other shells ("service unavailable", "access denied")
# count only when they are essentially the whole body, so a short order that
# uses those words is left for the judgment gates.
_MAX_700_LEFTOVER_CHARS = 80
_MAX_SHELL_LEFTOVER_CHARS = 16

FETCH_ERROR_700 = "fetch_error: pls_error_code_700"
FETCH_ERROR_PAGE = "fetch_error: pls_error_page"


def _visible(body: Optional[str]) -> str:
    raw = html_lib.unescape(body or "")
    raw = raw.replace("\xa0", " ").replace("\u200b", "").replace("\ufeff", "")
    raw = _SCRIPT_RE.sub(" ", raw)
    raw = _TAG_RE.sub(" ", raw)
    return _WS_RE.sub(" ", raw).strip()


def _leftover(visible: str) -> str:
    cleaned = _ERROR_CODE_RE.sub(" ", visible)
    cleaned = _SORRY_RE.sub(" ", cleaned)
    cleaned = _SHELL_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", cleaned, flags=re.UNICODE)
    return _WS_RE.sub(" ", cleaned).strip()


def detect_pls_case_detail_failure(body: Optional[str]) -> Optional[str]:
    """Return a fetch_error reason when `body` is a PLS failure page, else None."""
    visible = _visible(body)
    if not visible:
        return None
    code = _ERROR_CODE_RE.search(visible)
    sorry = _SORRY_RE.search(visible)
    shell = _SHELL_RE.search(visible)
    if code is None and sorry is None and shell is None:
        return None
    leftover = len(_leftover(visible))
    is_700 = (code is not None and code.group(1) == "700") or sorry is not None
    if is_700:
        if leftover > _MAX_700_LEFTOVER_CHARS:
            return None
        return FETCH_ERROR_700
    if leftover > _MAX_SHELL_LEFTOVER_CHARS:
        return None
    return FETCH_ERROR_PAGE
