"""Column-level SELECT grants for roles that must not read raw corpus content via SQL.

Used by ensure_roles() on startup and documented for manual RE-APPLY GRANT on droplets.
Table-wide ``GRANT SELECT`` is forbidden on tables that store page blobs, extraction JSON, or embeddings.
"""

from __future__ import annotations

import re
from typing import Iterable, Mapping, Sequence

# Tables that must never receive table-wide reader SELECT (content / blob columns).
READER_COLUMN_LIMITED_TABLES: frozenset[str] = frozenset(
    {
        "judgment",
        "instrument",
        "source_provenance",
        "scraper_staging",
        "statutes_staging",
    }
)

# Exact column names blocked on column-limited tables (case-insensitive match).
READER_BLOCKED_COLUMNS: frozenset[str] = frozenset(
    {
        "raw_html",
        "raw_text",
        "full_text",
        "summary",
        "ai_json",
        "deterministic_json",
        "reconciled_json",
        "route_json",
        "routes",
        "validation_errors",
        "statutes_cited",
        "embedding",
    }
)

# Any column whose name starts with one of these prefixes is blocked (e.g. headnote*, headnotes).
READER_BLOCKED_COLUMN_PREFIXES: tuple[str, ...] = ("headnote",)


def is_reader_blocked_column(name: str) -> bool:
    lower = name.lower()
    if "embedding" in lower:
        return True
    if lower in READER_BLOCKED_COLUMNS:
        return True
    return any(lower.startswith(prefix) for prefix in READER_BLOCKED_COLUMN_PREFIXES)


def reader_forbids_table_wide_select(table_name: str, column_names: Iterable[str]) -> bool:
    """True when reader must get column-level SELECT (content tables or any blocked column present)."""
    if table_name in READER_COLUMN_LIMITED_TABLES:
        return True
    return any(is_reader_blocked_column(c) for c in column_names)


def reader_select_columns(table_name: str, column_names: Iterable[str]) -> tuple[str, ...]:
    """Columns the reader role may SELECT (metadata/id/status; no blobs or embeddings)."""
    allowed = tuple(c for c in column_names if not is_reader_blocked_column(c))
    if not allowed:
        raise ValueError(f"no reader-selectable columns left on {table_name!r}")
    return allowed


def reader_grant_select_sql(table_ident: str, role_ident: str, columns: Sequence[str]) -> str:
    """``GRANT SELECT (…) ON TABLE … TO role`` — never table-wide on column-limited tables."""
    cols = ", ".join(f'"{c}"' for c in columns)
    return f"GRANT SELECT ({cols}) ON TABLE {table_ident} TO {role_ident}"


def assert_no_table_wide_reader_grant(sql: str, *, table_name: str) -> None:
    """Guard for deploy/ops scripts: reject table-wide SELECT where column grants are required."""
    if table_name not in READER_OPS_COLUMN_ALLOWLIST:
        return
    pattern = re.compile(
        rf"GRANT\s+SELECT\s+ON\s+(?:TABLE\s+)?(?:public\.)?{re.escape(table_name)}\b",
        re.IGNORECASE,
    )
    if pattern.search(sql) and "(" not in sql.split(table_name, 1)[-1].split("TO", 1)[0]:
        raise ValueError(
            f"table-wide GRANT SELECT on {table_name!r} is forbidden; use column list from scraper.reader_grants"
        )


# Frozen allow-lists for ops copy-paste (derived from models; re-run tests if schema drifts).
READER_OPS_COLUMN_ALLOWLIST: Mapping[str, tuple[str, ...]] = {
    "judgment": (
        "id",
        "canonical_citation",
        "case_title",
        "court_id",
        "court_name",
        "judge_names",
        "bench_size",
        "bench_type",
        "decision_date",
        "year",
        "reporter",
        "volume",
        "page_number",
        "docket_number",
        "docket_key",
        "unreported",
        "proceeding_type",
        "petitioner",
        "respondent",
        "full_text_hash",
        "citations_cited",
        "access_method",
        "source_name",
        "source_url",
        "source_provenance_id",
        "original_document_id",
        "has_original_pdf",
        "language",
        "citation_count",
        "confidence_score",
        "quality_flag_urdu_headnote_mojibake",
        "extraction_engine",
        "promoted_at",
        "created_at",
        "updated_at",
    ),
    "instrument": (
        "id",
        "type",
        "number",
        "date",
        "title",
        "gazette_ref",
        "full_text_hash",
        "affected_statute_id",
        "affected_statute_name",
        "affected_sections",
        "citation_mentions",
        "statute_mentions",
        "jurisdiction",
        "source_name",
        "source_url",
        "source_provenance_id",
        "created_at",
    ),
    "source_provenance": (
        "id",
        "source_name",
        "access_method",
        "source_url",
        "content_hash",
        "content_kind",
        "raw_ref",
        "byte_size",
        "http_status",
        "fetched_at",
        "is_original_document",
        "document_kind",
        "parent_id",
        "promoted_table",
        "promoted_id",
        "created_at",
    ),
    "scraper_staging": (
        "id",
        "source_id",
        "source_name",
        "access_method",
        "source_url",
        "provenance_id",
        "job_id",
        "content_hash",
        "raw_ref",
        "raw_text_hash",
        "pdf_provenance_id",
        "ocr_applied",
        "extracted_citation",
        "extracted_title",
        "extracted_court",
        "extracted_year",
        "extracted_decision_date",
        "extraction_engine",
        "status",
        "promoted_to_id",
        "quarantine_reason",
        "confidence_score",
        "created_at",
        "updated_at",
    ),
    "statutes_staging": (
        "id",
        "source_id",
        "source_name",
        "access_method",
        "source_url",
        "provenance_id",
        "job_id",
        "content_hash",
        "raw_ref",
        "kind",
        "extraction_engine",
        "status",
        "promoted_to_id",
        "quarantine_reason",
        "confidence_score",
        "created_at",
        "updated_at",
    ),
    "statute_section": (
        "id",
        "statute_id",
        "section_number",
        "section_title",
        "chapter",
        "part",
        "sort_key",
        "current_version_id",
        "created_at",
        "updated_at",
    ),
}


def ops_reapply_grant_snippet(table_name: str, role: str = "sikander_reader") -> str:
    """Single statement for docs/ops RE-APPLY GRANT (tables that forbid table-wide SELECT)."""
    if table_name not in READER_OPS_COLUMN_ALLOWLIST:
        raise ValueError(f"no ops column allow-list for {table_name!r}")
    cols = READER_OPS_COLUMN_ALLOWLIST[table_name]
    return reader_grant_select_sql(f'"{table_name}"', role, cols)
