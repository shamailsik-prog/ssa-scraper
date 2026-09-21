"""Fail-closed guards for judgment login/subscription stubs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class JudgmentGuardSignal:
    reason_code: str
    signal: str
    matched_value: str


# PLS parks authenticated case HTML on /login/check after ReferenceCaseLawSearch.
# URL alone is NOT a stub when the body carries real case content.
_URL_LOGIN_STUB_RE = re.compile(r"/login/check(?:[/?#]|$)", re.IGNORECASE)
_CASE_CONTENT_RE = re.compile(r"Citation\s*Name\s*:", re.IGNORECASE)
_SUBSCRIPTION_CHROME_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("obtaining_subscription", re.compile(r"\bobtaining\s+subscription\b", re.IGNORECASE)),
    ("update_subscriber", re.compile(r"\bupdate\s+subscriber\b", re.IGNORECASE)),
    ("update_subscription", re.compile(r"\bupdate\s+subscription\b", re.IGNORECASE)),
    ("subscription_account", re.compile(r"\bsubscriber\s+account\b", re.IGNORECASE)),
)


def _has_case_content(*, raw_text: Optional[str], raw_html: Optional[str]) -> bool:
    blob = f"{raw_text or ''}\n{raw_html or ''}"
    return bool(_CASE_CONTENT_RE.search(blob))


def _match_subscription_chrome(value: str) -> Optional[str]:
    for marker_name, marker_re in _SUBSCRIPTION_CHROME_MARKERS:
        if marker_re.search(value):
            return marker_name
    return None


def _judge_name_values(judge_names: Optional[Iterable[Any]]) -> list[str]:
    if judge_names is None:
        return []
    if isinstance(judge_names, str):
        return [judge_names]
    out: list[str] = []
    for item in judge_names:
        if item is None:
            continue
        out.append(str(item))
    return out


def detect_judgment_stub(
    *,
    source_url: Optional[str],
    raw_text: Optional[str],
    raw_html: Optional[str],
    judge_names: Optional[Iterable[Any]],
) -> Optional[JudgmentGuardSignal]:
    has_case = _has_case_content(raw_text=raw_text, raw_html=raw_html)
    url = source_url or ""
    if _URL_LOGIN_STUB_RE.search(url) and not has_case:
        return JudgmentGuardSignal(
            reason_code="login_stub",
            signal="source_url_login_check",
            matched_value=url[:500],
        )

    for judge_name in _judge_name_values(judge_names):
        marker = _match_subscription_chrome(judge_name)
        if marker:
            return JudgmentGuardSignal(
                reason_code="subscription_chrome",
                signal=f"judge_name_{marker}",
                matched_value=judge_name[:500],
            )
        if judge_name.strip().lower() == "read":
            return JudgmentGuardSignal(
                reason_code="login_stub",
                signal="judge_name_read_button",
                matched_value=judge_name[:500],
            )

    # Site chrome always includes Update Subscriber modal + FAQ "obtaining subscription".
    # Only treat those markers as stubs when the page has no case body.
    if not has_case:
        for field_name, value in (("raw_text", raw_text or ""), ("raw_html", raw_html or "")):
            marker = _match_subscription_chrome(value)
            if marker:
                return JudgmentGuardSignal(
                    reason_code="subscription_chrome",
                    signal=f"{field_name}_{marker}",
                    matched_value=value[:500],
                )
    return None


def guard_reason(signal: JudgmentGuardSignal) -> str:
    return f"{signal.reason_code}: {signal.signal}"
