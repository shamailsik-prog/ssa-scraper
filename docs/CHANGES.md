# Operational snippets

## RE-APPLY GRANT (reader / `sikander_reader`)

After manual privilege drift on a droplet, re-apply reader grants with **column-level** `SELECT` only.
Never run table-wide `GRANT SELECT ON public.judgment` (or `instrument`, `source_provenance`,
`scraper_staging`, `statutes_staging`). Blocked columns include `raw_html`, `raw_text`, `full_text`,
`headnote*`, `summary`, `ai_json`, `deterministic_json`, `reconciled_json`, `route_json`, `routes`,
`validation_errors`, `statutes_cited`, and any column whose name contains `embedding`
(including `statute_section.embedding`). `statute_section_version.section_text` and
`treatment.evidence_passage` remain readable via ordinary table-wide grants on those tables.

Preferred: restart the `api` service so `ensure_roles()` runs on startup (`init_db`).

Manual SQL (replace role name if customised):

```sql
-- judgment (metadata / identity only)
GRANT SELECT (
  id, canonical_citation, case_title, court_id, court_name, judge_names, bench_size, bench_type,
  decision_date, year, reporter, volume, page_number, docket_number, docket_key, unreported,
  proceeding_type, petitioner, respondent, full_text_hash, citations_cited, access_method,
  source_name, source_url, source_provenance_id, original_document_id, has_original_pdf, language,
  citation_count, confidence_score, quality_flag_urdu_headnote_mojibake, extraction_engine,
  promoted_at, created_at, updated_at
) ON TABLE public.judgment TO sikander_reader;

-- instrument
GRANT SELECT (
  id, type, number, date, title, gazette_ref, full_text_hash, affected_statute_id,
  affected_statute_name, affected_sections, citation_mentions, statute_mentions, jurisdiction,
  source_name, source_url, source_provenance_id, created_at
) ON TABLE public.instrument TO sikander_reader;

-- source_provenance (no route_json / routes)
GRANT SELECT (
  id, source_name, access_method, source_url, content_hash, content_kind, raw_ref, byte_size,
  http_status, fetched_at, is_original_document, document_kind, parent_id, promoted_table,
  promoted_id, created_at
) ON TABLE public.source_provenance TO sikander_reader;

-- scraper_staging (ops / status only — reader normally has no staging access; use for analyst fixes)
GRANT SELECT (
  id, source_id, source_name, access_method, source_url, provenance_id, job_id, content_hash,
  raw_ref, raw_text_hash, pdf_provenance_id, ocr_applied, extracted_citation, extracted_title,
  extracted_court, extracted_year, extracted_decision_date, extraction_engine, status,
  promoted_to_id, quarantine_reason, confidence_score, created_at, updated_at
) ON TABLE public.scraper_staging TO sikander_reader;

-- statutes_staging
GRANT SELECT (
  id, source_id, source_name, access_method, source_url, provenance_id, job_id, content_hash,
  raw_ref, kind, extraction_engine, status, promoted_to_id, quarantine_reason, confidence_score,
  created_at, updated_at
) ON TABLE public.statutes_staging TO sikander_reader;

-- statute_section (no embedding vector)
GRANT SELECT (
  id, statute_id, section_number, section_title, chapter, part, sort_key, current_version_id,
  created_at, updated_at
) ON TABLE public.statute_section TO sikander_reader;
```

Canonical lists are defined in `scraper/reader_grants.py` (`READER_OPS_COLUMN_ALLOWLIST`).
