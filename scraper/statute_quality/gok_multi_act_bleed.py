"""Detect multi-act bleed and name/PDF mismatches on law.gok.pk statute rows.

The sweep that uses this module only quarantines. It does not rewrite statute
names or section text. A four-digit year used as a section number, and a year
or other junk number that merely sits before section 1, are reported and are
not enough to quarantine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import unquote, urlsplit

from scraper.parsers.statute_parser import _parse_numeric_section_number

REASON_BLEED = "multi_act_pdf_bleed:law.gok.pk"
REASON_NAME_MISMATCH = "name_pdf_mismatch:law.gok.pk"

VERDICT_MATCH = "MATCH"
VERDICT_MISMATCH = "MISMATCH"
ACTION_NONE = "none"
ACTION_QUARANTINE = "quarantine"

# A handful of headings about a body the act never names or defines.
FOREIGN_BODY_MIN = 5

_GOK_HOSTS = frozenset({"law.gok.pk", "www.law.gok.pk"})
_STOP = frozenset(
    {
        "the",
        "of",
        "and",
        "a",
        "an",
        "for",
        "to",
        "in",
        "on",
        "by",
        "at",
        "azad",
        "jammu",
        "kashmir",
        "ajk",
        "islamic",
        "republic",
        "pakistan",
        "west",
        "north",
        "act",
        "ordinance",
        "order",
        "ord",
        "no",
        "vol",
        "volume",
        "revised",
    }
)
_BODY_WORDS = ("authority", "tribunal", "board", "council", "corporation", "commission")
_ACT_NUMBER_PARENS = re.compile(
    r"\(?\s*act\s+(?:no\.?\s*)?[ivxlcdm0-9]+\s+of\s+(?:18|19|20)\d{2}\s*\)?",
    re.IGNORECASE,
)
_YEAR_TOKEN = re.compile(r"(?:18|19|20)\d{2}")
_FILENAME_INSTRUMENT = re.compile(
    r"(?i)(act|ordinance|\bord\b|code|regulation|legislative|courts|volume|constitution)"
)
_ACT_HEADING_LINE = re.compile(
    r"^(?:the\s+)?"
    r"(?!an\s+act\b)"
    r"([a-z][a-z0-9&'’\-]*(?:\s+[a-z][a-z0-9&'’\-]*){2,18})"
    r"\s+(?:act|ordinance|order|code)"
    r"(?:,?\s+(?:no\.\s*)?[ivxlcdm]+)?"
    r"(?:,?\s+(?:of\s+)?(?:18|19|20)\d{2})?"
    r"\s*$",
    re.IGNORECASE,
)
_CALLED_TITLE = re.compile(
    r"(?i)\bthis\s+(?:act|ordinance|order|code)\s+(?:may|shall)\s+be\s+called\s+(?:the\s+)?"
    r"(.{6,180}?\b(?:act|ordinance|order|code)\b(?:,?\s+(?:18|19|20)\d{2})?)"
)
_PROSE_HEADING = re.compile(r"(?i)\b(shall|means|provided|whereas|amended|substituted|inserted)\b")
_FOREIGN_BODY = re.compile(
    r"(?i)\b(?:headquarters|powers|duties|functions|fund|funds|constitution|composition|"
    r"meetings|employees|budget|accounts|establishment|housing\s+schemes?)\s+of\s+the\s+"
    r"(authority|tribunal|board|council|corporation|commission)\b"
    r"|\b(authority|tribunal|board|council|corporation|commission)\s+(?:fund|funds|budget)\b"
    r"|\bthe\s+(authority|tribunal|board|council|corporation|commission)\b(?!\s+act\b)"
)
_EMBEDDED_SECTION_ONE = re.compile(r"(?im)^\s*1[\.\)]\s+short\s+title\b")
_SECTION_LINE = re.compile(r"^\d{1,3}[\.\)]\s+\S")
_FINISHED_SENTENCE_END = re.compile(r"[.?!](?:[\"'”’)\]]+)?\s*$")
_UNFINISHED_TAIL = re.compile(r"(?:[:;,—–]|--)(?:[\"'”’)\]]+)?\s*$")
_FOLLOWING_RESTART = re.compile(r"(?i)^\s*1[\.\)]\s+\S")
_FOLLOWING_CALLED = re.compile(r"(?i)\b(?:(?:may|shall)\s+be\s+called|short\s+title)\b")


@dataclass(frozen=True)
class GokMultiActVerdict:
    verdict: str
    action: str
    reason: Optional[str]
    signals: tuple[str, ...]
    fetch_url: str
    pipe_label: str


def split_statute_source_url(source_url: Optional[str]) -> tuple[str, str]:
    """Split a ``{pdf_url}|{label}`` identity key. The label is not an act title."""
    raw = (source_url or "").strip()
    if "|" not in raw:
        return raw, ""
    fetch, label = raw.split("|", 1)
    return fetch.strip(), label.strip()


def is_law_gok_source(source_url: Optional[str]) -> bool:
    fetch, _label = split_statute_source_url(source_url)
    host = (urlsplit(fetch).hostname or "").lower()
    return host in _GOK_HOSTS


def pdf_filename_stem(source_url: Optional[str]) -> str:
    fetch, _label = split_statute_source_url(source_url)
    return Path(unquote(urlsplit(fetch).path)).stem


def _content_tokens(value: str) -> set[str]:
    text = _ACT_NUMBER_PARENS.sub(" ", value or "")
    text = text.replace("|", " ")
    text = re.sub(r"[-_./]+", " ", text).lower()
    tokens = set(re.findall(r"[a-z0-9]+", text))
    tokens -= _STOP
    return {token for token in tokens if not _YEAR_TOKEN.fullmatch(token)}


def _same_instrument(left: str, right: str) -> bool:
    a = _content_tokens(left)
    b = _content_tokens(right)
    if not a or not b:
        return False
    return bool(a & b)


def _is_act_heading_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped or len(stripped) > 180:
        return False
    if _PROSE_HEADING.search(stripped):
        return False
    return _ACT_HEADING_LINE.match(stripped) is not None


def _is_unfinished_sentence(line: Optional[str]) -> bool:
    """True when ``line`` continues into the next line.

    A missing line is not unfinished: a heading that opens a chunk can be a
    new act. A line that ends with a colon, comma, or dash (``namely:—``,
    ``In``) is unfinished amending or repealing text.
    """
    if line is None:
        return False
    stripped = line.strip()
    if not stripped:
        return False
    if _UNFINISHED_TAIL.search(stripped):
        return True
    if _FINISHED_SENTENCE_END.search(stripped):
        return False
    return True


def _previous_nonempty(lines: Sequence[str], index: int) -> Optional[str]:
    for cursor in range(index - 1, -1, -1):
        if lines[cursor].strip():
            return lines[cursor]
    return None


def _following_opens_new_act(lines: Sequence[str], index: int) -> bool:
    """True when the lines after a title restart numbering or state a short title."""
    seen = 0
    for cursor in range(index + 1, len(lines)):
        stripped = lines[cursor].strip()
        if not stripped:
            continue
        seen += 1
        if _FOLLOWING_RESTART.match(stripped) or _EMBEDDED_SECTION_ONE.search(stripped):
            return True
        if _FOLLOWING_CALLED.search(stripped):
            return True
        if seen >= 6:
            break
    return False


def _heading_opens_new_act(lines: Sequence[str], index: int) -> bool:
    """A bare act-name line is a new act only with independent evidence.

    Evidence is a numbering restart, an embedded ``1. Short title`` or
    ``may be called``, or a previous line that is not an unfinished sentence.
    A title wrapped out of amending or repealing text is a citation.
    """
    if not _is_unfinished_sentence(_previous_nonempty(lines, index)):
        return True
    return _following_opens_new_act(lines, index)


def _foreign_titles(text: str, anchor_name: str) -> list[str]:
    found: list[str] = []
    lines = (text or "").splitlines()
    for index, line in enumerate(lines):
        if (
            _is_act_heading_line(line)
            and not _same_instrument(line, anchor_name)
            and _heading_opens_new_act(lines, index)
        ):
            found.append(line.strip())
    for match in _CALLED_TITLE.finditer(text or ""):
        title = re.sub(r"\s+", " ", match.group(1)).strip(" .,:;—-")
        if title and not _same_instrument(title, anchor_name):
            found.append(title)
    return found


def truncate_at_second_act_heading(text: str, anchor_name: str) -> str:
    """Drop a later act that shares one law.gok.pk PDF with ``anchor_name``.

    A standalone act name is cut only when it opens a new act: the previous
    line is a finished sentence, or the following lines restart at section 1,
    say ``1. Short title``, or say ``may be called``. A title carried over
    from amending or repealing text stays.
    """
    if not text or not anchor_name:
        return text
    raw_lines = text.splitlines(keepends=True)
    stripped_lines = [line.strip() for line in raw_lines]
    seen_anchor = False
    seen_section = False
    offset = 0
    for index, line in enumerate(raw_lines):
        stripped = stripped_lines[index]
        if _is_act_heading_line(stripped):
            if _same_instrument(stripped, anchor_name):
                seen_anchor = True
            elif (
                (seen_anchor or seen_section)
                and offset > 0
                and _heading_opens_new_act(stripped_lines, index)
            ):
                return text[:offset].rstrip()
        elif _SECTION_LINE.match(stripped):
            seen_section = True
        offset += len(line)
    return text


def _ordered_sections(sections: Sequence[dict]) -> list[dict]:
    def sort_key(item: tuple[int, dict]) -> tuple:
        index, section = item
        raw = section.get("sort_key")
        if raw is None:
            return (1, index, 0)
        return (0, int(raw), index)

    return [section for _index, section in sorted(enumerate(sections), key=sort_key)]


def _section_number(section: dict) -> Optional[int]:
    return _parse_numeric_section_number(str(section.get("section_number") or ""))


def _defined_bodies(stored_name: str, sections: Sequence[dict]) -> set[str]:
    chunks = [stored_name or ""]
    for section in sections:
        number = _section_number(section)
        title = str(section.get("section_title") or "")
        if (number is not None and number <= 3) or re.search(
            r"(?i)\b(definition|interpretation|short\s+title)\b", title
        ):
            chunks.append(title)
            chunks.append(str(section.get("section_text") or ""))
    blob = " ".join(chunks).lower()
    return {word for word in _BODY_WORDS if re.search(rf"\b{word}\b", blob)}


def _bodies_in_heading(section: dict) -> set[str]:
    title = str(section.get("section_title") or "")
    text = str(section.get("section_text") or "")
    window = f"{title}\n{text[:240]}"
    found: set[str] = set()
    for match in _FOREIGN_BODY.finditer(window):
        for group in match.groups():
            if group:
                found.add(group.lower())
    return found


def _foreign_body_count(stored_name: str, sections: Sequence[dict]) -> int:
    defined = _defined_bodies(stored_name, sections)
    count = 0
    for section in sections:
        number = _section_number(section)
        title = str(section.get("section_title") or "")
        if number is not None and number <= 3:
            continue
        if re.search(r"(?i)\b(definition|interpretation|short\s+title)\b", title):
            continue
        foreign = _bodies_in_heading(section) - defined
        if foreign:
            count += 1
    return count


def _number_signals(sections: Sequence[dict]) -> set[str]:
    signals: set[str] = set()
    numbers: list[int] = []
    for section in _ordered_sections(sections):
        number = _section_number(section)
        if number is not None:
            numbers.append(number)
        if number != 1 and _EMBEDDED_SECTION_ONE.search(str(section.get("section_text") or "")):
            signals.add("numbering_restart")
    if any(1800 <= number <= 2030 for number in numbers):
        signals.add("year_fragment_section")
    start = 0
    while start < len(numbers) and numbers[start] >= 1800:
        start += 1
    leading = numbers[:start]
    if (
        start < len(numbers)
        and numbers[start] <= 2
        and any(1800 <= number <= 2030 for number in leading)
    ):
        signals.add("year_before_section_one")
    seen_max: Optional[int] = None
    for number in numbers[start:]:
        if number >= 1800:
            continue
        if seen_max is not None and number <= 2 and seen_max >= 5 and number < seen_max:
            signals.add("numbering_restart")
            break
        if seen_max is None or number > seen_max:
            seen_max = number
    return signals


def _name_pdf_mismatch(stored_name: str, source_url: str) -> bool:
    stem = pdf_filename_stem(source_url)
    if not stem or not _FILENAME_INSTRUMENT.search(stem):
        return False
    file_tokens = _content_tokens(stem)
    name_tokens = _content_tokens(stored_name)
    if len(file_tokens) < 2 or not name_tokens:
        return False
    return not (file_tokens & name_tokens)


def classify_gok_multi_act(
    *,
    stored_name: str,
    source_url: str,
    sections: Sequence[dict],
    pdf_text: Optional[str] = None,
) -> GokMultiActVerdict:
    """Classify one statute. Non-law.gok.pk URLs are left alone."""
    fetch_url, pipe_label = split_statute_source_url(source_url)
    if not is_law_gok_source(source_url):
        return GokMultiActVerdict(
            verdict=VERDICT_MATCH,
            action=ACTION_NONE,
            reason=None,
            signals=(),
            fetch_url=fetch_url,
            pipe_label=pipe_label,
        )
    signals: set[str] = set()
    signals.update(_number_signals(sections))
    corpus = [str(section.get("section_text") or "") for section in sections]
    if pdf_text:
        corpus.append(pdf_text)
    if any(_foreign_titles(text, stored_name) for text in corpus):
        signals.add("second_act_title")
    if _foreign_body_count(stored_name, sections) >= FOREIGN_BODY_MIN:
        signals.add("foreign_body_run")
    if _name_pdf_mismatch(stored_name, source_url):
        signals.add("name_pdf_mismatch")

    bleed = bool(signals & {"second_act_title", "numbering_restart", "foreign_body_run"})
    if bleed:
        reason = REASON_BLEED
    elif "name_pdf_mismatch" in signals:
        reason = REASON_NAME_MISMATCH
    else:
        reason = None
    if reason is None:
        verdict, action = VERDICT_MATCH, ACTION_NONE
    else:
        verdict, action = VERDICT_MISMATCH, ACTION_QUARANTINE
    ordered = tuple(signal for signal in (
        "second_act_title",
        "numbering_restart",
        "foreign_body_run",
        "name_pdf_mismatch",
        "year_fragment_section",
        "year_before_section_one",
    ) if signal in signals)
    return GokMultiActVerdict(
        verdict=verdict,
        action=action,
        reason=reason,
        signals=ordered,
        fetch_url=fetch_url,
        pipe_label=pipe_label,
    )

