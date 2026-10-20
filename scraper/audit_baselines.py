"""Numeric baselines from the independent live-database audit of 2026-10-18.

Acceptance checks and dry-run CLIs compare current corpus metrics against these counts.
They are evidence of the production database that day, not live queries.
"""

from __future__ import annotations

from typing import Dict

# Source: Scraper Auditor, 2026-10-18, read-only. 90,039 judgments, 12,618 statutes.
AUDIT_2026_10_18: Dict[str, int] = {
    "judgments_total": 90_039,
    "judgments_no_title": 6_048,
    "judgments_short_text_under_500": 134,
    "judgments_malformed_pld": 187,
    "judgments_mojibake_question_runs": 6_708,
    "judgments_sc_label_hc_header": 20,
    "judgments_case_description_chrome": 115,
    "judgments_no_decision_date": 381,
    "archive_mismatch_or_failed": 8_600,
    "statutes_total": 12_618,
    "statutes_without_sections": 10_303,
    "statutes_with_any_sections": 2_300,
    "statutes_no_year": 588,
    "statutes_duplicate_names": 79,
}

CORE_ACT_PATTERNS = (
    "Constitution",
    "Pakistan Penal Code",
    "Code of Criminal Procedure",
    "Code of Civil Procedure",
    "Customs Act",
    "Income Tax Ordinance",
    "Sales Tax Act",
    "Qanun-e-Shahadat",
    "National Accountability Ordinance",
    "NAB Ordinance",
)


def statute_counts_as_complete(*, section_count: int, sections_with_text: int) -> bool:
    """A statute counts only when it has at least one section with body text."""
    return int(section_count) > 0 and int(sections_with_text) > 0
