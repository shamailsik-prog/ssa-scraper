r"""Read kpcode lawDetails / RuleDetails pages as operative sections, not the contents list.

kpcode prints a contents list (`5. Right of preemption.`) before the act. The generic
section splitter keeps that first copy of each number, so the corpus stored the heading
and dropped the body that follows in ``#content`` (or in the linked ``/uploads`` PDF).

This reader:

1. takes text from the ``#content`` container when the page HTML is available;
2. treats a contents line (``^\\d+\\. [^.]+\.$``) as a heading, and keeps the later
   operative chunk for that number;
3. falls back to plain text (the linked PDF) when the HTML chunks are still headings;
4. strips page furniture (``N | P a g e``) and a repeated act-title line followed by
   ``Received``.

The promotion gate ``kpcode_toc_stub`` refuses a kpcode statute when more than half of
its sections are still those headings (under 120 characters, or the contents pattern).
kpcode and pakp statutes from KPAssembly are jurisdiction ``Khyber Pakhtunkhwa``.
Pakistan Code stays Federal.
"""

from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

KPCODE_HOSTS = frozenset({"kpcode.kp.gov.pk", "www.kpcode.kp.gov.pk"})
KP_JURISDICTION = "Khyber Pakhtunkhwa"
TOC_STUB_MAX_CHARS = 120
TOC_STUB_FRACTION = 0.5

TOC_STUB_RE = re.compile(r"^\d+\. [^.]+\.$")
PAGE_LINE_RE = re.compile(r"(?im)^[ \t]*\d{1,3}[ \t]*\|[ \t]*P[ \t]*a[ \t]*g[ \t]*e[ \t]*$")
# "The Khyber Pakhtunkhwa Preemption Act, 1987" on one line and "Received" on the next,
# or both on the same line. The assent clause ("received the assent") does not match.
FOOTER_BLEED_RE = re.compile(
    r"(?im)^[ \t]*(?P<title>[^\n]{6,200}?\b(?:Act|Ordinance|Rules),?[ \t]+\d{4}\.?)[ \t]*(?:\n[ \t]*)?Received[ \t]*$"
)
SECTION_START_RE = re.compile(
    r"(?m)^[ \t]*(?:\[\d{1,3}\][ \t]*)*(\d{1,3}[A-Z]?)\.[ \t]*([^\n]*)$"
)
CONTENTS_RE = re.compile(r"(?im)^[ \t]*CONTENTS[ \t]*$")
DETAIL_PATH_RE = re.compile(r"(?i)/homepage/(?:law|rule)details/\d+")


def is_kpcode_statute_url(url: Optional[str]) -> bool:
    """True for a kpcode host, or a lawDetails / RuleDetails path (including fixture hosts)."""
    raw = (url or "").strip()
    if not raw:
        return False
    parts = urlsplit(raw)
    host = (parts.hostname or "").lower()
    path = parts.path or ""
    if host in KPCODE_HOSTS:
        return True
    return DETAIL_PATH_RE.search(path) is not None


def is_kpcode_statute_source(source_meta: Optional[dict]) -> bool:
    meta = source_meta or {}
    for key in ("url", "detail_url", "source_url"):
        if is_kpcode_statute_url(str(meta.get(key) or "")):
            return True
    return False


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").replace("\xa0", " ").strip()


def section_is_toc_stub(section_text: str) -> bool:
    """A contents heading: the ``N. Title.`` pattern, or a section under ~120 characters."""
    body = _flat(section_text)
    if not body:
        return True
    if TOC_STUB_RE.match(body):
        return True
    return len(body) < TOC_STUB_MAX_CHARS


def kpcode_sections_are_toc_stubs(sections: Optional[List[dict]]) -> bool:
    """True when more than half the sections are contents headings.

    The gate is strict greater-than one half, so a statute with a few short
    commencement lines still passes. An empty list is not this failure (the
    caller already treats missing sections as a different quarantine).
    """
    rows = [s for s in (sections or []) if isinstance(s, dict)]
    if not rows:
        return False
    stubs = sum(1 for s in rows if section_is_toc_stub(str(s.get("section_text") or "")))
    return stubs / len(rows) > TOC_STUB_FRACTION


def kpcode_toc_stub_quarantine_reason(
    *,
    source_name: Optional[str],
    source_url: Optional[str],
    sections: Optional[List[dict]],
) -> Optional[str]:
    """Reason string for the pre-upsert gate, or None when the statute may be saved.

    Fires only for kpcode URLs (lawDetails, RuleDetails, or the kpcode host).
    Pakistan Code and pakp gazette PDFs are left to their own gates. A kpcode
    statute is refused when more than half of its sections are contents headings.
    """
    if not is_kpcode_statute_url(source_url):
        return None
    rows = [s for s in (sections or []) if isinstance(s, dict)]
    if not rows or not kpcode_sections_are_toc_stubs(rows):
        return None
    stubs = sum(1 for s in rows if section_is_toc_stub(str(s.get("section_text") or "")))
    logger.info(
        "kpcode_toc_stub gate: %s/%s sections are contents headings source=%s url=%s",
        stubs,
        len(rows),
        source_name or "",
        source_url or "",
    )
    return f"kpcode_toc_stub: {stubs} of {len(rows)} sections are table-of-contents headings"


def _strip_page_furniture(text: str, statute_name: str = "") -> str:
    cleaned = PAGE_LINE_RE.sub("", text or "")
    cleaned = FOOTER_BLEED_RE.sub("", cleaned)
    title = _flat(statute_name).lower().rstrip(".")
    if not title:
        return cleaned
    lines = cleaned.splitlines()
    other = [
        line
        for line in lines
        if _flat(line).lower().rstrip(".") not in {title, "received"} and _flat(line)
    ]
    drop_bare_title = bool(other)
    kept: List[str] = []
    index = 0
    while index < len(lines):
        current = _flat(lines[index]).lower().rstrip(".")
        if not current:
            kept.append(lines[index])
            index += 1
            continue
        if current == title and drop_bare_title:
            lookahead = index + 1
            while lookahead < len(lines) and not _flat(lines[lookahead]):
                lookahead += 1
            if lookahead < len(lines) and _flat(lines[lookahead]).lower() == "received":
                index = lookahead + 1
            else:
                index += 1
            continue
        if current == "received":
            previous = ""
            scan = index - 1
            while scan >= 0 and not previous:
                previous = _flat(lines[scan]).lower().rstrip(".")
                scan -= 1
            if previous == title:
                index += 1
                continue
        kept.append(lines[index])
        index += 1
    return "\n".join(kept)


# Text that follows the last contents line is the act restart, not the section body.
_ACT_RESTART_RE = re.compile(
    r"(?i)^(?:\d{1,4}|whereas\b|first published\b|an act\b|preamble\b|the\s+\[|\[?(?:khyber|north-west)\b)"
)


def _leading_line(chunk: str) -> str:
    for line in (chunk or "").splitlines():
        flat = _flat(line)
        if flat:
            return flat
    return ""


def _first_line_is_toc_stub(chunk: str) -> bool:
    return bool(TOC_STUB_RE.match(_leading_line(chunk)))


def _trim_contents_line_bleed(chunk: str) -> str:
    """A contents line must not swallow the next page's act title.

    ``36. Rules.`` is followed, in the PDF text layer, by the repeated act
    heading. A wrapped operative sentence (``36. Government may ... Rules.`` /
    ``Ideology, by notification``) keeps its continuation.
    """
    lines = (chunk or "").splitlines()
    first_index = next((i for i, line in enumerate(lines) if _flat(line)), None)
    if first_index is None or not TOC_STUB_RE.match(_flat(lines[first_index])):
        return chunk
    rest = _strip_page_furniture("\n".join(lines[first_index + 1 :])).strip()
    if not rest:
        return lines[first_index].strip()
    restart = _flat(rest.splitlines()[0])
    if _ACT_RESTART_RE.match(restart):
        return lines[first_index].strip()
    return chunk


def _toc_title(chunk: str) -> str:
    flat = _flat(chunk)
    match = TOC_STUB_RE.match(flat)
    if not match:
        return ""
    return flat.split(". ", 1)[1].rstrip(".")


def kpcode_content_plain_text(html: str) -> str:
    """Visible text of the act container. Navigation outside ``#content`` is dropped."""
    soup = BeautifulSoup(html or "", "html.parser")
    node = soup.select_one("#content") or soup.body or soup
    for tag in node.find_all(["script", "style", "nav", "header", "footer"]):
        tag.decompose()
    return node.get_text("\n")


def _document_has_contents_list(text: str) -> bool:
    if CONTENTS_RE.search(text or ""):
        return True
    count = 0
    for line in (text or "").splitlines():
        if TOC_STUB_RE.match(_flat(line)):
            count += 1
            if count >= 3:
                return True
    return False


def parse_kpcode_plain_text(text: str, *, statute_name: str = "") -> List[Dict[str, str]]:
    """Split kpcode plain text into sections, preferring operative chunks over contents lines."""
    body = _strip_page_furniture(text or "", statute_name)
    matches = []
    for match in SECTION_START_RE.finditer(body):
        number = match.group(1)
        base = re.match(r"(\d+)", number)
        if base and int(base.group(1)) >= 1800:
            continue
        if base and int(base.group(1)) == 0:
            continue
        matches.append(match)
    titles: Dict[str, str] = {}
    chosen: Dict[str, str] = {}
    order: List[str] = []
    for index, match in enumerate(matches):
        number = match.group(1)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        chunk = _strip_page_furniture(body[match.start() : end].strip(), statute_name)
        chunk = _trim_contents_line_bleed(chunk)
        if not chunk:
            continue
        title = _toc_title(chunk)
        if title:
            titles.setdefault(number, title)
        if number not in chosen:
            chosen[number] = chunk
            order.append(number)
            continue
        previous = chosen[number]
        previous_is_heading = _first_line_is_toc_stub(previous)
        current_is_heading = _first_line_is_toc_stub(chunk)
        if previous_is_heading and not current_is_heading:
            chosen[number] = chunk
        elif not current_is_heading and not previous_is_heading and len(chunk) > len(previous):
            chosen[number] = chunk
        elif current_is_heading and previous_is_heading and len(_flat(chunk)) > len(_flat(previous)):
            chosen[number] = chunk
    bodies = [number for number in order if not section_is_toc_stub(chosen[number])]
    use = bodies if bodies else order
    sections: List[Dict[str, str]] = []
    for number in use:
        chunk = chosen[number]
        title = titles.get(number, "")
        text_body = chunk
        if title and title.lower() not in _flat(chunk).lower():
            text_body = f"{number}. {title}\n{chunk}"
        sections.append(
            {
                "section_number": number,
                "section_title": title,
                "section_text": text_body.strip(),
            }
        )
    return sections


def parse_kpcode_detail_html(html: str, *, statute_name: str = "") -> List[Dict[str, str]]:
    """Sections from a lawDetails-style page. Contents anchors are headings, not the body."""
    return parse_kpcode_plain_text(kpcode_content_plain_text(html), statute_name=statute_name)


def kpcode_detail_html_has_operative_body(html: str, *, statute_name: str = "") -> bool:
    """True when ``#content`` yields sections that are not a contents list."""
    sections = parse_kpcode_detail_html(html or "", statute_name=statute_name)
    return bool(sections) and not kpcode_sections_are_toc_stubs(sections)


def extract_kpcode_statute_sections(
    *,
    html: Optional[str],
    text: Optional[str],
    statute_name: str = "",
) -> Optional[List[Dict[str, str]]]:
    """HTML content container first; linked-document plain text when that body is thin.

    Returns None when neither input looks like a kpcode contents page, so the caller
    keeps the generic splitter (pakp gazettes, one-line fixtures).
    """
    html_text = kpcode_content_plain_text(html) if html else ""
    plain = text or ""
    html_is_contents = _document_has_contents_list(html_text)
    plain_is_contents = _document_has_contents_list(plain)
    if not html_is_contents and not plain_is_contents:
        return None
    html_sections = parse_kpcode_plain_text(html_text, statute_name=statute_name) if html_is_contents else []
    if html_sections and not kpcode_sections_are_toc_stubs(html_sections):
        return html_sections
    plain_sections = parse_kpcode_plain_text(plain, statute_name=statute_name) if plain_is_contents else []
    if plain_sections and not kpcode_sections_are_toc_stubs(plain_sections):
        return plain_sections
    if html_sections:
        return html_sections
    if plain_sections:
        return plain_sections
    return []
