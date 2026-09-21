"""Fail-closed guards for judgment login/subscription stubs."""

from __future__ import annotations

import html as html_lib
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
# #100 short-circuit: a filled Citation Name value is always case content.
_CASE_CONTENT_RE = re.compile(r"Citation\s*Name\s*:\s*(?:&nbsp;|\s)*[A-Za-z0-9\[\(]", re.IGNORECASE)
# CLC / SCMR / YLR (and similar) reporter bodies often omit the PLS chrome
# label. Line-anchored so mid-sentence words ("plan before continuing") do not count.
_CASE_BODY_CONTENT_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*(?:before|coram)\b"),
    re.compile(r"(?im)^\s*in\s+the\s+[A-Z][^\n]{0,160}\bcourt\b"),
    re.compile(r"(?im)^[^\n]{0,220}\b(?:versus|vs\.?|v\.)\b"),
    re.compile(r"(?im)^\s*(?:judgment|judgement)\b"),
    re.compile(r"(?im)^\s*held\s*[:\-.]"),
)
_HTML_BREAK_RE = re.compile(r"(?i)<br\s*/?>|</(?:p|div|tr|h[1-6]|li|td|th)>")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_NBSP_RE = re.compile(r"&nbsp;|&#160;", re.IGNORECASE)
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
# Cookie / login-form leftovers. These only quarantine when they dominate
# (thin body or high marker share), never as crumbs on a long judgment.
_LOGIN_SURFACE_BODY_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("agree_terms", re.compile(r"\bi\s+agree\s+with\s+the\s+terms\b", re.IGNORECASE)),
)
_PASSWORD_INPUT_RE = re.compile(r"(?i)<input\b[^>]*\btype\s*=\s*['\"]password['\"]")
_SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style|noscript|svg)\b[^>]*>.*?</\1>")

# Dominate-vs-crumbs thresholds for body-level login/subscription chrome.
# Structured fields (judge_names / court == "read") still fail closed on their own.
#
# - Thin visible body + any marker => the page IS the chrome (paywall / login / CTA).
# - Marker characters are a large share of visible text => chrome dominates.
# - Medium unstructured copy + markers => subscription/login CTA as main content.
# - Long structured judgment + a few nav/footer/cookie crumbs => NOT a stub.
_THIN_VISIBLE_COMPACT_CHARS = 400
_MARKER_DOMINANCE_RATIO = 0.25
_MEDIUM_UNSTRUCTURED_COMPACT_CHARS = 1200
_MODAL_CHROME_LINE_MARKERS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\s*[\u00d7x]+\s*$", re.IGNORECASE),
    re.compile(r"^\s*case\s+description\s*$", re.IGNORECASE),
    re.compile(r"^\s*bookmark\s+this\s+case\s*$", re.IGNORECASE),
    re.compile(r"^\s*update\s+subscriber\b.*$", re.IGNORECASE),
    re.compile(r"^\s*obtaining\s+subscription\b.*$", re.IGNORECASE),
    re.compile(r"^\s*citation\s*name\s*:.*$", re.IGNORECASE),
    re.compile(r"^\s*notes?\s+on\s+cases?\s*$", re.IGNORECASE),
)
_MODAL_CHROME_INLINE_PREFIX_RE = re.compile(
    r"(?is)^\s*(?:\u00d7+\s*)?(?:case\s+description\s*)?(?:bookmark\s+this\s+case\s*)?(?:citation\s*name\s*:[^\n]*\n*)?(?:notes?\s+on\s+cases?\s*)?"
)
_JUDGMENT_CONTENT_ANCHORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?im)^\s*(?:before|coram)\b"),
    re.compile(r"(?im)^\s*in\s+the\s+[A-Z][^\n]{0,120}\bcourt\b"),
    re.compile(r"(?im)^[^\n]{0,220}\b(?:versus|vs\.?|v\.)\b"),
    re.compile(r"(?im)^\s*(?:judgment|judgement)\b"),
)


def _looks_like_modal_chrome_prefix(prefix: str) -> bool:
    normalized = re.sub(r"\s+", " ", (prefix or "").strip().lower())
    if not normalized:
        return False
    return (
        "case description" in normalized
        or "bookmark this case" in normalized
        or "update subscriber" in normalized
        or "obtaining subscription" in normalized
        or "citation name:" in normalized
        or "notes on cases" in normalized
        or normalized.startswith("\u00d7")
    )


def _visible_case_blob(*, raw_text: Optional[str], raw_html: Optional[str]) -> str:
    """Flatten HTML enough for line-anchored judgment-body detectors."""
    text = (raw_text or "").replace("\r\n", "\n").replace("\r", "\n")
    html = raw_html or ""
    if html:
        html = _HTML_BREAK_RE.sub("\n", html)
        html = _HTML_TAG_RE.sub(" ", html)
        html = _HTML_NBSP_RE.sub(" ", html)
    return f"{text}\n{html}"


def _has_case_content(*, raw_text: Optional[str], raw_html: Optional[str]) -> bool:
    """True when the payload is a real judgment body, not empty Citation Name chrome.

    A filled `Citation Name:` value still short-circuits (#100). Structured case
    text (parties, court heading, Before/Coram, JUDGMENT, Held) also counts so
    CLC and similar reporter bodies without that chrome label are not treated
    as empty. Never treat length alone or empty Citation Name chrome as case
    content (Auditor fail-closed).
    """
    raw_blob = f"{raw_text or ''}\n{raw_html or ''}"
    if _CASE_CONTENT_RE.search(raw_blob):
        return True
    visible = _visible_case_blob(raw_text=raw_text, raw_html=raw_html)
    return any(pattern.search(visible) for pattern in _CASE_BODY_CONTENT_RES)


def _match_subscription_chrome(value: str) -> Optional[str]:
    for marker_name, marker_re in _SUBSCRIPTION_CHROME_MARKERS:
        if marker_re.search(value):
            return marker_name
    return None


def _compact_len(value: str) -> int:
    return len(re.sub(r"\s+", "", value or ""))


def _html_to_visible_text(raw_html: str) -> str:
    if not raw_html:
        return ""
    text = _SCRIPT_STYLE_RE.sub(" ", raw_html)
    text = _HTML_TAG_RE.sub(" ", text)
    text = _HTML_NBSP_RE.sub(" ", text)
    return html_lib.unescape(text)


def _visible_payload(*, raw_text: Optional[str], raw_html: Optional[str]) -> str:
    """Prefer the longer visible surface so a thin extracted line cannot hide a real body."""
    text = (raw_text or "").strip()
    html_visible = _html_to_visible_text(raw_html or "").strip()
    if text and html_visible:
        if _compact_len(text) >= _compact_len(html_visible):
            return text
        return html_visible
    return text or html_visible


def _marker_hits(visible: str) -> list[tuple[str, int]]:
    hits: list[tuple[str, int]] = []
    for marker_name, marker_re in _SUBSCRIPTION_CHROME_MARKERS + _LOGIN_SURFACE_BODY_MARKERS:
        for match in marker_re.finditer(visible):
            hits.append((marker_name, match.end() - match.start()))
    return hits


def login_subscription_chrome_dominates(
    *,
    raw_text: Optional[str],
    raw_html: Optional[str],
) -> Optional[str]:
    """Return a marker name when login/subscription chrome dominates; None for crumbs/absent.

    Incidental nav/footer/cookie/subscription-widget leftovers on a long real
    judgment must not quarantine. Markers must be the main content.
    """
    visible = _visible_payload(raw_text=raw_text, raw_html=raw_html)
    hits = _marker_hits(visible)
    html_blob = raw_html or ""
    has_password = bool(_PASSWORD_INPUT_RE.search(html_blob))
    if not hits and not has_password:
        return None

    compact_total = _compact_len(visible)
    marker_chars = sum(length for _name, length in hits)
    first_marker = hits[0][0] if hits else "password_input"
    marker_share = (marker_chars / compact_total) if compact_total else 1.0

    # Thin / empty visible body: a leftover widget IS the page.
    if compact_total < _THIN_VISIBLE_COMPACT_CHARS:
        return first_marker
    # Chrome phrases are a large share of the visible text.
    if marker_share >= _MARKER_DOMINANCE_RATIO:
        return first_marker
    # Medium paywall/CTA copy with no judgment structure still fail-closes.
    if (
        hits
        and compact_total < _MEDIUM_UNSTRUCTURED_COMPACT_CHARS
        and not _JUDGMENT_STRUCTURE_RE.search(visible)
    ):
        return first_marker
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


def _flatten_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_flatten_values(v))
        return out
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_values(item))
        return out
    return [str(value)]


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
    judge_fields: Optional[dict[str, Any]] = None,
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

    for field_name, value in (judge_fields or {}).items():
        for flattened in _flatten_values(value):
            marker = _match_subscription_chrome(flattened)
            if marker:
                return JudgmentGuardSignal(
                    reason_code="subscription_chrome",
                    signal=f"{field_name}_{marker}",
                    matched_value=flattened[:500],
                )
            if flattened.strip().lower() == "read":
                return JudgmentGuardSignal(
                    reason_code="login_stub",
                    signal=f"{field_name}_read_button",
                    matched_value=flattened[:500],
                )

    # Site chrome always includes Update Subscriber modal + FAQ crumbs.
    # Only quarantine when those markers dominate the visible payload
    # (thin body, high marker share, or unstructured CTA copy). A long
    # real judgment with incidental nav/footer leftovers is not a stub.
    # Citation Name with a value still short-circuits (#100).
    if not has_case:
        dominating = login_subscription_chrome_dominates(raw_text=raw_text, raw_html=raw_html)
        if dominating:
            reason_code = "login_stub" if dominating in {"agree_terms", "password_input"} else "subscription_chrome"
            for field_name, value in (("raw_text", raw_text or ""), ("raw_html", raw_html or "")):
                if dominating == "password_input" and field_name == "raw_html" and _PASSWORD_INPUT_RE.search(value):
                    return JudgmentGuardSignal(
                        reason_code=reason_code,
                        signal=f"{field_name}_{dominating}",
                        matched_value=value[:500],
                    )
                marker = _match_subscription_chrome(value)
                if marker:
                    return JudgmentGuardSignal(
                        reason_code=reason_code,
                        signal=f"{field_name}_{marker}",
                        matched_value=value[:500],
                    )
                if dominating == "agree_terms" and re.search(r"(?i)\bi\s+agree\s+with\s+the\s+terms\b", value):
                    return JudgmentGuardSignal(
                        reason_code=reason_code,
                        signal=f"{field_name}_{dominating}",
                        matched_value=value[:500],
                    )
            return JudgmentGuardSignal(
                reason_code=reason_code,
                signal=f"payload_{dominating}",
                matched_value=(raw_text or raw_html or "")[:500],
            )
    return None


def guard_reason(signal: JudgmentGuardSignal) -> str:
    return f"{signal.reason_code}: {signal.signal}"
