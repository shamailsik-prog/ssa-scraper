"""Fail-closed guards for judgment login/subscription stubs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from scraper.parsers.bench_parser import normalise_judge_name


@dataclass(frozen=True)
class JudgmentGuardSignal:
    reason_code: str
    signal: str
    matched_value: str


# PLS parks authenticated case HTML on /login/check after ReferenceCaseLawSearch.
# URL alone is NOT a stub when the body carries real case content.
_URL_LOGIN_STUB_RE = re.compile(r"/login/check(?:[/?#]|$)", re.IGNORECASE)
_CASE_CONTENT_RE = re.compile(r"Citation\s*Name\s*:", re.IGNORECASE)
_NOTES_ON_CASES_RE = re.compile(r"\bnotes?\s+on\s+cases?\b", re.IGNORECASE)
_JUDGMENT_STRUCTURE_RE = re.compile(
    r"(?i)\b(judgment|judgement|decided on|coram|before|versus|vs\.?|v\.)\b"
)
_BEFORE_JJ_LINE_RE = re.compile(r"(?im)^\s*before\s*[:\-]?\s*(.{5,300}?)\s*$")
_JJ_SUFFIX_RE = re.compile(r"(?i),?\s*(?:j\.?|jj\.?|c\.?j\.?|cj)\s*$")
_NAME_SPLIT_RE = re.compile(r"\s*(?:,|;|\band\b|&)\s*", re.IGNORECASE)
_SUBSCRIPTION_CHROME_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("obtaining_subscription", re.compile(r"\bobtaining\s+subscription\b", re.IGNORECASE)),
    ("update_subscriber", re.compile(r"\bupdate\s+subscriber\b", re.IGNORECASE)),
    ("update_subscription", re.compile(r"\bupdate\s+subscription\b", re.IGNORECASE)),
    ("subscription_account", re.compile(r"\bsubscriber\s+account\b", re.IGNORECASE)),
)
_MODAL_CHROME_LINE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[\u00d7x]+\s*$", re.IGNORECASE),
    re.compile(r"^\s*case\s+description\s*$", re.IGNORECASE),
    re.compile(r"^\s*bookmark\s+this\s+case\s*$", re.IGNORECASE),
    re.compile(r"^\s*update\s+subscriber\b.*$", re.IGNORECASE),
    re.compile(r"^\s*obtaining\s+subscription\b.*$", re.IGNORECASE),
)
_MODAL_CHROME_INLINE_PREFIX_RE = re.compile(
    r"(?is)^\s*(?:\u00d7+\s*)?(?:case\s+description\s*)?(?:bookmark\s+this\s+case\s*)?"
)
_JUDGMENT_CONTENT_ANCHORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^citation\s*name\s*:"),
    re.compile(r"(?im)^\s*(?:before|coram)\b"),
    re.compile(r"(?im)^\s*in\s+the\s+[A-Z][^\n]{0,120}\bcourt\b"),
    re.compile(r"(?im)^[^\n]{0,220}\b(?:versus|vs\.?|v\.)\b"),
)


def _looks_like_modal_chrome_prefix(prefix: str) -> bool:
    normalized = re.sub(r"\s+", " ", (prefix or "").strip().lower())
    if not normalized:
        return False
    return (
        "case description" in normalized
        or "bookmark this case" in normalized
        or         "update subscriber" in normalized
        or "obtaining subscription" in normalized
        or normalized.startswith("\u00d7")
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


def strip_leading_judgment_chrome(raw_text: Optional[str]) -> str:
    """Remove known PakistanLawSite modal chrome prefixes while preserving body text."""
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    if not text.strip():
        return ""
    lines = text.split("\n")
    idx = 0
    while idx < len(lines):
        candidate = lines[idx].strip()
        if not candidate:
            idx += 1
            continue
        if any(marker.match(candidate) for marker in _MODAL_CHROME_LINE_MARKERS):
            idx += 1
            continue
        break
    stripped = "\n".join(lines[idx:]).strip()
    if not stripped:
        return ""
    previous = None
    while previous != stripped:
        previous = stripped
        stripped = _MODAL_CHROME_INLINE_PREFIX_RE.sub("", stripped).lstrip(" :-|\n\t")
    first_anchor = None
    for anchor in _JUDGMENT_CONTENT_ANCHORS:
        match = anchor.search(stripped)
        if match is None:
            continue
        if first_anchor is None or match.start() < first_anchor.start():
            first_anchor = match
    if first_anchor is not None and first_anchor.start() > 0:
        prefix = stripped[: first_anchor.start()]
        if len(prefix) <= 800 and _looks_like_modal_chrome_prefix(prefix):
            stripped = stripped[first_anchor.start() :].lstrip()
    return stripped


def extract_before_jj_judge_names(raw_text: Optional[str]) -> list[str]:
    text = raw_text or ""
    if not text:
        return []
    names: list[str] = []
    seen = set()
    for match in _BEFORE_JJ_LINE_RE.finditer(text[:30000]):
        clause = match.group(1).strip()
        if not re.search(r"(?i)\bjj?\b", clause):
            continue
        clause = _JJ_SUFFIX_RE.sub("", clause).strip(" ,.;:-")
        for piece in _NAME_SPLIT_RE.split(clause):
            normalized = normalise_judge_name(piece)
            if not normalized:
                continue
            key = normalized.lower()
            if key in seen:
                continue
            seen.add(key)
            names.append(normalized)
    return names


def detect_headnotes_only(
    *,
    raw_text: Optional[str],
    raw_html: Optional[str] = None,
    min_full_text_chars: int = 2500,
) -> Optional[JudgmentGuardSignal]:
    blob = (raw_text or "").strip()
    if not blob:
        return JudgmentGuardSignal(
            reason_code="headnote_only",
            signal="body_missing",
            matched_value="",
        )
    if _NOTES_ON_CASES_RE.search(blob):
        jj_names = extract_before_jj_judge_names(blob)
        if not jj_names:
            return JudgmentGuardSignal(
                reason_code="headnote_only",
                signal="notes_on_cases_only",
                matched_value=blob[:500],
            )
    compact_len = len(re.sub(r"\s+", "", blob))
    if compact_len < max(200, min_full_text_chars // 2) and not _JUDGMENT_STRUCTURE_RE.search(blob):
        return JudgmentGuardSignal(
            reason_code="headnote_only",
            signal="body_short_non_judgment",
            matched_value=blob[:500],
        )
    html_blob = raw_html or ""
    if compact_len < min_full_text_chars and _NOTES_ON_CASES_RE.search(html_blob) and not _JUDGMENT_STRUCTURE_RE.search(blob):
        return JudgmentGuardSignal(
            reason_code="headnote_only",
            signal="body_short_with_notes",
            matched_value=blob[:500],
        )
    return None


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
