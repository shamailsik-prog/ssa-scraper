# Working specification vs. the running service (24 September 2026)

The operator supplied the working specification ("SIKANDER AI CORPUS SERVICE — WORKING SPECIFICATION
AND IMPLEMENTATION PROMPT") on 24 September 2026 with the instruction that where the code differs the
code changes, and on the same day answered the four open questions with "follow the specification
literally". This note records, section by section, where the service stands against the document
after that change, what was removed to match it, and the few additions that remain because they
contradict no rule of the document.

## Holds as specified (verified in code and tests)

| Section | Rule | Where |
|---|---|---|
| 0.1 | Raw first: SHA-256, disk, `source_provenance`, staging before any extraction | `scraper/fetchers.py`, acceptance test 1 |
| 0.2 | Deterministic parsers authoritative; AI fields need raw evidence | `scraper/extractors/hybrid_extractor.py`, validation tests |
| 0.3 | Login-session material never leaves the firm; managed engine refused by code, validator and guard | `config.py` validators, `scripts/guard_scan.sh`, privacy tests |
| 0.4 | No evasion: no CAPTCHA solving, proxies, stealth, slot rotation past a block; a block halts (public sources too, no cooldown retry) | guard scan, `session_manager.halt_source`, `public_pipeline.halt` |
| 0.5 | Credentials never in code, prompts, logs, database or environment; only the encrypted storage state; the human login is the only way a session is created | `browser_login.py`, `session_manager.py`, migration 007 |
| 1 | api, worker-scraper (login_session, concurrency 1), worker-public (scraper + maintenance, concurrency 2), worker-embed, celery-beat, celery-flower, postgres, redis, ollama profile, caddy overlay | `docker-compose.yml`, `docker-compose.cloud.yml` |
| 2 | 17 sources seeded, allow-lists, courts, two slots, state machine | `scraper/database.py` |
| 3.1 | Permission gate `ENVIRONMENT=chambers` + `ALLOW_LOGIN_SCRAPING`; one login-session worker; one Redis lock, TTL 3600, refreshed per page | `config.py`, `SessionLock`, `_charge_page` |
| 3.2 | Streamed human login, focus reporting without values, phone/desktop viewport, "I Agree" and one-login-per-account notes; Complete stores the encrypted state, marks the slot ACTIVE and resumes a PAUSED source | `browser_login.py`, `session_manager.save_storage_state`, dashboard |
| 3.3 | Search-form map from PLS_SEARCH_URL, deterministic introspection, LOCAL engine only, versions, `SEARCH_MAP_UPDATED`, staleness after 5 failures | `tasks/search_map.py`, `PakistanLawSitePipeline.ensure_search_map` |
| 3.4 | Four tiers, keys `t1:`/`t2:`/`t3:`/`t4:`, VOLUME_END_GAP 40, 60 probes per run, TIER3_RETIRE_AFTER 3, top-10 Tier 4 vocab; nothing invented when reporters/years are unset; a row the map cannot serve is retired | `tasks/pakistanlawsite.py` |
| 3.5 | Tiers 2-4 page with `{page, row_index, next_url}` for 10 pages per run; `row_index` flushed after every detail; route on every staged record | same |
| 3.6 | One pacing profile: 4.0-9.0 s, 300/hour, 2,500/day, counters in `source.config_json.pacing`; `PacingBudgetExceeded` ends the run, the source stays ACTIVE | `_charge_page`, pacing test |
| 3.7 | Run order (permitted, lock, current slot, seed, map, 20 rows); page verdicts; same slot after RECONNECT_SECONDS 30, then the alternate slot, then pause; login/verification → NEEDS_HUMAN_LOGIN; block → HALTED, no switch | `ContinuityRunner`, `run()`, tests |
| 3.8 | Deterministic + LOCAL only; managed engine never constructed in the path (guard) | `guard_scan.sh` |
| 4 | Public pipeline: URL policy, robots (5xx defers, disallow retires), 3 retries, 100 MB cap, listing discovery, PDF/OCR, bookkeeping; a block halts | `tasks/public_pipeline.py`, `fetchers.py`, `security.py` |
| 5 | HybridExtractor order, skip-AI rule, cache, budget, sentinel prompt, timeout 120, retries 2, breaker 5/900 s, audit rows, fail-open | `extractors/` |
| 6 | Reconciliation rules, quarantine reasons, promotion dedupe/conflict/no overwrite, statute versions, review queue; promotion every 15 min, 200 rows | `tasks/promotion.py`, `celery_app.py` |
| 7 | Raw tree, archive tree and slugs, seven adapters, write-once ledger, mirror every 30 min / reconcile daily, double flag for login-session rows | `storage/`, `tasks/archive_mirror.py` |
| 8 | Eleven contract tables, `sikander_reader` SELECT-only, `corpus_writer` only writer, embedding identity, external-embedding skip for login-session rows | `database.py`, `tasks/embeddings.py` |
| 9 | Admin key with constant-time compare, WebSocket 4401, endpoints as listed (state actions pause/resume/re_enable/disable), export guard with the three-part confirmation, dashboard tabs | `routers/`, `templates/dashboard.html` |
| 10 | Beat: dispatch every 30 min (`next_scrape_at = now + scrape_frequency_hours`), promotion every 15 min, treatment 24 h, embeddings 5 min, mirror 30 min, reconcile 24 h | `tasks/celery_app.py`, `tasks/dispatcher.py` |
| 11 | Validators: stealth refused, managed-public-only with login scraping, `LOGIN_SESSION_CONCURRENCY` other than 1 refused, PostgreSQL/Redis URLs, reporter codes, delay ranges, positive limits; PLAYWRIGHT_TIMEOUT_MS 30000 default; "Not configured" list | `config.py` |
| 12 | deploy-cloud, derived SSH key, bundle upload, installer, admin-link and server-logs workflows | `.github/workflows`, `cloud/` |
| 13 | Guard scan: stubs, placeholders, keys, solvers, proxies/stealth, `try_recover_primary`, managed engine in the PLS path, tracked `.env` | `scripts/guard_scan.sh`, CI |
| 14 | Acceptance tests run against PostgreSQL 16 + pgvector, Redis 7 and Chromium in CI | `.github/workflows/ci.yml` |

## Removed on 24 September 2026 to match the document

| Section | What the service did until then | What was removed |
|---|---|---|
| 1, 3.1, 11 | Two login-session worker processes, two reporter shards, one browser per login, exclusive per-slot Redis locks, per-slot pacing counters (`LOGIN_SESSION_CONCURRENCY=2`) | Reporter shards, per-slot locks and per-slot counters. One worker, one lock, one `pacing` object. `LOGIN_SESSION_CONCURRENCY` other than 1 is refused; the installer sets a 2 left in `.env` back to 1. |
| 0.5, 3.2, 3.7 | Saved per-slot username/password (encrypted at rest, migration 006); a `recover-login-slots` Beat task re-verified a bounced slot after 15 minutes and, if dead, signed in again with the saved credentials; a paused source resumed on its own | The credential columns (migration 007 drops them and the ciphertext they held), the credential endpoints and autofill, the recovery task and its settings (`LOGIN_AUTO_RECOVER`, `LOGIN_RECOVERY_*`), the `SLOT_RECOVERED`, `SLOT_RECOVERY_FAILED` and `SOURCE_RESUMED` notifications. A bounced slot now waits for a human; a paused source resumes on the next human login Complete or an admin resume. |
| 3.3-3.5 | "Citation-grid surface mode": the authenticated CitationSearch page walked as a 20,568-row table in windows with a row cursor, compact DOM snapshots and an oversize-page guard | The grid walker, `archived_grid_seek.js`, the compact snapshot and oversize guard in `PlaywrightBrowser`, the `PLS_ARCHIVED_GRID_*`, `PLS_CITATION_GRID_*`, `PLS_RUN_MAX_MINUTES`, `PLAYWRIGHT_MAX_HTML_BYTES`, `PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD` settings and the grid progress view. Searches are submitted through the form map and paged, as 3.3-3.5 describe. The committed grid cursor (`citation_grid_cursor*` in the source config) is left in the database untouched. |
| 3.6, 11 | Two pacing profiles selected by a harvest mode (`updates`: the specified numbers; `backfill`: 6-9 s, 450/hour, 200,000/day) | Harvest modes altogether (`scraper/harvest_mode.py`, `HARVEST_MODE`, `HARVEST_AUTO_SWITCH`, `BACKFILL_*`, `UPDATE_CADENCE_HOURS`, the `/admin/sources/harvest-mode` endpoints, the `HARVEST_MODE_SWITCHED` notification). One profile: 4.0-9.0 s, 300/hour, 2,500/day. |
| 10, 1 | Dispatch every 60 s (`DISPATCH_LOOP_SECONDS`), promotion every 5 min with 500 rows on a dedicated `worker-maintenance` | The setting, the service and the cadence. Dispatch every 30 min, promotion every 15 min with 200 rows on `worker-public`. |
| 4.5 | Public sources could retry an explicit block after a cooldown (`auto_retry_on_block`, `BLOCK_RETRY_*`, `SOURCE_BLOCK_COOLDOWN`) | The cooldown retry. A block halts the source for admin review, as 3.7 says. |
| 11 | `PLAYWRIGHT_TIMEOUT_MS` default 90000 | Default 30000. A value set in `.env` is the operator's and is kept. |

The audit report (`docs/AUDIT_2026-09-22.md`, sections 1-13) keeps the measurements that led to each
of those features; they remain the record of what the site did under each setting.

## Additions that remain (no rule of the document is contradicted)

| Where | What | Why it stays |
|---|---|---|
| `tasks/dispatcher.py`, `celery_app.py` | A running `scraper_jobs` row is retired when it has not heartbeated for 30 minutes or is older than 3 hours; the login-session worker retires its orphaned running rows and lock at start | Housekeeping after a deploy or crash; without it a dead job blocks its source for hours. |
| `session_manager.merge_source_config` | Atomic JSONB merge of `config_json` keys | The connector, the dashboard and the API each hold a copy of the source row. |
| `PakistanLawSitePipeline._persist_live_session` | The cookies the site renewed during a run are written back into the slot (compare-and-update) | Section 3.2 stores the state; keeping it current is the same session, not a new one. |
| `PlaywrightBrowser.goto` | On a `ReferenceCaseLawSearch` detail page the "Case Description" control is opened and the full judgment text taken from it; headnote-only pages are classified `headnote` and never promoted as full judgments | Deterministic parsing of the detail page the site actually serves (section 0.2: full text is the preserved source text). |
| `tasks/promotion.py` | A share of each promotion batch is reserved for PakistanLawSite rows; relation reconciliation tasks for instruments, treatments and judgment citations run on Beat; migrations 003-006 (relation graphs) | Promotion and relation internals; the contract tables of section 8 are unchanged. |
| `cloud/install.sh`, `cloud/do_server_logs.sh` | Local edits on the server are stashed and pinned before checkout; root cron jobs that log in to the site or dispatch outside the stack are removed; an `inspect` mode for the logs workflow | Section 12: the installer "resets the service checkout to the fetched commit"; the cron jobs ended the worker's session (audit section 12). |
| `pytest.ini`, CI | `pytest-timeout` 600 s per test, 60-minute job timeout | Section 14 needs the suite to finish. |

## What to expect after this change

* PakistanLawSite runs one browser on the current ACTIVE slot at 4-9 s per page, at most 300 pages
  an hour and 2,500 a day, dispatched once every `scrape_frequency_hours` (24 h for this source)
  when Beat's 30-minute dispatch finds it due, or at once from the dashboard's *Run*.
* Searches go through the CitationSearch form map. On the live site the authenticated
  CitationSearch page is the citation table with the site's own filter inputs beside it; the audit
  (sections 1-3, 13) recorded that the form path yielded about 450 citations in weeks and that the
  filter inputs, once mapped, produced `Page.fill` timeouts. If a run retires rows with "search map
  offers no field for this query" or the map goes stale five times, that is the form path meeting
  the grid page, not a fault in the tiers.
* A slot the site bounces waits for a person: the dashboard shows `NEEDS_HUMAN_LOGIN`, the
  alternate ACTIVE slot continues the same cursor if there is one, otherwise the source pauses until
  the next human login Complete or an admin resume.
