"""Detect multi-act bleed and name/PDF mismatches on law.gok.pk statute rows.

The sweep that uses this module only quarantines. It does not rewrite statute
names or section text. A four-digit year used as a section number, and a year
or other junk number that merely sits before section 1, are reported and are
not enough to quarantine. A numbering drop quarantines when the restarted
text names a different instrument, or when it is a Short-title heading that
does not name the stored act. The stored act's own short title, including one
that differs only by a scan confusion or a single extra or missing letter, a
date or schedule row stored as section 01, and a called title in the same
sentence as ``substituted`` or ``namely`` are not bleed. A one- or two-letter
margin stamp in front of ``the`` (``ay) the ... University of Bhimber``) is
not a second act. ``amended`` in an earlier sentence does not hide the next
act. A Board or Council the act itself constitutes is not a foreign body, and
``the commission of an offence`` is not a Commission.
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
# A margin stamp on a scan becomes one or two letters, sometimes with a
# closing paren, glued to the front of "the". "New the ..." is a real word
# and stays. "ay) the University of Bhimber Act" is this act.
_CALLED_TITLE_STAMP = re.compile(
    r"^(?:(?:[a-z0-9]{1,2}|[a-z0-9]{1,4}[^a-z0-9\s]+)\s+)+(?=the\s+)",
    re.IGNORECASE,
)
_PROSE_HEADING = re.compile(r"(?i)\b(shall|means|provided|whereas|amended|substituted|inserted)\b")
_BODY_GROUP = r"authority|tribunal|board|council|corporation|commission"
# "the commission of an offence" is the act of committing, not a body named Commission.
_FOREIGN_BODY = re.compile(
    r"(?i)\b(?:headquarters|powers|duties|functions|fund|funds|constitution|composition|"
    r"meetings|employees|budget|accounts|establishment|housing\s+schemes?)\s+of\s+the\s+"
    rf"({_BODY_GROUP})\b"
    rf"|\b({_BODY_GROUP})\s+(?:fund|funds|budget)\b"
    r"|\bthe\s+(authority|tribunal|board|council|corporation)\b(?!\s+act\b)"
    r"|\bthe\s+(commission)\b(?!\s+act\b)"
    r"(?!\s+of\s+(?:(?:an|any|the|such)\s+)?offences?\b)"
)
_BODY_MEANS = re.compile(rf"(?i)[\"'“”]?\s*({_BODY_GROUP})\s*[\"'“”]?\s+means\b")
_BODY_CONSTITUTED = re.compile(
    rf"(?i)\bconstitution\s+of\s+(?:the\s+)?({_BODY_GROUP})\b"
    r"|\b(board|council)\s+of\s+(?:the\s+)?(?:directors|governors)\b"
    rf"|\bestablish(?:ed)?\s+(?:a|an|the)\s+(?:[a-z]+\s+){{0,6}}({_BODY_GROUP})\b"
)
# i/l/1, c/e, o/0, s/5. Not a general edit distance: election/elections is only
# a one-character slip when the two titles have the same number of words.
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
_EMBEDDED_SECTION_ONE = re.compile(r"(?im)^\s*1[\.\)]\s+short\s+title\b")
_SECTION_LINE = re.compile(r"^\d{1,3}[\.\)]\s+\S")
_FINISHED_SENTENCE_END = re.compile(r"[.?!](?:[\"'”’)\]]+)?\s*$")
_UNFINISHED_TAIL = re.compile(r"(?:[:;,—–]|--)(?:[\"'”’)\]]+)?\s*$")
_FOLLOWING_RESTART = re.compile(r"(?i)^\s*1[\.\)]\s+\S")
# "shall be called in question" is a presumption clause, not a short title.
_FOLLOWING_CALLED = re.compile(
    r"(?i)\bshort\s+title\b|\b(?:may|shall)\s+be\s+called\s+(?!in\b|into\b|upon\b)"
)
_AMENDMENT_CITATION = re.compile(
    r"(?i)\b(?:substituted|inserted|amended|repealed|omitted|namely)\b"
)
# Verbs that introduce a quoted short title. "amended" in a preamble does not.
_SUBSTITUTION_KEYWORD = re.compile(
    r"(?i)\b(?:substituted|inserted|omitted|repealed|namely)\b"
)
# A real sentence end. Skip the period in "1." and the marginal note "title.—".
# "enacted as follows" starts a new act.
_CITATION_SENTENCE_BOUNDARY = re.compile(
    r"(?i)[;?!]|(?<!\d)\.(?!\d)(?![-—–])|\benacted\s+as\s+follows\b"
)
_NEW_ACT_MARKER = re.compile(
    r"(?i)\b(?:1[\.\)]\s+short\s+title|section\s+1[\.\)]\s+|enacted\s+as\s+follows)\b"
)
_SHORT_TITLE_HEADING = re.compile(r"(?i)\bshort\s+title\b")
_THIS_INSTRUMENT = re.compile(r"(?i)\bthis\s+(?:act|ordinance|order|code)\b\s*$")


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


def _one_char_indel(left: str, right: str) -> bool:
    """True when one word is the other plus a single letter (``bhimber`` / ``bhimbher``)."""
    if abs(len(left) - len(right)) != 1:
        return False
    shorter, longer = (left, right) if len(left) < len(right) else (right, left)
    if len(shorter) < 6:
        return False
    return any(longer[:index] + longer[index + 1 :] == shorter for index in range(len(longer)))


def _ocr_confusion_equivalent(left: str, right: str) -> bool:
    """True when two words differ only by scan confusions, including ``m`` / ``rn``."""
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


def _ocr_token_equivalent(left: str, right: str) -> bool:
    if left == right:
        return True
    if _one_char_indel(left, right):
        return True
    return _ocr_confusion_equivalent(left, right)


def _ocr_sets_align(left: set[str], right: set[str]) -> bool:
    """Pair every word. Extra words on one side stay different.

    ``Elections`` is one letter from ``Election``, but ``Elections Act`` is not
    ``Election Commission Act``: the second title has a word the first lacks.
    ``Bhimbher Universty`` does line up with ``University of Bhimber``.
    """
    if not left or len(left) != len(right):
        return False
    pending = list(right)

    def pair(tokens: list[str]) -> bool:
        if not tokens:
            return True
        token = tokens[0]
        for index, other in enumerate(pending):
            if other is None:
                continue
            if token == other or _ocr_token_equivalent(token, other):
                pending[index] = None
                if pair(tokens[1:]):
                    return True
                pending[index] = other
        return False

    return pair(list(left))


def _same_instrument(left: str, right: str) -> bool:
    a = _content_tokens(left)
    b = _content_tokens(right)
    if not a or not b:
        return False
    if a & b:
        return True
    return _ocr_sets_align(a, b)


def _is_act_heading_line(line: str) -> bool:
    stripped = (line or "").strip()
    if not stripped or len(stripped) > 180:
        return False
    if _PROSE_HEADING.search(stripped):
        return False
    # A wrapped operative line that merely ends "this Act" is not an instrument title.
    if _THIS_INSTRUMENT.search(stripped):
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


def _normalize_called_title(title: str) -> str:
    """Drop a scan stamp glued in front of ``the`` on a called title."""
    cleaned = re.sub(r"\s+", " ", title or "").strip(" .,:;—-")
    return _CALLED_TITLE_STAMP.sub("", cleaned).strip()


def _called_title_is_citation(text: str, start: int) -> bool:
    """A called title is a citation only when the keyword shares its sentence.

    ``amended`` in an earlier sentence, or before ``1. Short title`` /
    ``enacted as follows`` with no substitution verb, does not quote the
    next act. ``substituted`` or ``namely`` in that same sentence does.
    """
    window = (text or "")[max(0, start - 500):start]
    bounds = list(_CITATION_SENTENCE_BOUNDARY.finditer(window))
    sentence = window[bounds[-1].end():] if bounds else window
    if not _AMENDMENT_CITATION.search(sentence):
        return False
    marker = _NEW_ACT_MARKER.search(sentence)
    if marker is None:
        return True
    return _SUBSTITUTION_KEYWORD.search(sentence[: marker.start()]) is not None


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
        if _called_title_is_citation(text or "", match.start()):
            continue
        title = _normalize_called_title(match.group(1))
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


def _called_titles(text: str) -> list[str]:
    found: list[str] = []
    for match in _CALLED_TITLE.finditer(text or ""):
        title = _normalize_called_title(match.group(1))
        if title:
            found.append(title)
    return found


def _bodies_named_by_anchor(section: dict, anchor_name: str) -> set[str]:
    """Bodies this section constitutes for the stored act, not for a later act.

    ``Constitution of the Board`` of the Cell, ``Board of Directors`` of the
    Organization, ``establish a Zakat Council``. A Tribunal constituted in
    text that never names the stored act stays foreign.
    """
    blob = _section_blob(section)
    if not _same_instrument(blob, anchor_name):
        return set()
    found: set[str] = set()
    for pattern in (_BODY_MEANS, _BODY_CONSTITUTED):
        for match in pattern.finditer(blob):
            for group in match.groups():
                if group:
                    found.add(group.lower())
    for token in _content_tokens(anchor_name):
        if len(token) < 4:
            continue
        for body in _BODY_WORDS:
            if re.search(rf"\b{re.escape(token)}\s+{body}\b", blob, re.I):
                found.add(body)
    return found


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
    defined = {word for word in _BODY_WORDS if re.search(rf"\b{word}\b", blob)}
    for section in sections:
        defined |= _bodies_named_by_anchor(section, stored_name)
    return defined


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


def _section_blob(section: dict) -> str:
    return "\n".join(
        (
            str(section.get("section_title") or ""),
            str(section.get("section_text") or ""),
        )
    )


def _restart_opens_other_act(section: dict, anchor_name: str) -> bool:
    """True when a numbering drop is a later act, not the stored instrument."""
    text = str(section.get("section_text") or "")
    if _foreign_titles(text, anchor_name):
        return True
    if not _SHORT_TITLE_HEADING.search(_section_blob(section)):
        return False
    called = _called_titles(text)
    # A called title of this act, including an OCR-garbled one, is not a new act.
    # A heading that says only "Short title" and never names this act still is.
    if called and all(_same_instrument(title, anchor_name) for title in called):
        return False
    return not _same_instrument(_section_blob(section), anchor_name)


def _number_signals(sections: Sequence[dict], anchor_name: str) -> set[str]:
    signals: set[str] = set()
    numbers: list[int] = []
    ordered = _ordered_sections(sections)
    parsed: list[tuple[Optional[int], dict]] = []
    for section in ordered:
        number = _section_number(section)
        parsed.append((number, section))
        if number is not None:
            numbers.append(number)
        text = str(section.get("section_text") or "")
        # "1. Short title" inside a later section is another act only when that
        # text names a different instrument, not when it quotes an amended title.
        if number != 1 and _EMBEDDED_SECTION_ONE.search(text) and _foreign_titles(text, anchor_name):
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
    for number, section in parsed:
        if number is None or number >= 1800:
            continue
        if seen_max is not None and number <= 2 and seen_max >= 5 and number < seen_max:
            # A drop back to 1/2 is a second act when that block names another
            # instrument, or when it is a Short-title heading that does not
            # name the stored act. The stored act's own short title, a date
            # stored as section 01, or a schedule row "01. Middle Classes" is not.
            if _restart_opens_other_act(section, anchor_name):
                signals.add("numbering_restart")
                break
            continue
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
    signals.update(_number_signals(sections, stored_name))
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

