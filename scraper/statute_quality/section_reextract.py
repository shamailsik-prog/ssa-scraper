"""PASS/FAIL gate for applying a statute section re-extract.

Distribution-list checks reject an addressee or printing-letter row. They do
not reject a split that glues the whole act into one numbered row. That shape
passed, replaced the stored sections, and went visible (Balochistan Animal
Slaughter Control Act, 2025: 9 sections to one row numbered ``2``).

``gate_section_reextract`` is the shared decision. ``PASS`` may be applied and
may be un-quarantined. ``FAIL`` must not be written and must not clear
``document_quarantine``.

Thresholds, and why:

* ``REEXTRACT_COLLAPSE_RATIO`` is one half, compared as ``after * 2 < before``
  so the cut is exact. 9→5 stays (not strictly under half). 9→4 and 9→1 fail.
  A normal footer strip removes a few junk rows and leaves the count roughly
  stable, which stays at or above half on an act that already has a body.
* ``REEXTRACT_FOOTER_STRIP_MAX_DROP`` is 3. A drop of 0–3 rows does not fail
  the count check even if the ratio trips (a short act losing only its
  addressee lines). 9→1 drops 8, so it still fails.
* ``REEXTRACT_MEGA_SECTION_CHARS`` is 5000. Any stored re-extract row whose
  stripped text is at least that long fails, including a single section that
  holds extent through repeal. A section 2 that is only a definitions
  catalogue may pass that 5000 cut and still fail above
  ``REEXTRACT_DEFINITION_CATALOGUE_MAX_CHARS`` (10000). The catalogue needs
  the opening formula, at least one quoted clause marker per
  ``REEXTRACT_DEFINITION_CLAUSE_DENSITY_CHARS`` (600) characters, and no
  later section heading or repeal. A heading may omit its period
  (``3 Establishment``). A definition clause ``1N) "`` is not a heading.
  The North-West Frontier Province Urban Planning Act, 1975 definitions are
  about 8000 characters once clause ``(2)`` is no longer split off as a
  fake section 12, so they stay inside the catalogue cap. That same text
  stored under number 12 still fails. The first ingest of a statute has no stored
  body (``before`` empty) and is not this gate: there is nothing to collapse
  and nothing to un-quarantine. Deleting stored rows and then promoting is
  not a first ingest. Pass those rows through ``reextract_before_sections``
  so this function still sees them. The 5000-character cut, the half-count
  cut, and the footer-strip allowance are unchanged.
* A real section 1 (not a distribution-list designation) that is present
  before and absent after fails. A footer-only ``1. The Principal Secretary…``
  may disappear.
* Tariff schedule rows, income-tax rate rows, and unheaded sub-rows such as
  ``4. Rolled…`` are not part of the collapse baseline. Finance Act 1985's
  stored body is eleven of those rows. A five-section re-extract is not a
  collapse against that set. An amending or surcharge section that carries
  the schedule in its body may pass the 5000-character cut. A later real
  heading inside that body still fails. An amending Finance Act has no
  Definitions section, and this gate does not require one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from scraper.parsers.statute_parser import (
    is_tariff_schedule_section,
    section_text_opens_unheaded_tariff_subrow,
    section_text_starts_with_tariff_schedule_row,
)
from scraper.statute_quality.distribution_list import is_distribution_addressee_section

REEXTRACT_COLLAPSE_RATIO = 0.5
REEXTRACT_FOOTER_STRIP_MAX_DROP = 3
REEXTRACT_MEGA_SECTION_CHARS = 5000
# A pure definitions section may pass the 5000 cut. Past this length it is a
# mega section even when every other catalogue check holds.
REEXTRACT_DEFINITION_CATALOGUE_MAX_CHARS = 10_000
# One quoted clause marker per this many characters. Thinner text is not a catalogue.
REEXTRACT_DEFINITION_CLAUSE_DENSITY_CHARS = 600

REASON_COLLAPSE_COUNT = "reextract_collapse_count"
REASON_MEGA_SECTION = "reextract_mega_section"
REASON_MISSING_SECTION_1 = "reextract_missing_section_1"

_SECTION_ONE_PREFIX_RE = re.compile(r"(?i)^(?:section|sec\.?|s\.?|article|art\.?)\s+")


@dataclass(frozen=True)
class SectionReextractGate:
    verdict: str
    reason: str
    section_count_before: int
    section_count_after: int

    @property
    def may_apply(self) -> bool:
        return self.verdict == "PASS"

    @property
    def may_unquarantine(self) -> bool:
        return self.verdict == "PASS"


def _parseable_rows(sections: Sequence[Any] | None) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for item in sections or []:
        if not isinstance(item, dict):
            continue
        number = str(item.get("section_number") or "").strip()
        text = str(item.get("section_text") or "").strip()
        if number and text:
            rows.append((number, text))
    return rows


def _is_section_one(number: str) -> bool:
    cleaned = _SECTION_ONE_PREFIX_RE.sub("", (number or "").strip())
    cleaned = cleaned.rstrip(".").strip()
    return cleaned in {"1", "01"}


def _has_real_section_one(rows: Sequence[tuple[str, str]]) -> bool:
    for number, text in rows:
        if _is_section_one(number) and not is_distribution_addressee_section(text):
            return True
    return False


def _count_collapsed(before_count: int, after_count: int) -> bool:
    """True when the new split keeps strictly less than half, past a footer strip.

    ``REEXTRACT_COLLAPSE_RATIO`` is 1/2. ``after < before * ratio`` is that cut:
    9→5 passes, 9→4 fails. A drop of ``REEXTRACT_FOOTER_STRIP_MAX_DROP`` or fewer
    is the junk-row allowance.
    """
    if before_count <= 0:
        return False
    drop = before_count - after_count
    if drop <= REEXTRACT_FOOTER_STRIP_MAX_DROP:
        return False
    return after_count < before_count * REEXTRACT_COLLAPSE_RATIO


_DEFINITIONS_SECTION_RE = re.compile(
    r"(?is)^\s*(?:section\s+|s\.\s*)?2\s*[\.\)\-:]\s*.{0,180}?"
    r"(?:\bdefi\w{3,16}\b|\bin\s+this\s+act\s*,?\s*unless\b)"
)
# Period optional, so ``3 Establishment`` is a heading. ``1N) "term"`` is a
# definition clause and is excluded below, not by this pattern.
_EMBEDDED_SECTION_HEADING_RE = re.compile(
    r"(?m)^[ \t]*\d{1,3}[A-Z]?[ \t]*[.,:\-)]?[ \t]+[A-Z][a-z]{2,}"
)
_DEFINITION_CLAUSE_LINE_RE = re.compile(
    r"^[ \t]*(?:\(\s*(?:\d{1,2}|[lI])\s*\)|1\d{1,2}\))[ \t]+[\"'“”‘’:]"
)
_REPEAL_OPERATIVE_RE = re.compile(r"(?i)\bhereby\s+repealed\b")
_DEFINITION_CLAUSE_RE = re.compile(
    r"(?m)(?:\(\s*(?:\d{1,2}|[lI])\s*\)|(?<![\d(])1\d{1,2}\))[ \t]+[\"'“”‘’:]"
)


def _is_section_two(number: str) -> bool:
    cleaned = _SECTION_ONE_PREFIX_RE.sub("", (number or "").strip())
    cleaned = cleaned.rstrip(".").strip()
    return cleaned in {"2", "02"}


def _has_embedded_section_heading(body: str) -> bool:
    """True when a later line reads as a section heading, including a missing period.

    The opening ``2. Definitions`` line is skipped. A definition clause
    ``(2) "term"`` or ``12) "term"`` is not a heading.
    """
    for match in _EMBEDDED_SECTION_HEADING_RE.finditer(body):
        if match.start() <= 40:
            continue
        line_start = body.rfind("\n", 0, match.start()) + 1
        if _DEFINITION_CLAUSE_LINE_RE.match(body[line_start:]):
            continue
        return True
    return False


def _is_definition_catalogue(number: str, text: str) -> bool:
    """True when a long section 2 is only the definitions list.

    Clause ``(2)`` OCR'd as ``12)`` used to be its own mega row. Folding it
    back leaves one definitions section over 5000 characters. The catalogue
    may pass that cut up to ``REEXTRACT_DEFINITION_CATALOGUE_MAX_CHARS``.
    It needs about one quoted clause per 600 characters. A later heading,
    including ``3 Establishment`` with no period, or a repeal, is a collapsed act.
    """
    body = text or ""
    if len(body) > REEXTRACT_DEFINITION_CATALOGUE_MAX_CHARS:
        return False
    if not _is_section_two(number) or not _DEFINITIONS_SECTION_RE.search(body):
        return False
    clauses = _DEFINITION_CLAUSE_RE.findall(body)
    if len(clauses) < 4:
        return False
    if len(clauses) * REEXTRACT_DEFINITION_CLAUSE_DENSITY_CHARS < len(body):
        return False
    if _REPEAL_OPERATIVE_RE.search(body):
        return False
    return not _has_embedded_section_heading(body)


# Amendment, or a short marginal note that names a surcharge, closed by the
# statute dash. A later mention of either word does not qualify.
_AMENDING_SCHEDULE_HEADING_RE = re.compile(
    r"(?is)^\s*\d{1,3}[A-Z]?\s*[\.\)\]]\s*"
    r"(?:amendment\b|(?:\S+\s+){0,6}\bsurcharge\b)"
    r".{0,180}?(?:\.—|\.--|—|–|\.-|--)"
)


def _body_contains_schedule_row(body: str) -> bool:
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if section_text_starts_with_tariff_schedule_row(stripped):
            return True
        if section_text_opens_unheaded_tariff_subrow(stripped):
            return True
    return False


def _has_later_real_heading(body: str) -> bool:
    """A later section heading. Schedule rows and tariff sub-rows are not one."""
    for match in _EMBEDDED_SECTION_HEADING_RE.finditer(body):
        if match.start() <= 40:
            continue
        line_start = body.rfind("\n", 0, match.start()) + 1
        if _DEFINITION_CLAUSE_LINE_RE.match(body[line_start:]):
            continue
        line_end = body.find("\n", match.start())
        line = body[line_start: line_end if line_end != -1 else len(body)].strip()
        if section_text_starts_with_tariff_schedule_row(line):
            continue
        if section_text_opens_unheaded_tariff_subrow(line):
            continue
        return True
    return False


def _is_schedule_bearing_amending_section(number: str, text: str) -> bool:
    """An Amendment or surcharge section may hold its schedule and pass 5000.

    The opening line has to be that heading, closed by a marginal dash.
    A rate row or an HS row inside explains the length. A later real heading
    means the section swallowed the next one.
    """
    if not str(number or "").strip():
        return False
    body = text or ""
    if not _AMENDING_SCHEDULE_HEADING_RE.match(body.strip()):
        return False
    if not _body_contains_schedule_row(body):
        return False
    return not _has_later_real_heading(body)


def _row_is_tariff_baseline_noise(number: str, text: str) -> bool:
    """A stored schedule row is not a section the collapse ratio should defend."""
    if section_text_starts_with_tariff_schedule_row(text):
        return True
    if is_tariff_schedule_section(number, text):
        return True
    return section_text_opens_unheaded_tariff_subrow(text)


def _collapse_baseline(rows: Sequence[tuple[str, str]]) -> list[tuple[str, str]]:
    return [row for row in rows if not _row_is_tariff_baseline_noise(row[0], row[1])]


def _has_mega_section(rows: Sequence[tuple[str, str]]) -> bool:
    for number, text in rows:
        if len(text) < REEXTRACT_MEGA_SECTION_CHARS:
            continue
        if _is_definition_catalogue(number, text):
            continue
        if _is_schedule_bearing_amending_section(number, text):
            continue
        return True
    return False


def _merge_row(item: Any) -> dict[str, Any] | None:
    parsed = _parseable_rows([item])
    if not parsed:
        return None
    number, text = parsed[0]
    row: dict[str, Any] = {"section_number": number, "section_text": text}
    if isinstance(item, dict) and item.get("sort_key") is not None:
        row["sort_key"] = item["sort_key"]
    return row


def merge_reextract_rows(*groups: Sequence[Any] | None) -> list[dict[str, Any]]:
    """Merge section rows by ``section_number``. Later groups win on text.

    A number that appears only in an earlier group stays. ``sort_key`` from
    an earlier group is kept when the winner does not carry one.
    """
    ordered: list[str] = []
    by_number: dict[str, dict[str, Any]] = {}
    for group in groups:
        for item in group or []:
            row = _merge_row(item)
            if row is None:
                continue
            number = str(row["section_number"])
            if number not in by_number:
                ordered.append(number)
                by_number[number] = row
                continue
            previous = by_number[number]
            if "sort_key" not in row and "sort_key" in previous:
                row["sort_key"] = previous["sort_key"]
            by_number[number] = row
    return [by_number[number] for number in ordered]


def reextract_before_sections(
    stored_sections: Sequence[Any] | None,
    prior_sections: Sequence[Any] | None = None,
    snapshot_sections: Sequence[Any] | None = None,
) -> list[Any]:
    """Rows the gate must compare against.

    Capture, explicit pre-delete rows, and live rows are merged by section
    number. Live text wins. Explicit prior wins over the capture. A partial
    delete that leaves section 1 in place still counts the captured sections.
    An empty result is a first fill, which ``gate_section_reextract`` PASSes.
    """
    return merge_reextract_rows(snapshot_sections, prior_sections, stored_sections)


def gate_section_reextract(
    before_sections: Sequence[Any] | None,
    after_sections: Sequence[Any] | None,
) -> SectionReextractGate:
    """Compare stored sections to a candidate re-extract.

    Empty ``before`` is a first ingest, not a re-extract: the verdict is PASS.
    """
    before_rows = _parseable_rows(before_sections)
    after_rows = _parseable_rows(after_sections)
    # An empty stored body is a first ingest. Schedule rows left over from a
    # tariff-polluted parse are not that body: they drop out of the baseline
    # and the mega check still runs.
    raw_before = len(before_rows)
    before_count = len(_collapse_baseline(before_rows))
    after_count = len(after_rows)
    if raw_before == 0:
        return SectionReextractGate("PASS", "", before_count, after_count)

    reason = ""
    if _count_collapsed(before_count, after_count):
        reason = REASON_COLLAPSE_COUNT
    elif _has_mega_section(after_rows):
        reason = REASON_MEGA_SECTION
    elif _has_real_section_one(before_rows) and not _has_real_section_one(after_rows):
        reason = REASON_MISSING_SECTION_1
    if reason:
        return SectionReextractGate("FAIL", reason, before_count, after_count)
    return SectionReextractGate("PASS", "", before_count, after_count)
