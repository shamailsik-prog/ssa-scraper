"""Classify Senate-sourced statute rows against the PDF at source_url.

``{pdf_url}|{label}`` is a normal staging identity key (act number or bill title).
The label differing from ``statute.name`` is not, by itself, a mismatch.

Verdicts: MATCH, MISMATCH, PARTIAL, FETCH_FAIL, plus SKIPPED_SHELL for the
already-quarantined fragment-name / bill-title / no-section rows.

An act-number gap is ignored only when the significant words agree in both
directions under OCR equivalence, both titles carry the same year, and the
act numbers share that year and differ by one (roman 30 against 31). Amendment
and ordinal words stay significant. A year stored as a section number stays
on the row and does not leave MATCH when that wording already agrees.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlsplit

from scraper.parsers.statute_parser import (
    _is_plausible_act_title,
    _parse_numeric_section_number,
    looks_like_clause_fragment,
    looks_like_fragment_name,
)
from scraper.statute_quality.gazette_header import extract_gazette_title_year

VERDICT_MATCH = "MATCH"
VERDICT_MISMATCH = "MISMATCH"
VERDICT_PARTIAL = "PARTIAL"
VERDICT_FETCH_FAIL = "FETCH_FAIL"
VERDICT_SKIPPED_SHELL = "SKIPPED_SHELL"

ACTION_NONE = "none"
ACTION_QUARANTINE = "quarantine"
ACTION_NEEDS_REEXTRACT = "needs_reextract"

REASON_URL_MISMATCH = "senate_integrity:url_act_mismatch"
REASON_PARTIAL_REEXTRACT = "senate_integrity:partial_needs_reextract"
REASON_PARTIAL_IDENTITY = "senate_integrity:partial_identity"

SENATE_HOSTS = frozenset({"senate.gov.pk", "www.senate.gov.pk"})
PCP_HOSTS = frozenset({"pcp.gov.pk", "www.pcp.gov.pk"})
GAZETTE_SOURCE_NAMES = frozenset({"Senate", "GazetteOfPakistan"})

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}

_TOKEN_STOP = frozenset(
    {
        "the",
        "of",
        "and",
        "for",
        "act",
        "ordinance",
        "order",
        "orders",
        "rules",
        "regulations",
        "pakistan",
        "islamic",
        "republic",
        "federal",
        "bill",
    }
)

_CALLED_TITLE_RE = re.compile(
    r"(?is)\b(?:this|the)\s+(?:act|ordinance|order)\s+may\s+be\s+called\s+(?:the\s+)?"
    r"(?P<title>[^.;\n]{6,220}?\b(?:act|ordinance|order|rules|regulations)\b[^.;\n]{0,48})"
)
_ACT_NO_LABELED_RE = re.compile(
    r"(?i)\bact\s+no\.?\s*([IVXLCDM]+|\d{1,4})\s+of\s+((?:18|19|20)\d{2})\b"
)
_ACT_NO_HEADING_RE = re.compile(
    r"(?im)^[ \t]*act\s+no\.?\s*([IVXLCDM]+|\d{1,4})\s+of\s+((?:18|19|20)\d{2})\b"
)
# Short extracts must not be treated as proof that stored sections are missing.
MIN_PDF_WORDS_FOR_ABSENCE = 50
_PDF_WORD_RE = re.compile(r"[A-Za-z]{3,}")
_INCOHERENT_NAME_SIGNALS = frozenset(
    {"preamble_junk_name", "definitions_fragment_name", "gazette_header_not_act"}
)
_PIPE_ACT_NO_RE = re.compile(
    r"(?i)^(?:act\s+)?(?:no\.?\s+)?([IVXLCDM]+|\d{1,4})\s+of\s+((?:18|19|20)\d{2})$"
)
_GAZETTE_NON_ACT_RE = re.compile(
    r"(?i)^(?:the\s+)?(?:"
    r"president'?s\s+orders\s+and\s+regulations"
    r"|acts?,?\s+ordinances?,?\s+president'?s\s+orders\s+and\s+regulations"
    r"|gazette\s+of\s+pakistan(?:\s+extraordinary)?"
    r"|published\s+by\s+authority"
    r"|registered\s+no\.?"
    r")\.?$"
)
_PREAMBLE_RE = re.compile(
    r"(?i)^(?:whereas\b|an act to\b|an ordinance to\b|be it enacted\b|it is hereby enacted\b)"
)
_NAMED_INSTRUMENT_RE = re.compile(
    r"(?i)\b([a-z][a-z'’-]{1,}(?:\s+[a-z][a-z'’-]{1,}){0,12}\s+"
    r"(?:act|ordinance|order|code|rules|regulations)),?\s*(?:of\s+)?((?:18|19|20)\d{2})"
)
_WORD_RE = re.compile(r"[a-z]{5,}")
_SUPPORT_STOP = frozenset(
    {
        "which",
        "their",
        "there",
        "about",
        "shall",
        "under",
        "every",
        "other",
        "would",
        "could",
        "being",
        "after",
        "before",
        "where",
        "those",
        "these",
        "until",
        "while",
        "section",
        "provided",
        "including",
        "except",
        "without",
        "within",
        "against",
        "between",
        "through",
    }
)


@dataclass(frozen=True)
class SenateIntegrityVerdict:
    verdict: str
    action: str
    reason: Optional[str]
    signals: tuple[str, ...]
    pdf_title: Optional[str]
    pipe_label: Optional[str]
    fetch_url: str
    needs_reextract: bool


def split_statute_source_url(source_url: Optional[str]) -> tuple[str, Optional[str]]:
    """Split ``https://host/file.pdf|label`` into the fetch URL and the label.

    The label is an identity disambiguator (``XIII of 1985`` or a bill title).
    It is not evidence that the stored act and the PDF disagree.
    """
    raw = (source_url or "").strip()
    if not raw:
        return "", None
    match = re.match(r"^(https?://[^\s|]+)(?:\|(.*))?$", raw, flags=re.IGNORECASE)
    if not match:
        return raw, None
    label = (match.group(2) or "").strip() or None
    return match.group(1), label


def _host(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_senate_sourced(source_url: Optional[str], source_name: Optional[str] = None) -> bool:
    """Senate uploads, plus Gazette of Pakistan PDFs on pcp.gov.pk for Senate/Gazette rows."""
    fetch_url, _label = split_statute_source_url(source_url)
    host = _host(fetch_url)
    if host in SENATE_HOSTS:
        return True
    if host in PCP_HOSTS and (source_name or "") in GAZETTE_SOURCE_NAMES:
        return True
    return False


def is_pdf_document_url(fetch_url: str) -> bool:
    path = (urlsplit(fetch_url).path or "").lower()
    return path.endswith(".pdf") or "/uploads/" in path or "/siteimage/" in path


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def is_gazette_non_act_title(name: str) -> bool:
    flat = _collapse(name).strip(" .")
    if not flat:
        return False
    return bool(_GAZETTE_NON_ACT_RE.match(flat))


def is_preamble_junk_name(name: str) -> bool:
    return bool(_PREAMBLE_RE.match(_collapse(name)))


def is_definitions_fragment_name(name: str) -> bool:
    flat = _collapse(name)
    if not flat:
        return False
    if re.match(r"(?i)(?:means\b|in this (?:act|ordinance)\b)", flat):
        return True
    if looks_like_clause_fragment(flat) and re.search(r"(?i)\bmeans\b", flat):
        return True
    return False


def is_truncated_or_newline_title(name: str) -> bool:
    raw = name or ""
    if re.search(r"[\r\n]", raw):
        return True
    flat = _collapse(raw)
    return bool(re.search(r"[-—/]\s*$", flat))


def is_coherent_act_title(name: str) -> bool:
    flat = _collapse(name)
    if not flat:
        return False
    if is_gazette_non_act_title(flat) or is_preamble_junk_name(flat) or is_definitions_fragment_name(flat):
        return False
    return bool(_is_plausible_act_title(flat))


def is_quarantined_bill_shell(
    *,
    already_quarantined: bool,
    stored_name: str,
    pipe_label: Optional[str],
    section_count: int,
) -> bool:
    """Common quarantined class: fragment name, ``|bill title``, and no sections.

    These rows are already unservable. The sweep counts them and does not fetch
    or relabel them.
    """
    if not already_quarantined or section_count != 0 or not (pipe_label or "").strip():
        return False
    name = stored_name or ""
    if (
        is_preamble_junk_name(name)
        or is_definitions_fragment_name(name)
        or is_gazette_non_act_title(name)
        or looks_like_fragment_name(name)
        or looks_like_clause_fragment(name)
    ):
        return True
    return not is_coherent_act_title(name)


def _roman_or_int(token: str) -> Optional[int]:
    raw = (token or "").strip().upper()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if any(ch not in _ROMAN for ch in raw):
        return None
    total = 0
    prev = 0
    for ch in reversed(raw):
        val = _ROMAN[ch]
        if val < prev:
            total -= val
        else:
            total += val
            prev = val
    return total or None


def parse_act_number(text: str, *, pipe_label: bool = False) -> Optional[tuple[int, int]]:
    """Return ``(number, year)`` for an act-number label or the first ``ACT No.`` in text."""
    raw = _collapse(text) if pipe_label else (text or "")
    if pipe_label:
        match = _PIPE_ACT_NO_RE.match(raw.strip(" ."))
    else:
        match = _ACT_NO_LABELED_RE.search(raw[:12000])
    if not match:
        return None
    number = _roman_or_int(match.group(1))
    if number is None:
        return None
    return number, int(match.group(2))


def _years(text: str) -> set[int]:
    return {int(y) for y in re.findall(r"\b(?:18|19|20)\d{2}\b", text or "")}


def _fold_title(text: str) -> str:
    """Title text with diacritics removed, before case and punctuation folding."""
    folded = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in folded if not unicodedata.combining(ch))


def _tokens(name: str) -> set[str]:
    cleaned = re.sub(r"[^a-z0-9\s]", " ", _collapse(_fold_title(name)).lower())
    cleaned = re.sub(r"\b(?:18|19|20)\d{2}\b", " ", cleaned)
    return {tok for tok in cleaned.split() if len(tok) >= 4 and tok not in _TOKEN_STOP}


# Single-character OCR swaps seen in Senate scans. Not general edit distance:
# police/policy, arms/army, civil/civic, and port/part must stay distinct.
_OCR_CONFUSABLE_SUBS = frozenset(
    {
        ("i", "l"),
        ("l", "i"),
        ("i", "1"),
        ("1", "i"),
        ("l", "1"),
        ("1", "l"),
        ("o", "0"),
        ("0", "o"),
        ("s", "5"),
        ("5", "s"),
        ("c", "e"),
        ("e", "c"),
    }
)


def _ocr_equivalent(left: str, right: str) -> bool:
    """True when two title words differ only by a few scan confusions.

    Allowed: ``i``/``l``/``1``, ``c``/``e``, and digit lookalikes, plus ``m``
    versus ``rn`` in a longer word (``amendment`` / ``arnendment``). At most
    one confusion in a word shorter than 8 characters, otherwise two.
    """
    if left == right:
        return True
    if min(len(left), len(right)) < 5 or abs(len(left) - len(right)) > 2:
        return False
    budget = 1 if min(len(left), len(right)) < 8 else 2
    allow_rn = max(len(left), len(right)) >= 8
    unreachable = budget + 1
    costs = [[unreachable] * (len(right) + 1) for _ in range(len(left) + 1)]
    costs[0][0] = 0
    for i in range(len(left) + 1):
        for j in range(len(right) + 1):
            used = costs[i][j]
            if used > budget:
                continue
            if i < len(left) and j < len(right) and left[i] == right[j] and used < costs[i + 1][j + 1]:
                costs[i + 1][j + 1] = used
            if (
                i < len(left)
                and j < len(right)
                and used + 1 <= budget
                and (left[i], right[j]) in _OCR_CONFUSABLE_SUBS
                and used + 1 < costs[i + 1][j + 1]
            ):
                costs[i + 1][j + 1] = used + 1
            if allow_rn and used + 1 <= budget:
                if (
                    i < len(left)
                    and left[i] == "m"
                    and j + 1 < len(right)
                    and right[j : j + 2] == "rn"
                    and used + 1 < costs[i + 1][j + 2]
                ):
                    costs[i + 1][j + 2] = used + 1
                if (
                    j < len(right)
                    and right[j] == "m"
                    and i + 1 < len(left)
                    and left[i : i + 2] == "rn"
                    and used + 1 < costs[i + 2][j + 1]
                ):
                    costs[i + 2][j + 1] = used + 1
    return costs[len(left)][len(right)] <= budget


def _matched_token_count(left: set[str], right: set[str]) -> int:
    """One-to-one overlap. Exact words pair first, then OCR-equivalent leftovers."""
    remaining = set(right)
    matched = 0
    leftover: list[str] = []
    for token in left:
        if token in remaining:
            remaining.remove(token)
            matched += 1
        else:
            leftover.append(token)
    for token in leftover:
        partner = next((other for other in remaining if _ocr_equivalent(token, other)), None)
        if partner is not None:
            remaining.remove(partner)
            matched += 1
    return matched


def _same_title_tokens(left: set[str], right: set[str]) -> bool:
    """Subset or Jaccard overlap, with OCR-equivalent words counted as shared."""
    matched = _matched_token_count(left, right)
    if matched == min(len(left), len(right)):
        return True
    union = len(left) + len(right) - matched
    return union > 0 and (matched / union) >= 0.55


def title_relation(left: str, right: str) -> str:
    """``same``, ``year_differs``, ``different``, or ``unknown``.

    Significant words are compared after case, diacritic, and punctuation
    folding. A scanned word still counts as shared when it differs only by
    OCR confusions (see ``_ocr_equivalent``), so a garbled same-act title is
    ``same`` rather than ``different``. Ordinal amendments and unrelated
    instruments stay ``different``.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return "unknown"
    if not _same_title_tokens(a, b):
        return "different"
    years_a, years_b = _years(left), _years(right)
    if years_a and years_b and years_a.isdisjoint(years_b):
        return "year_differs"
    return "same"


def _bidirectional_ocr_titles(left: str, right: str) -> bool:
    """Every significant word has an OCR partner, in both directions.

    ``title_relation`` still treats a subset or a Jaccard overlap of 0.55 as
    ``same``. That is too wide to drop an act-number disagreement: ``Companies
    Act`` would swallow ``Companies (Amendment) Act``, and ``Amendment`` would
    swallow ``Second Amendment``. Amendment and ordinal words are not stop words.
    """
    a, b = _tokens(left), _tokens(right)
    if not a or not b or len(a) != len(b):
        return False
    return _matched_token_count(a, b) == len(a)


def _titles_share_enactment_year(left: str, right: str) -> bool:
    """Both titles carry a year, and the year sets are equal."""
    years_left, years_right = _years(left), _years(right)
    return bool(years_left) and years_left == years_right


def _one_roman_act_misread(left: tuple[int, int], right: tuple[int, int]) -> bool:
    """Same act-number year, values one apart (roman 30 against 31)."""
    return left[1] == right[1] and abs(left[0] - right[0]) == 1


@dataclass(frozen=True)
class _ActSegment:
    act_number: Optional[tuple[int, int]]
    title: Optional[str]
    text: str


def _called_title_in(chunk: str) -> Optional[str]:
    match = _CALLED_TITLE_RE.search(chunk or "")
    if not match:
        return None
    title = _collapse(match.group("title")).strip(" .")
    if not title or is_gazette_non_act_title(title):
        return None
    return title


def _pdf_word_count(text: str) -> int:
    return len(_PDF_WORD_RE.findall(text or ""))


def act_segments(pdf_text: str) -> list[_ActSegment]:
    """Split a Gazette that prints several acts. A single act stays one segment."""
    text = pdf_text or ""
    if not text.strip():
        return []
    headings = list(_ACT_NO_HEADING_RE.finditer(text))
    if len(headings) >= 2:
        segments: list[_ActSegment] = []
        for index, heading in enumerate(headings):
            start = 0 if index == 0 else heading.start()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            number = _roman_or_int(heading.group(1))
            act = (number, int(heading.group(2))) if number is not None else None
            title_scope = text[heading.start() : end]
            segments.append(_ActSegment(act, _called_title_in(title_scope), text[start:end]))
        return segments
    return [_ActSegment(parse_act_number(text), extract_pdf_act_title(text), text)]


def _segment_matches(
    segment: _ActSegment,
    *,
    stored_name: str,
    pipe_label: Optional[str],
    pipe_act: Optional[tuple[int, int]],
) -> bool:
    if pipe_act and segment.act_number == pipe_act:
        return True
    if segment.title and is_coherent_act_title(stored_name):
        if title_relation(stored_name, segment.title) in {"same", "year_differs"}:
            return True
    if segment.title and pipe_label and pipe_act is None:
        if title_relation(pipe_label, segment.title) in {"same", "year_differs"}:
            return True
    return False


def _choose_segment(
    segments: Sequence[_ActSegment],
    *,
    stored_name: str,
    pipe_label: Optional[str],
    pipe_act: Optional[tuple[int, int]],
) -> Optional[_ActSegment]:
    matches = [
        segment
        for segment in segments
        if _segment_matches(segment, stored_name=stored_name, pipe_label=pipe_label, pipe_act=pipe_act)
    ]
    if not matches:
        return None
    if pipe_act:
        for segment in matches:
            if segment.act_number == pipe_act:
                return segment
    return matches[0]


def extract_pdf_act_title(pdf_text: str) -> Optional[str]:
    """First real act/ordinance short title, ignoring gazette running headers."""
    head = pdf_text or ""
    match = _CALLED_TITLE_RE.search(head[:16000])
    title = _collapse(match.group("title")) if match else ""
    if title and not is_gazette_non_act_title(title):
        return title.strip(" .")
    extracted = extract_gazette_title_year(head, source_name="Senate")
    if not extracted:
        return None
    fallback = _collapse(extracted[0])
    if not fallback or is_gazette_non_act_title(fallback):
        return None
    return fallback.strip(" .")


def _content_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall((text or "").lower()) if w not in _SUPPORT_STOP]


def _section_supported(section_text: str, pdf_words: set[str]) -> Optional[bool]:
    words = []
    for word in _content_words(section_text):
        if word not in words:
            words.append(word)
        if len(words) == 12:
            break
    if len(words) < 8:
        return None
    hits = sum(1 for word in words if word in pdf_words)
    return hits >= max(4, int(0.6 * len(words)))


def _substantial_section_support(sections: Sequence[dict], pdf_text: str) -> Optional[bool]:
    """True when any substantial section is in the PDF, False when every one misses."""
    pdf_words = set(_content_words(pdf_text))
    flags: list[bool] = []
    for section in sections:
        supported = _section_supported(section.get("section_text") or "", pdf_words)
        if supported is not None:
            flags.append(supported)
    if not flags:
        return None
    return any(flags)


def _year_as_section(sections: Sequence[dict]) -> bool:
    for section in sections:
        number = _parse_numeric_section_number(str(section.get("section_number") or ""))
        if number is not None and 1800 <= number <= 2100:
            return True
    return False


def _signals_for_sections(
    *,
    stored_name: str,
    sections: Sequence[dict],
    pdf_text: str,
) -> list[str]:
    signals: list[str] = []
    pdf_words = set(_content_words(pdf_text))
    support_flags: list[bool] = []
    named_other = False
    for section in sections:
        text = section.get("section_text") or ""
        supported = _section_supported(text, pdf_words)
        if supported is not None:
            support_flags.append(supported)
        if supported is False:
            for match in _NAMED_INSTRUMENT_RE.finditer(text):
                named = f"{_collapse(match.group(1))}, {match.group(2)}"
                if title_relation(stored_name, named) == "different":
                    named_other = True
                    break
    if (
        support_flags
        and not any(support_flags)
        and _pdf_word_count(pdf_text) >= MIN_PDF_WORDS_FOR_ABSENCE
    ):
        signals.append("section_absent_from_pdf")
    if named_other:
        signals.append("section_names_other_act")
    return signals


def _verdict(
    *,
    signals: list[str],
    pdf_title: Optional[str],
    pipe_label: Optional[str],
    fetch_url: str,
    title_agrees: bool = False,
) -> SenateIntegrityVerdict:
    signal_tuple = tuple(dict.fromkeys(signals))
    repairable = bool(pdf_title) and not is_gazette_non_act_title(pdf_title)
    if "act_number_disagrees" in signal_tuple or "pdf_title_disagrees" in signal_tuple or (
        "section_absent_from_pdf" in signal_tuple and _INCOHERENT_NAME_SIGNALS.intersection(signal_tuple)
    ):
        return SenateIntegrityVerdict(
            VERDICT_MISMATCH,
            ACTION_QUARANTINE,
            REASON_URL_MISMATCH,
            signal_tuple,
            pdf_title,
            pipe_label,
            fetch_url,
            False,
        )
    identity = {
        "preamble_junk_name",
        "definitions_fragment_name",
        "truncated_title",
        "year_disagrees",
        "year_as_section_number",
        "gazette_header_not_act",
        "section_absent_from_pdf",
        "section_names_other_act",
    }
    # A year stored as a section number is a storage defect (#363), not a
    # different statute, once the short title already agrees.
    if title_agrees:
        identity.discard("year_as_section_number")
    if identity.intersection(signal_tuple):
        if repairable and "gazette_header_not_act" not in signal_tuple:
            return SenateIntegrityVerdict(
                VERDICT_PARTIAL,
                ACTION_NEEDS_REEXTRACT,
                REASON_PARTIAL_REEXTRACT,
                signal_tuple,
                pdf_title,
                pipe_label,
                fetch_url,
                True,
            )
        return SenateIntegrityVerdict(
            VERDICT_PARTIAL,
            ACTION_QUARANTINE,
            REASON_PARTIAL_IDENTITY,
            signal_tuple,
            pdf_title,
            pipe_label,
            fetch_url,
            False,
        )
    if "pdf_text_empty" in signal_tuple:
        return SenateIntegrityVerdict(
            VERDICT_PARTIAL,
            ACTION_NONE,
            None,
            signal_tuple,
            pdf_title,
            pipe_label,
            fetch_url,
            False,
        )
    return SenateIntegrityVerdict(
        VERDICT_MATCH,
        ACTION_NONE,
        None,
        signal_tuple,
        pdf_title,
        pipe_label,
        fetch_url,
        False,
    )


def classify_senate_identity(
    *,
    stored_name: str,
    year_enacted: Optional[int] = None,
    source_url: Optional[str] = None,
    sections: Optional[Sequence[dict]] = None,
    pdf_text: Optional[str] = None,
    pdf_available: bool = True,
    already_quarantined: bool = False,
    section_count: Optional[int] = None,
) -> SenateIntegrityVerdict:
    """Compare one stored statute with the text of its current Senate/Gazette PDF."""
    body_sections = list(sections or [])
    count = len(body_sections) if section_count is None else section_count
    fetch_url, pipe_label = split_statute_source_url(source_url)
    if is_quarantined_bill_shell(
        already_quarantined=already_quarantined,
        stored_name=stored_name,
        pipe_label=pipe_label,
        section_count=count,
    ):
        return SenateIntegrityVerdict(
            VERDICT_SKIPPED_SHELL,
            ACTION_NONE,
            None,
            (),
            None,
            pipe_label,
            fetch_url,
            False,
        )
    if not pdf_available:
        return SenateIntegrityVerdict(
            VERDICT_FETCH_FAIL,
            ACTION_NONE,
            None,
            ("pdf_unavailable",),
            None,
            pipe_label,
            fetch_url,
            False,
        )

    text = pdf_text or ""
    signals: list[str] = []
    if is_gazette_non_act_title(stored_name):
        signals.append("gazette_header_not_act")
    if is_preamble_junk_name(stored_name):
        signals.append("preamble_junk_name")
    if is_definitions_fragment_name(stored_name):
        signals.append("definitions_fragment_name")
    if is_truncated_or_newline_title(stored_name):
        signals.append("truncated_title")
    if _year_as_section(body_sections):
        signals.append("year_as_section_number")

    segments = act_segments(text)
    pipe_act = parse_act_number(pipe_label or "", pipe_label=True) if pipe_label else None
    chosen = _choose_segment(segments, stored_name=stored_name, pipe_label=pipe_label, pipe_act=pipe_act)
    pdf_title = chosen.title if chosen is not None else (segments[0].title if segments else None)
    compare_text = chosen.text if chosen is not None else text
    title_agrees = bool(
        chosen is not None
        and pdf_title
        and is_coherent_act_title(stored_name)
        and _bidirectional_ocr_titles(stored_name, pdf_title)
        and _titles_share_enactment_year(stored_name, pdf_title)
    )
    # Drop the act-number signal only for a one-numeral misread on that title.
    # A wider gap, a missing year, or an extra amendment/ordinal word still disagrees.
    act_number_is_one_roman_misread = bool(
        title_agrees
        and pipe_act
        and chosen is not None
        and chosen.act_number
        and _one_roman_act_misread(pipe_act, chosen.act_number)
    )

    def _note_title_relation(title: Optional[str]) -> None:
        if not title or not is_coherent_act_title(stored_name):
            return
        relation = title_relation(stored_name, title)
        if relation == "different":
            signals.append("pdf_title_disagrees")
        elif relation == "year_differs" or (
            year_enacted
            and relation == "same"
            and _years(title)
            and year_enacted not in _years(title)
            and _years(stored_name).isdisjoint(_years(title))
        ):
            signals.append("year_disagrees")

    if chosen is not None:
        if (
            pipe_act
            and chosen.act_number
            and pipe_act != chosen.act_number
            and not act_number_is_one_roman_misread
        ):
            signals.append("act_number_disagrees")
        _note_title_relation(pdf_title)
    else:
        # No act segment matches the pipe label or stored name. Disagree only when
        # the stored sections are also absent, so a later act in a shared Gazette
        # is not quarantined for the first act's number.
        substantial_support = _substantial_section_support(body_sections, text)
        unsupported = substantial_support is False and _pdf_word_count(text) >= MIN_PDF_WORDS_FOR_ABSENCE
        if unsupported:
            if pipe_act and not any(segment.act_number == pipe_act for segment in segments):
                signals.append("act_number_disagrees")
            _note_title_relation(pdf_title)

    if compare_text.strip():
        signals.extend(
            _signals_for_sections(stored_name=stored_name, sections=body_sections, pdf_text=compare_text)
        )
    elif pdf_available:
        signals.append("pdf_text_empty")

    return _verdict(
        signals=signals,
        pdf_title=pdf_title,
        pipe_label=pipe_label,
        fetch_url=fetch_url,
        title_agrees=title_agrees,
    )
