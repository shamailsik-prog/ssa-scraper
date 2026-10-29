"""Strip Balochistan-style distribution-list footers before section splitting.

Assembly PDFs end with a "copy forwarded" / "No. …" block whose lines are
numbered like sections:

    1. The Principal Secretary to Governor Balochistan, Quetta.
    6. The System Analyst, Balochistan Provincial Assembly.

Those lines were stored as sections. The number is not always 1–6: the
footer can be only s.1, or only s.5 and s.6, or the same addressees glued
onto the final section after "Copy forwarded". Defect class:
``distribution_list_footer_as_sections``.

The block is a suffix of the extracted text. A secretary named inside an
operative provision stays. The printing-press letter is part of the same
suffix: ``No.PAB/Legis…``, "A copy is forwarded to the Chief Controller",
and "Fifty copies…" / "may please be supplied". Those lines wrap, so "be
sent" and "may please" often sit on their own lines and must still come off
the last section. A final provision that publishes an extra-ordinary issue
stays. Re-extract from the source PDF after this change; do not rewrite the
stored footer rows in place.
"""

from __future__ import annotations

import re

_MAY_PLEASE_RE = re.compile(r"(?i)\bmay\s+please\b")
# Weak copulas show up in forwarding sentences ("be sent", "may please",
# "a copy is forwarded"). They do not make a line operative on their own.
_OPERATIVE_RE = re.compile(r"(?i)\b(?:may|is|are|was|were|be|been)\b")
_STRONG_OPERATIVE_RE = re.compile(
    r"(?i)\b(?:shall|must|means|includes|notwithstanding|hereby|deemed|"
    r"provided\s+that|subject\s+to|publish|appoint|notify|hold|exercise|"
    r"perform|consist|comprise)\b"
)
# A real provision that uses only a weak copula. "may please" and "be sent"
# are not in this list.
_PROVISION_RE = re.compile(
    r"(?i)\b(?:shall|must|means|includes|notwithstanding|hereby|deemed|"
    r"provided\s+that|subject\s+to|publish|appoint|notify|hold|exercise|"
    r"perform|consist|comprise|"
    r"may\s+(?:be\s+called|make|notify|appoint|publish|establish|constitute|by|also|not)|"
    r"is\s+(?:the|hereby|amended|inserted|substituted|repealed|omitted)|"
    r"are\s+(?:hereby|amended|inserted))\b"
)
# Cut points for a forwarding suffix glued onto the last operative sentence.
# "extra-ordinary issue" is deliberately absent: a real final section uses it.
_FORWARDING_ANCHOR_RE = re.compile(
    r"(?i)(?:"
    r"(?:\ba\s+)?cop(?:y|ies)\s+(?:is\s+|are\s+)?forwarded\b"
    r"|\bcopy\s+for\s+information\b"
    r"|\bchief\s+controller\b"
    r"|\bprinting\s+press\b"
    r"|\bfifty\s+copies\b"
    r"|\bproof\s+read"
    r"|\bbefore\s+final\s+printing\b"
    r"|\bmay\s+please\b"
    r"|\bno\.?\s*(?:pab\b|\S*legis)"
    r")"
)
_ADDRESSEE_RE = re.compile(
    r"(?i)^(?:\d+\s*[\.\)\]\:\-]+\s*)?(?:the\s+)?"
    r"(?:"
    r"principal\s+secretary\b"
    r"|chief\s+secretary\b"
    r"|secretary\s*[,.]?\s+government\b"
    r"|secretary\s+to\s+(?:the\s+)?(?:government|governor|chief\s+minister)\b"
    r"|director[\s-]*general\b[^.\n]{0,120}?\b(?:public\s+relations|press\s+information)\b"
    r"|system\s+analyst\b"
    r"|(?:p\.?\s*s\.?|private\s+secretary)\s+to\s+(?:the\s+)?secretary\b"
    r")"
)
_HEADER_RE = re.compile(
    r"(?i)(?:\ba\s+)?cop(?:y|ies)\s+(?:is\s+|are\s+)?forwarded\b|\bcopy\s+for\s+information\b"
)
_REFERENCE_RE = re.compile(r"(?i)^no\.?\s*\S*(?:/|legis)")
_DATED_RE = re.compile(r"(?i)^dated\b")
_SIGNATURE_RE = re.compile(
    r"(?i)^(?:"
    r"[\(\{][^)\n]{2,80}[\)\}]"
    r"|special\s+secretary\b.*$"
    r"|secretary(?:\s*\([^)]{0,40}\))?\s*\.?"
    r")$"
)
_CONTINUATION_RE = re.compile(
    r"(?i)^(?:and\s+)?(?:department|quetta|karachi|islamabad|lahore|peshawar|muzaffarabad|gilgit)\b.{0,80}$"
)
_PRINTING_RE = re.compile(
    r"(?i)(?:printing\s+press|proof\s+read|fifty\s+copies|chief\s+controller|"
    r"for\s+favou?r\s+of\s+publication|extra[\s\-–]*ordinary|ordinary\s+issue|"
    r"final\s+printing|may\s+please|secretariat\s+for\s+record)"
)
# Cues that identify the forwarding letter. "extra-ordinary issue" is not one:
# a real final section uses those words, and a line break can leave them alone.
_FORWARDING_PRINT_RE = re.compile(
    r"(?i)(?:printing\s+press|proof\s+read|fifty\s+copies|chief\s+controller|"
    r"for\s+favou?r\s+of\s+publication|final\s+printing|may\s+please|"
    r"secretariat\s+for\s+record)"
)
_AMBIGUOUS_ISSUE_RE = re.compile(r"(?i)extra[\s\-–]*ordinary|ordinary\s+issue")
_PAGE_RE = re.compile(r"^\d{1,3}$")
_BLOCK_LINE_KINDS = frozenset(
    {"addressee", "header", "reference", "dated", "signature", "page", "continuation", "printing", "blank"}
)
_TRAILING_FURNITURE = frozenset({"blank", "signature", "page"})


def _operative(text: str) -> bool:
    return bool(_OPERATIVE_RE.search(_MAY_PLEASE_RE.sub(" ", text or "")))


def _strong_operative(text: str) -> bool:
    return bool(_STRONG_OPERATIVE_RE.search(text or ""))


def _forwarding_cut_pos(text: str) -> int | None:
    match = _FORWARDING_ANCHOR_RE.search(text or "")
    return match.start() if match else None


def _prefix_has_provision(prefix: str) -> bool:
    return bool(_PROVISION_RE.search(prefix or ""))


def _line_kind(stripped: str) -> str:
    if not stripped:
        return "blank"
    # "must publish … extra-ordinary issue" and "shall be kept" are the Act,
    # even when a printing word sits on the same line. A provision before the
    # glue ("may make regulations. Fifty copies…") stops the walk; the glue is
    # cut afterwards. Wrapped letter lines ("thereof be sent", "may please be
    # supplied") have no such provision, so printing still wins.
    cut = _forwarding_cut_pos(stripped)
    prefix = stripped if cut is None else stripped[:cut]
    if _strong_operative(stripped) or _prefix_has_provision(prefix):
        return "act"
    if _HEADER_RE.search(stripped):
        return "header"
    if _REFERENCE_RE.match(stripped):
        return "reference"
    if _DATED_RE.match(stripped):
        return "dated"
    if _SIGNATURE_RE.match(stripped):
        return "signature"
    if _PAGE_RE.fullmatch(stripped):
        return "page"
    if _PRINTING_RE.search(stripped):
        return "printing"
    if _operative(stripped):
        return "act"
    if _ADDRESSEE_RE.match(stripped):
        return "addressee"
    if _CONTINUATION_RE.match(stripped):
        return "continuation"
    return "act"


def _ambiguous_issue_fragment(stripped: str) -> bool:
    """An extra-ordinary / ordinary-issue line with no forwarding cue."""
    return bool(_AMBIGUOUS_ISSUE_RE.search(stripped)) and not _FORWARDING_PRINT_RE.search(stripped)


def _continues_operative_provision(lines: list[str], index: int) -> bool:
    """True when this issue-fragment continues the operative line above it."""
    if not _ambiguous_issue_fragment(lines[index].strip()):
        return False
    j = index - 1
    while j >= 0:
        above = lines[j].strip()
        if not above:
            j -= 1
            continue
        if _ambiguous_issue_fragment(above):
            j -= 1
            continue
        return _line_kind(above) == "act"
    return False


def _distribution_block_start(lines: list[str]) -> int | None:
    """Line index where a trailing distribution block begins, or None."""
    i = len(lines) - 1
    while i >= 0 and not lines[i].strip():
        i -= 1
    skipped = 0
    while i >= 0 and skipped < 8 and _line_kind(lines[i].strip()) in _TRAILING_FURNITURE:
        i -= 1
        skipped += 1
    if i < 0 or _line_kind(lines[i].strip()) not in _BLOCK_LINE_KINDS:
        return None
    addressees = 0
    forwarding = False
    while i >= 0:
        kind = _line_kind(lines[i].strip())
        if kind not in _BLOCK_LINE_KINDS:
            break
        if kind == "printing" and _continues_operative_provision(lines, i):
            break
        if kind == "addressee":
            addressees += 1
        if kind in {"header", "reference"} or (
            kind == "printing" and _FORWARDING_PRINT_RE.search(lines[i].strip())
        ):
            forwarding = True
        i -= 1
    # One designation is enough. So is the printing letter on its own, or a
    # "Fifty copies…" line sitting under the addressee list.
    if addressees < 1 and not forwarding:
        return None
    return i + 1


def _starts_new_sentence(prefix: str) -> bool:
    stripped = (prefix or "").rstrip()
    if not stripped:
        return True
    return stripped.endswith((".", ":", ";", "!", "?"))


def _tail_is_distribution(tail: str) -> bool:
    """True when text that begins at a forwarding anchor is only the footer."""
    if not tail or len(tail) > 3500 or _strong_operative(tail):
        return False
    saw_forwarding = False
    for line in tail.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        kind = _line_kind(stripped)
        if kind == "act":
            return False
        if kind in {"header", "printing", "reference", "addressee", "dated"}:
            saw_forwarding = True
    return saw_forwarding


def _cut_glued_copy_forwarded(text: str) -> str:
    """Cut a forwarding suffix that shares a line with the last real section.

    The cut is the earliest ``No.PAB/Legis``, "copy is forwarded", Chief
    Controller, or "Fifty copies" anchor whose tail is only the footer. A
    strong verb in the same sentence ("shall … printing press", "must publish
    … extra-ordinary issue") keeps that sentence.
    """
    for match in _FORWARDING_ANCHOR_RE.finditer(text):
        line_start = text.rfind("\n", 0, match.start()) + 1
        same_line_prefix = text[line_start : match.start()]
        if (
            _strong_operative(same_line_prefix) or _prefix_has_provision(same_line_prefix)
        ) and not _starts_new_sentence(same_line_prefix):
            continue
        if not _tail_is_distribution(text[match.start() :]):
            continue
        return text[: match.start()].rstrip()
    return text


def strip_distribution_list_footer(text: str) -> str:
    """Remove a trailing copy-forwarded addressee block. Leave other text unchanged.

    Catches a block of its own lines and the same block glued onto the final
    section, including the Chief Controller printing letter and a trailing
    "Fifty copies" line. A single trailing designation (s.1, or s.5 and s.6)
    is enough.
    """
    if not text:
        return text or ""
    lines = text.splitlines()
    start = _distribution_block_start(lines)
    if start is not None:
        text = "\n".join(lines[:start]).rstrip()
    return _cut_glued_copy_forwarded(text)


def is_distribution_addressee_section(text: str) -> bool:
    """True when a candidate section is only distribution furniture.

    That is a designation line, or the printing-press letter (``No.PAB/Legis``,
    Chief Controller, "Fifty copies") with no operative provision.
    """
    raw = (text or "").strip()
    if not raw or _strong_operative(raw):
        return False
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        return False
    addressees = 0
    forwarding = False
    for line in lines:
        if _prefix_has_provision(line) and _forwarding_cut_pos(line) is None:
            return False
        kind = _line_kind(line)
        if kind == "addressee":
            addressees += 1
            continue
        if kind in {"continuation", "signature", "page", "header", "reference", "dated", "printing"}:
            if kind in {"header", "printing", "reference"}:
                forwarding = True
            continue
        return False
    return addressees >= 1 or forwarding
