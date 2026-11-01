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
  holds extent through repeal. The first ingest of a statute has no stored
  body (``before`` empty) and is not this gate: there is nothing to collapse
  and nothing to un-quarantine. Deleting stored rows and then promoting is
  not a first ingest. Pass those rows through ``reextract_before_sections``
  so this function still sees them. Thresholds above are unchanged.
* A real section 1 (not a distribution-list designation) that is present
  before and absent after fails. A footer-only ``1. The Principal Secretary…``
  may disappear.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Sequence

from scraper.statute_quality.distribution_list import is_distribution_addressee_section

REEXTRACT_COLLAPSE_RATIO = 0.5
REEXTRACT_FOOTER_STRIP_MAX_DROP = 3
REEXTRACT_MEGA_SECTION_CHARS = 5000

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


def _has_mega_section(rows: Sequence[tuple[str, str]]) -> bool:
    return any(len(text) >= REEXTRACT_MEGA_SECTION_CHARS for _, text in rows)


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
    before_count = len(before_rows)
    after_count = len(after_rows)
    if before_count == 0:
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
