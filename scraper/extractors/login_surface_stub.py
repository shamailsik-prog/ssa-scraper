"""Detect PakistanLawSite login-surface stub pages that must never promote."""
from __future__ import annotations

from typing import Any, Dict, Optional

from scraper.extractors.judgment_guards import (
    _has_case_content,
    login_subscription_chrome_dominates,
)


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
    has_case = _has_case_content(raw_text=raw_text, raw_html=raw_html)
    url = (source_url or "").lower()
    # /login/check is the post-redirect host for real PLS case pages when session is good.
    # Empty Citation Name chrome is not case content (#100). Structured CLC-like
    # bodies without that label still count as case content.
    if "/login/check" in url and not has_case:
        return True
    court_val = str(court or data.get("court") or data.get("court_canonical") or "").strip()
    if court_val.lower() == "read":
        return True
    names = judge_names if judge_names is not None else data.get("judge_names")
    name_blob = " ".join(
        str(part)
        for part in (names, data.get("judge"), data.get("judges"))
        if part is not None
    ).lower()
    # Structured login-surface fields dominate regardless of body length.
    if "obtaining subscription" in name_blob or "update subscriber" in name_blob:
        return True
    # Body chrome: fail-closed only when markers dominate. Incidental
    # nav/footer/cookie crumbs on a long real judgment are not a stub.
    return login_subscription_chrome_dominates(raw_text=raw_text, raw_html=raw_html) is not None
