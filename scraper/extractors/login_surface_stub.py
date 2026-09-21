"""Detect PakistanLawSite login-surface stub pages that must never promote."""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

_CASE_CONTENT_RE = re.compile(r"Citation\s*Name\s*:", re.IGNORECASE)


def is_login_surface_stub(
    *,
    source_url: Optional[str] = None,
    raw_text: Optional[str] = None,
    raw_html: Optional[str] = None,
    reconciled: Optional[Dict[str, Any]] = None,
    court: Optional[str] = None,
    judge_names: Any = None,
) -> bool:
    data = reconciled or {}
    blob_text = f"{raw_text or ''}\n{raw_html or ''}"
    has_case = bool(_CASE_CONTENT_RE.search(blob_text))
    url = (source_url or "").lower()
    # /login/check is the post-redirect host for real PLS case pages when session is good.
    if "/login/check" in url and not has_case:
        return True
    court_val = str(court or data.get("court") or data.get("court_canonical") or "").strip()
    if court_val.lower() == "read":
        return True
    names = judge_names if judge_names is not None else data.get("judge_names")
    name_blob = str(names or "").lower()
    if "obtaining subscription" in name_blob or "update subscriber" in name_blob:
        return True
    if not has_case:
        blob_parts = [
            source_url or "",
            raw_text or "",
            (raw_html or "")[:12000],
            str(names or ""),
            court_val,
            str(data.get("case_title") or ""),
        ]
        blob = " ".join(blob_parts).lower()
        if "obtaining subscription" in blob:
            return True
        if "update subscriber" in blob:
            return True
        if "i agree with the terms" in blob and ("subscriber" in blob or "login" in blob):
            return True
    return False
