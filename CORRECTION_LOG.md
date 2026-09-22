# CORRECTION_LOG.md — SIKANDER AI Corpus Service

Written before coding, as required by Integration Amendment D-15 §25 step 5.
Branch: `claude/scrapegraphai-mcp-plugins-0dh85u`. Base inventory taken on 10 September 2026.

## 0. Governing documents — what was available

| Document | Required by | Status at start |
|---|---|---|
| `SIKANDER_AI_SCRAPER_SCRAPEGRAPH_INTEGRATED_MASTER_COMMAND.md` (Amendment D-15) | — | **Present** (operator upload, two identical copies) |
| `CURSOR_COMMAND_SCRAPER_REPO.md` | Amendment §25 step 2 | **Present** (pushed to this branch, commit `0723247`) |
| `FINAL_SCRAPER_PROMPT_3.md` (governing corpus contract) | Amendment §25 step 1, §22, §23, §24 | **ABSENT** — not in this repository, not in any of the owner's six GitHub repositories, not in the uploads |
| `SIKANDER_AI_Scraper_Plain_English_11Sep2026.pdf` | Amendment §25 step 3 | **ABSENT as PDF**; the operator pushed `docs/SIKANDER_AI_Scraper_Plain_English_REFERENCE.md` (its textual counterpart) mid-build, commit `8de7151`. Applied: archive layout `Citations/<reporter>/<year>`, `Unreported/<court>/<year>`, `Statutes/<jurisdiction>/<Act>`, `Instruments/<year>`, `_index/`; pacing guards `LOGIN_DELAY_MIN/MAX`, `PAGES_PER_HOUR/DAY`; Tier 4 re-checks high-yield vocabulary; dashboard last/next run, saved today, found/estimated/missing, archive test control. |
| `docs/blackletter-annex-b-conformance.md` (claude-code-workspace, commit `9d5e25d`) | Not named by the amendment, but it records Layer 17 Annex B corrections B-1…B-10 against the earlier build | **Present** — used as the authoritative statement of the contract tables and source list |

### Decision recorded

The operator instructed the run to proceed. FINAL_SCRAPER_PROMPT_3.md is therefore
**reconstructed** from (a) every requirement the amendment attributes to it, (b) every
requirement the Cursor command attributes to it (Sections 6, 9, 9A, 14, 15), and (c) the
Annex B conformance record. Every reconstructed requirement is listed in §3 below and is
implemented; nothing in the reconstruction is presented as the verbatim governing text.
Where the governing text would have supplied a firm value (subscribed reporters, earliest
year, region, mirror targets, credentials) the value is left blank, shown as NOT CONFIGURED,
and the most conservative default is used and logged, exactly as the Cursor command directs.

Also pushed mid-build (commit range `326d143..8de7151`): `CLAUDE_CLOUD_START_HERE.md`, `SCRAPEGRAPH_PACKAGE_README.md`, `scrapegraph.env.example`, `requirements-scrapegraph.txt`, `mcp/scrapegraph.example.json`, and the master command itself. `requirements-scrapegraph.txt` asked for `scrapegraph-py>=2.1.0`; no 2.x exists on PyPI, so it is pinned to the tested 1.47.0 and the file records why.

## 1. EXISTING (copied from `claude-code-workspace/blackletter`, PR #1, commit `6fd9466`)

| File | Lines | Assessment |
|---|---|---|
| `scraper/config.py` | 447 | Usable base. pydantic-settings, Fernet key validation, env-var set preserved. Contains `PLS_USER/PLS_PASS` automated-login fields (Annex B-1 breach) — retained as accepted-but-inert names, never used for login. |
| `scraper/database.py` | 67 | Usable base (async engine, `run_async`, `init_db`). No roles, no migrations runner. |
| `scraper/models.py` | 768 | Pre-Annex-A schema (`case_law_master`, `statutes_master`, `limitation_periods`). Replaced by Annex A contract tables (B-3), `limitation_periods` dropped (B-4), `statute_section_version` added (B-5), `bench_size/bench_type` (B-10). |
| `scraper/parsers/citation_extractor.py` | 712 | **Kept.** Deterministic CITATION_PATTERNS, court canonicalisation, statute recognition, `score_confidence`. Authoritative over any AI output per Amendment §1-D, §7-E. |
| `scraper/parsers/statute_parser.py` | 771 | **Kept.** Five section-splitting strategies, `detect_statute_name`, `parse_statute_document`. |
| `scraper/parsers/pdf_extractor.py` | 510 | **Kept.** pdfplumber + Tesseract OCR fallback, streamed download, magic-byte validation. |
| `scraper/parsers/pdf_writer.py` | 48 | **Kept, amended.** Rendered corpus PDF now carries the mandatory "CORPUS RENDERED COPY — NOT AN ORIGINAL SOURCE PDF" label (Amendment §12, §19). |
| `scraper/parsers/text_cleaner.py` | 39 | **Kept.** |
| `scraper/tasks/celery_app.py` | 41 | Rewritten: queues `scraper`, `login_session` (concurrency 1), `embeddings`; new beat entries. |
| `scraper/tasks/dispatcher.py` | 51 | Rewritten: routes by `access_method`/source, runs raw-first pipeline. |
| `scraper/tasks/pakistanlawsite.py` | 170 | Rewritten: was httpx form-login + year walk with env passwords. Now Playwright storage-state session, four tiers, frontier/coverage. |
| `scraper/tasks/pakistancode.py` | 58 | Rewritten around raw-first staging + hybrid extraction + versioned sections. |
| `scraper/tasks/nasirlawsite.py` | 42 | Rewritten around raw-first staging + hybrid extraction. |
| `scraper/tasks/promotion.py` | 56 | Rewritten: validation, dedupe by canonical identity and content hash, promote/quarantine into Annex A tables. |
| `scraper/tasks/embeddings.py` | 55 | Rewritten: EMBEDDING_MODEL / EMBEDDING_DIM from env, corpus_metadata check, refuse on mismatch (B-6). |
| `scraper/routers/sources.py` | 31 | Extended: extraction-settings, access method, slot states; credential card removed (B-8). |
| `scraper/templates/dashboard.html` | 161 | Rewritten: Sources / Coverage / Check viewer / Review queue / Archive / ScrapeGraph / Health / Human login. |
| `scraper/main.py` | 79 | Rewritten. |
| `docker-compose.yml`, `Dockerfile`, `requirements.txt`, `.env.example`, `init.sql`, `pytest.ini`, `deploy.sh` | — | Rewritten / extended. |
| `tests/test_smoke.py`, `tests/conftest.py` | 278 / 28 | Replaced by the full suite (§5). |

## 2. MISSING (not in the base at all)

`scraper/fetchers.py`, `scraper/security.py`, `scraper/auth/session_manager.py`,
`scraper/auth/browser_login.py`, `scraper/parsers/bench_parser.py`, `scraper/tasks/search_map.py`,
`scraper/tasks/superior_courts.py`, `scraper/tasks/legislatures.py`, `scraper/tasks/treatment.py`,
`scraper/tasks/archive_mirror.py`, `scraper/storage/*` (seven adapters + reconcile),
`scraper/routers/coverage.py`, `scraper/routers/review.py`, `scraper/routers/sessions.py`,
`scraper/routers/archive.py`, `scraper/routers/scrapegraph.py`, a real `migrations/001_initial.py`,
`migrations/002_scrapegraph_integration.py`, every table in Annex A, `crawl_frontier`,
`crawl_coverage`, `search_form_map`, `browser_session_slots`, `extraction_audit`,
`scrapegraph_cache`, `archive_targets`, `archive_objects`, `source_provenance`, `corpus_metadata`,
the `sikander_reader` role and grants, the whole `scraper/extractors/` subsystem.

## 3. INCOMPLETE (present but not to contract)

- `migrations/001_initial.py` was a one-line placeholder.
- `routers/export.py` exported `full_text` of every row with no login_session guard.
- `tasks/supremecourt.py`, `tasks/shc.py`, `tasks/nationalassembly.py`: hard-coded limits (`[:20]`), no raw-first provenance, no robots/allow-list, stored into the pre-contract tables. Merged into `superior_courts.py` / `legislatures.py`.
- `tasks/cross_reference.py`: `is_landmark` at `citation_count >= 10`; no treatment rows. Replaced by `treatment.py` (B-7); `citation_count` stays a derived statistic.
- Dashboard had Start/Stop only; no Coverage, Check viewer, Review queue, Archive screens.

## 4. CONFLICTING (base vs. contract)

| Base behaviour | Contract | Resolution |
|---|---|---|
| Automated form login with env passwords, two seats, slot failover | B-1; Cursor cmd §5: `ALLOW_LOGIN_SCRAPING` only with `ENVIRONMENT=chambers`; human login only; no CAPTCHA solving; no rotation past a block; no `try_recover_primary` | `credential_manager.py`, `workers/harvester.py` **removed**. Replaced by `auth/session_manager.py` + `auth/browser_login.py`. |
| Google Drive outbox only (`workers/drive_sync.py`) | §9A seven archive target types, write-once, per-target isolation, `_index` CSVs, `reconcile_storage` | `drive_sync.py` **removed**; `storage/` package added. |
| `limitation_periods` table with legal-rule seed rows | B-4: service holds no legal rules | Dropped. |
| `Vector(1536)` hard-coded; model hard-coded | B-6 | `EMBEDDING_DIM` drives the column; model/dim written to `corpus_metadata`; mismatch refuses to run. |
| Six sources | B-2: add LHC, PHC, BHC, IHC, FSC; provincial assemblies and Gazette | Sixteen sources seeded with `access_method`, `extraction_mode`, allow-lists. |
| `bench_type` free text | B-10 | `bench_size` integer + `bench_type ∈ {single, division, full, larger}` via `parsers/bench_parser.py`. |
| Worker wrote PDF "originals" with reportlab | §12: never recreate a fake original; rendered copies labelled | Original bytes preserved when a PDF exists; HTML-only judgments rendered with explicit "corpus rendered copy" label. |

## 5. SCRAPEGRAPH ADDITIONS (Amendment D-15)

`scraper/extractors/__init__.py`, `schemas.py`, `scrapegraph_base.py`, `scrapegraph_managed.py`,
`scrapegraph_local.py`, `hybrid_extractor.py`, `validation.py`, `cache.py`, `prompts.py`,
`deterministic.py`; tables `extraction_audit`, `scrapegraph_cache`; per-source
`ai_extract_enabled`, `extraction_mode`, `extraction_min_confidence`, `scrapegraph_schema_version`;
fifteen `SGAI_*` settings with validators; routers `/admin/scrapegraph/status`,
`/admin/scrapegraph/test-public`, `/admin/scrapegraph/usage`,
`/admin/sources/{name}/extraction-settings`; `docs/MCP_DEVELOPMENT.md`; tests
`tests/test_extractors_*.py`, `tests/test_privacy.py`, `tests/test_cost_cache.py`.

SDK note: PyPI's newest `scrapegraph-py` at build time is 1.47.0 (no 2.x published). The
1.47 client exposes `smartscraper(website_url|website_html|website_markdown, output_schema)`,
`scrape`, `markdownify`, `crawl`, `searchscraper`, `get_credits` and scheduled jobs (monitor).
`scrapegraph_managed.py` isolates the SDK surface behind one adapter so the v2 rename is a
one-file change. The `cookies`, `headers` and `stealth` parameters are **never** passed.

## 6. Reconstructed Section 15 — the 33 required files

| # | File | # | File |
|---|---|---|---|
| 1 | `scraper/__init__.py` | 18 | `scraper/tasks/search_map.py` |
| 2 | `scraper/config.py` | 19 | `scraper/tasks/superior_courts.py` |
| 3 | `scraper/database.py` | 20 | `scraper/tasks/pakistancode.py` |
| 4 | `scraper/models.py` | 21 | `scraper/tasks/legislatures.py` |
| 5 | `scraper/fetchers.py` | 22 | `scraper/tasks/nasirlawsite.py` |
| 6 | `scraper/security.py` | 23 | `scraper/tasks/promotion.py` |
| 7 | `scraper/auth/session_manager.py` | 24 | `scraper/tasks/treatment.py` |
| 8 | `scraper/auth/browser_login.py` | 25 | `scraper/tasks/embeddings.py` |
| 9 | `scraper/parsers/citation_extractor.py` | 26 | `scraper/storage/archive.py` |
| 10 | `scraper/parsers/statute_parser.py` | 27 | `scraper/routers/sources.py` |
| 11 | `scraper/parsers/text_cleaner.py` | 28 | `scraper/routers/coverage.py` |
| 12 | `scraper/parsers/pdf_extractor.py` | 29 | `scraper/routers/review.py` |
| 13 | `scraper/parsers/pdf_writer.py` | 30 | `scraper/routers/export.py` |
| 14 | `scraper/parsers/bench_parser.py` | 31 | `scraper/templates/dashboard.html` |
| 15 | `scraper/tasks/celery_app.py` | 32 | `migrations/001_initial.py` |
| 16 | `scraper/tasks/dispatcher.py` | 33 | `README.md` |
| 17 | `scraper/tasks/pakistanlawsite.py` | | |

Infrastructure files required alongside them: `docker-compose.yml`, `Dockerfile`,
`requirements.txt`, `.env.example`, `init.sql`, `pytest.ini`, `tests/*`.


## 7. Throughput audit, 22 September 2026

`docs/AUDIT_2026-09-22.md` records the line-by-line audit made after the corpus stalled at ~450
citations. Fourteen findings (F1–F14) are listed there with the amendment that introduced or left each
one, the fix applied and the regression test that pins it. The contract rules (raw-first, human login
only, no bypass, HALT on explicit block, login-session material never sent to a managed engine) are
unchanged; the findings concern scheduling, pacing, batch selection and page classification.
