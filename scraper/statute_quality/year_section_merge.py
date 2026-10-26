"""Plan merge-back of statute sections whose number is a year, not a heading.

Auditor 2026-10-26: ``section_number`` matching ``^(1[89]|20)\\d{2}$`` is a year
split out of citation text (``of 1860)``, ``2001) dt.``, ``1930), hereinafter``).
Re-extraction is not the repair: the statute row does not keep the source PDF
text, and splitting again can drop the tail. Copy the year-section text onto
the previous real section instead. A leading year with no previous real
section is left in place.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlparse

from scraper.parsers.statute_parser import is_exact_year_section_number, year_section_is_bogus_heading

# PostgreSQL POSIX regex. Same class as EXACT_YEAR_SECTION_RE / the auditor pattern.
YEAR_SECTION_NUMBER_SQL = r"^(1[89]|20)[0-9]{2}$"


def spread_host_samples(items: Sequence[Mapping[str, Any]], *, limit: int = 20) -> List[Mapping[str, Any]]:
    """Take up to ``limit`` samples, round-robin by host so one source cannot fill the list."""
    buckets: Dict[str, List[Mapping[str, Any]]] = {}
    order: List[str] = []
    for item in items:
        host = str(item.get("host") or "(none)")
        if host not in buckets:
            buckets[host] = []
            order.append(host)
        buckets[host].append(item)
    spread: List[Mapping[str, Any]] = []
    while len(spread) < limit and any(buckets[host] for host in order):
        for host in order:
            if buckets[host] and len(spread) < limit:
                spread.append(buckets[host].pop(0))
    return spread


def source_host(url: Optional[str]) -> str:
    raw = (url or "").strip()
    if not raw:
        return "(none)"
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or "").strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host or "(none)"


def _order_key(section: Mapping[str, Any]) -> tuple:
    sort_key = section.get("sort_key")
    return (
        sort_key is None,
        sort_key if sort_key is not None else 0,
        str(section.get("created_at") or ""),
        str(section.get("id") or ""),
    )


def plan_year_section_merges(sections: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Return merges for exact year section numbers, in document order.

    Each section mapping needs ``id``, ``section_number``, and ``text``.
    ``sort_key`` / ``created_at`` control order. A year is merged only when
    ``year_section_is_bogus_heading`` rejects it (citation, or off the act's
    sequence). Years that continue the act are ``skipped_in_sequence`` and
    stay. A bogus year with no previous kept section is ``skipped_no_previous``.
    """
    ordered = sorted(sections, key=_order_key)
    sibling_tokens = [str(section.get("section_number") or "") for section in ordered]
    merged: List[Dict[str, str]] = []
    skipped_no_previous: List[Dict[str, str]] = []
    skipped_empty: List[Dict[str, str]] = []
    skipped_in_sequence: List[Dict[str, str]] = []
    merged_text_by_target: Dict[str, str] = {}
    last_real: Optional[Mapping[str, Any]] = None

    for index, section in enumerate(ordered):
        number = str(section.get("section_number") or "").strip()
        text = str(section.get("text") or "")
        if not is_exact_year_section_number(number):
            last_real = section
            continue
        entry = {"year_section_id": str(section.get("id")), "year_section_number": number}
        if not text.strip():
            skipped_empty.append(entry)
            continue
        previous = ordered[index - 1] if index else None
        previous_text = str(previous.get("text") or "") if previous is not None else ""
        if not year_section_is_bogus_heading(number, text, previous_text, sibling_tokens):
            # Continues this act's section numbers and is not a citation tail.
            skipped_in_sequence.append(entry)
            last_real = section
            continue
        if last_real is None:
            skipped_no_previous.append(entry)
            continue
        target_id = str(last_real.get("id"))
        base = merged_text_by_target.get(target_id, str(last_real.get("text") or ""))
        merged_text_by_target[target_id] = base.rstrip() + "\n" + text.lstrip()
        merged.append(
            {
                **entry,
                "into_section_id": target_id,
                "into_section_number": str(last_real.get("section_number") or "").strip(),
            }
        )
    return {
        "merged": merged,
        "skipped_no_previous": skipped_no_previous,
        "skipped_empty": skipped_empty,
        "skipped_in_sequence": skipped_in_sequence,
        "merged_text_by_target": merged_text_by_target,
    }
