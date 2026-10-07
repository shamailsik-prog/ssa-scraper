"""Read a statute from its official text using the table of contents as the checklist.

PakistanCode (and most official consolidated texts) print a CONTENTS list before the body:
"1. Short title, extent and commencement", "2. Interpretation clause", ... The body then repeats
each heading as "1. Short title, extent and commencement.___(1) This Act may be called ...".

The contents list is the ground truth for what the statute contains. This reader:

1. takes the statute's name from the official listing title, never from a sentence in the text
   ("This Act may be called ..." is how 2026 statutes were misnamed);
2. reads the contents list (number + heading, joined across wrapped lines, "[Omitted]" noted);
3. finds every listed section in the body, in order, and cuts the body at those headings;
4. strips page furniture ("Page 5 of 21") and amendment footnotes ("1Subs. by ...") from the text
   and keeps the footnotes beside the section;
5. verifies: the title matches the document, every listed (not omitted) section was found, and
   each has real text. `verified` is True only when all three hold; anything else is reported, so
   an unverified statute is held for review instead of entering the corpus half-read.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

CONTENTS_RE = re.compile(r"(?im)^\s*C\s*O\s*N\s*T\s*E\s*N\s*T\s*S\s*$")
PAGE_RE = re.compile(r"(?im)^\s*Page\s+\d+\s+of\s+\d+\s*$")
# "1Subs. by ...", "23Omitted by ...", "4Ins. by ...": footnote number glued to the amendment verb.
FOOTNOTE_RE = re.compile(
    r"(?m)^\s*\d{1,3}\s?(?:Subs|Ins|Omitted|Added|Rep|Renumbered|The words|Proviso|Explanation|For the|Vide|Section|Clause|Sub-section|Now|See|Amended|Inserted|Substituted)\b.*$"
)
ENTRY_RE = re.compile(r"^\s*(\d{1,4}[A-Z]{0,2})(?![A-Za-z])(?:\s*\.\s*|\s+)([A-Z\[(].+?)\s*$")
# Lines that end the contents list: the title repeated, the Act number, the preamble.
PREAMBLE_RE = re.compile(r"(?i)^\s*(\d{0,3}\[?\s*)?(THE\s+.+(ACT|ORDINANCE|ORDER|REGULATIONS?|RULES),?\s*\d{4}|ACT\s+NO\b|ORDINANCE\s+NO\b|P\.?\s*O\.?\s+NO\b|AN\s+ACT\b|AN\s+ORDINANCE\b|WHEREAS\b|\[\s*\d{1,2}(st|nd|rd|th)\s)")
# A body heading may carry an amendment marker: "4[3. Appointment ...", "1[33A. ...".
MARK = r"(?:\d{0,3}\s*\[\s*)?"
UNDERSCORE_RULE_RE = re.compile(r"^\s*_{3,}\s*$")
STRUCTURE_RE = re.compile(r"(?i)^\s*(PART|CHAPTER|SCHEDULE|THE\s+SCHEDULE|APPENDIX|FORM)\b|^\s*\([a-z]{1,4}\)\s+[A-Z]")
OMITTED_RE = re.compile(r"(?i)^\[?\s*(omitted|repealed|deleted)\s*\.?\]?\.?$")
MIN_SECTION_CHARS = 30


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = text.replace("’", "'").replace("‘", "'").replace("´", "'").replace("`", "'")
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


STATUS_NOTE_RE = re.compile(r"\(\s*(repealed|omitted|expired|lapsed|ceased|superseded)[^)]*\)", re.IGNORECASE)


def clean_title(official_title: str) -> str:
    """The listing title without a status note: "Anti-Dumping Duties Ordinance, 2000 (Repealed)" ->
    "Anti-Dumping Duties Ordinance, 2000"."""
    return re.sub(r"\s{2,}", " ", STATUS_NOTE_RE.sub("", official_title or "")).strip(" .")


def title_status(official_title: str) -> Optional[str]:
    m = STATUS_NOTE_RE.search(official_title or "")
    return m.group(1).lower() if m else None


def title_matches(official_title: str, document_text: str) -> bool:
    """Every significant word of the listing title appears near the top of the document."""
    words = [w for w in _norm(clean_title(official_title)).split() if len(w) > 2 or w.isdigit()]
    if not words:
        return False
    head = _norm(document_text[:3000]).replace(" ", "")
    return all(w in head for w in words)


def _contents_block(text: str) -> Optional[Tuple[str, str]]:
    """(contents text, body text) or None when the document has no contents list."""
    m = CONTENTS_RE.search(text)
    if m:
        after = text[m.end():]
    else:
        # Some texts print the list without a "CONTENTS" heading: numbered headings straight after the title.
        head_lines = text.splitlines()[:12]
        if not any(ENTRY_RE.match(line) for line in head_lines):
            return None
        after = text
    entries = [ENTRY_RE.match(line) for line in after.splitlines()]
    first = next((e for e in entries if e), None)
    if not first:
        return None
    # The body starts at the second occurrence of the first entry's heading.
    num, heading = first.group(1), first.group(2)
    probe = _heading_prefix(heading)
    starts = [x.start() for x in re.finditer(rf"(?im)^\s*{re.escape(num)}\s*\.?\s*{probe}", after)]
    if len(starts) < 2:
        return None
    return after[: starts[1]], after[starts[1]:]


def _num(number: str) -> str:
    """A section number as a pattern; a letter suffix may be spaced off in the body ("9 A.")."""
    m = re.fullmatch(r"(\d+)([A-Z]*)", number)
    return rf"{m.group(1)}\s?{m.group(2)}" if m and m.group(2) else re.escape(number)


def _heading_prefix(heading: str, words: int = 3) -> str:
    """The first few words of a heading, as a whitespace-tolerant literal for searching the body."""
    toks = re.findall(r"[A-Za-z0-9'’]+", heading)[:words]
    return r"[\W_]*".join(re.escape(t) for t in toks) if toks else ""


def parse_contents(contents: str) -> List[Dict[str, Any]]:
    """[{number, heading, omitted}] in order, wrapped headings joined."""
    out: List[Dict[str, Any]] = []
    for raw in contents.splitlines():
        line = raw.strip()
        if not line or PAGE_RE.match(line) or UNDERSCORE_RULE_RE.match(line):
            continue
        m = ENTRY_RE.match(line)
        if m and not re.fullmatch(r"(1[89]|20)\d{2}", m.group(1)):
            heading = m.group(2).strip().rstrip(".")
            out.append({"number": m.group(1), "heading": heading, "omitted": bool(OMITTED_RE.match(heading))})
        elif PREAMBLE_RE.match(line):
            out and out[-1].setdefault("closed", True)
        elif out and not out[-1].get("closed") and not STRUCTURE_RE.match(line) and not line.isupper() and len(out[-1]["heading"]) < 200:
            out[-1]["heading"] = f"{out[-1]['heading']} {line}".strip().rstrip(".")
    # A number listed twice (a heading wrapped onto a line starting with a number) keeps the first.
    seen = set()
    unique = []
    for e in out:
        e.pop("closed", None)
        if e["number"] in seen:
            continue
        seen.add(e["number"])
        unique.append(e)
    return unique


def _clean_body(body: str) -> Tuple[str, List[str]]:
    footnotes = [m.group(0).strip() for m in FOOTNOTE_RE.finditer(body)]
    body = FOOTNOTE_RE.sub("", body)
    body = PAGE_RE.sub("", body)
    return body, footnotes


SEQ_HEAD_RE = re.compile(rf"(?m)^\s*{MARK}(\d{{1,4}})\s?([A-Z]{{0,2}})(?![A-Za-z])\s*\.\s*(?=[A-Z\[(])")
HEAD_END_RE = re.compile(r"(_{2,}|\.\s*[—–]|\.\s*-(?=\s*[(A-Z])|:\s*[—–-]|\.\s+(?=\())")


def _read_by_sequence(text: str, report: Dict[str, Any]) -> List[Dict[str, Any]]:
    """No contents list: take numbered headings that run 1, 2, 3 ... (a lettered insert such as 9A may
    follow 9). Anything out of sequence (schedule items, footnotes, years) is not a section."""
    body, _ = _clean_body(text)
    picks: List[Tuple[int, str]] = []
    last_base, last_suffix = 0, ""
    for m in SEQ_HEAD_RE.finditer(body):
        base, suffix = int(m.group(1)), m.group(2)
        if (base == last_base + 1 and not suffix) or (base == last_base and suffix and suffix > last_suffix):
            picks.append((m.start(), f"{base}{suffix}"))
            last_base, last_suffix = base, suffix
        elif not suffix and last_base and last_base + 1 < base <= last_base + 3:
            # A heading the text layer lost (glued to the line above): carry on, but the gap is a
            # missing section, so the statute cannot pass the check.
            report["missing"].extend(str(n) for n in range(last_base + 1, base))
            picks.append((m.start(), f"{base}{suffix}"))
            last_base, last_suffix = base, suffix
    sections = []
    for i, (start, number) in enumerate(picks):
        end = picks[i + 1][0] if i + 1 < len(picks) else len(body)
        chunk = re.sub(rf"^\s*{MARK}{_num(number)}\s*\.\s*", "", body[start:end].strip())
        he = HEAD_END_RE.search(chunk[:300])
        title, section_text = (chunk[: he.start()].strip(" ."), chunk[he.end():].strip()) if he else ("", chunk)
        omitted = bool(OMITTED_RE.match(re.sub(r"\d{1,3}\s*(?=\[)", "", chunk).strip(" .[]*") or "x"))
        if not omitted and len(section_text) < MIN_SECTION_CHARS:
            report["thin"].append(number)
        sections.append({"section_number": number, "section_title": title or None, "section_text": section_text, "omitted": omitted})
    return sections


def read_statute(text: str, official_title: str) -> Dict[str, Any]:
    """Sections and a verification report. See the module docstring."""
    name = clean_title(official_title)
    report: Dict[str, Any] = {"method": "contents", "has_contents": False, "listed": 0, "omitted": 0, "found": 0, "missing": [], "thin": [], "title_ok": False, "verified": False, "status": title_status(official_title)}
    report["title_ok"] = title_matches(official_title, text)
    if len((text or "").strip()) < 200:
        report["reason"] = "no text in the document (a scanned PDF needs OCR)"
        return {"statute_name": name, "sections": [], "verification": report}
    block = _contents_block(text or "")
    if block is None:
        report["method"] = "sequence"
        sections = _read_by_sequence(text, report)
        report["found"] = sum(1 for x in sections if not x["omitted"])
        report["omitted"] = sum(1 for x in sections if x["omitted"])
        first = next((x for x in sections if x["section_number"] == "1"), None)
        short_title_ok = bool(first and title_matches(official_title, first["section_text"]))
        report["verified"] = bool(report["title_ok"] and len(sections) >= 2 and first and not report["thin"] and not report["missing"] and short_title_ok)
        if not report["verified"]:
            why = []
            if not report["title_ok"]:
                why.append("listing title not found at the top of the document")
            if len(sections) < 2 or not first:
                why.append("no numbered sections running from 1")
            elif not short_title_ok:
                why.append("section 1 does not name the statute")
            if report["missing"]:
                why.append(f"numbering skips section(s) {', '.join(report['missing'][:8])}")
            if report["thin"]:
                why.append(f"{len(report['thin'])} sections with almost no text ({', '.join(report['thin'][:8])})")
            report["reason"] = "no contents list; " + ("; ".join(why) or "unverified")
        return {"statute_name": name, "sections": sections, "verification": report}
    contents, body = block
    report["has_contents"] = True
    entries = parse_contents(contents)
    body, footnotes = _clean_body(body)
    report["listed"] = len(entries)
    report["omitted"] = sum(1 for e in entries if e["omitted"])

    # Pass 1: number + the first words of the heading, in order. Pass 2, for what pass 1 missed: the
    # number alone (heading reworded or absent in the body), only between its found neighbours.
    found: Dict[int, int] = {}
    cursor = 0
    for i, e in enumerate(entries):
        for words in (3, 2):
            prefix = _heading_prefix(e["heading"], words)
            if not prefix:
                break
            m = re.compile(rf"(?im)^\s*{MARK}{_num(e['number'])}\s*\.?\s*{prefix}").search(body, cursor)
            if m:
                found[i] = m.start()
                cursor = m.end()
                break
    for i, e in enumerate(entries):
        if i in found:
            continue
        lo = max([found[j] for j in found if j < i], default=0)
        hi = min([found[j] for j in found if j > i], default=len(body))
        m = re.compile(rf"(?im)^\s*{MARK}{_num(e['number'])}\s*\.").search(body, lo + 1 if lo else 0, hi)
        if m:
            found[i] = m.start()
        elif not e["omitted"]:
            report["missing"].append(e["number"])
    positions = sorted((pos, entries[i]) for i, pos in found.items())

    sections: List[Dict[str, Any]] = []
    for i, (start, e) in enumerate(positions):
        end = positions[i + 1][0] if i + 1 < len(positions) else len(body)
        chunk = body[start:end].strip()
        # Text after the heading: "1. Short title ...___(1) This Act ..." -> "(1) This Act ..."
        head_end = re.search(r"(_{2,}|\.\s*[—–-]{1,2}|:\s*[—–-])", chunk[: 40 + len(e["heading"]) * 2])
        section_text = chunk[head_end.end():].strip() if head_end else chunk
        section_text = re.sub(r"[ \t]+\n", "\n", re.sub(r"\n{3,}", "\n\n", section_text))
        bare = re.sub(rf"^\s*{MARK}{_num(e['number'])}\s*\.?", "", section_text)
        bare = re.sub(r"\d{1,3}\s*(?=\[)", "", bare).strip(" .[]*")
        if not e["omitted"] and OMITTED_RE.match(bare or "x"):
            e = {**e, "omitted": True}  # listed in the contents, omitted in the body text
        if not e["omitted"] and len(section_text) < MIN_SECTION_CHARS:
            report["thin"].append(e["number"])
        sections.append({"section_number": e["number"], "section_title": e["heading"], "section_text": section_text if section_text else chunk, "omitted": e["omitted"]})
    report["omitted"] = report["omitted"] + sum(1 for x in sections if x["omitted"]) - sum(1 for e in entries if e["omitted"] and any(x["section_number"] == e["number"] for x in sections))
    report["found"] = sum(1 for x in sections if not x["omitted"])
    report["footnotes"] = len(footnotes)
    expected = report["listed"] - report["omitted"]
    report["verified"] = bool(report["title_ok"] and expected > 0 and not report["missing"] and not report["thin"] and report["found"] == expected)
    if not report["verified"]:
        why = []
        if not report["title_ok"]:
            why.append("listing title not found at the top of the document")
        if report["missing"]:
            why.append(f"{len(report['missing'])} of {expected} listed sections not found ({', '.join(report['missing'][:8])})")
        if report["thin"]:
            why.append(f"{len(report['thin'])} sections with almost no text ({', '.join(report['thin'][:8])})")
        report["reason"] = "; ".join(why) or "no sections listed"
    return {"statute_name": name, "sections": sections, "verification": report, "footnotes": footnotes}
