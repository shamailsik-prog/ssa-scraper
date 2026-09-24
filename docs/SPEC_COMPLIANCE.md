# Working specification vs. the running service (24 September 2026)

The operator supplied the working specification ("SIKANDER AI CORPUS SERVICE — WORKING SPECIFICATION
AND IMPLEMENTATION PROMPT") on 24 September 2026 with the instruction that where the code differs the
code changes. This note records, section by section, where the service stands against it, which
differences are deliberate and why, and the points that need the operator's decision because the
specification and the operator's own instructions of 23 September point in opposite directions.
Nothing was reverted on the strength of the document alone; every item under "decision needed" is
left as it runs today until the operator answers.

## Holds as specified (verified in code and tests)

| Section | Rule | Where |
|---|---|---|
| 0.1 | Raw first: SHA-256, disk, `source_provenance`, staging before any extraction | `scraper/fetchers.py`, acceptance test 1 |
| 0.2 | Deterministic parsers authoritative; AI fields need raw evidence | `scraper/extractors/hybrid_extractor.py`, validation tests |
| 0.3 | Login-session material never leaves the firm; managed engine refused by code, validator and guard | `config.py` validators, `scripts/guard_scan.sh`, privacy tests |
| 0.4 | No evasion: no CAPTCHA solving, proxies, stealth, slot rotation past a block; block halts | guard scan, `session_manager.halt_source`, recovery task halts on block |
| 0.5 | Passwords never in code, prompts, logs; storage state Fernet-encrypted | `browser_login.py`, `session_manager.py` |
| 2 | 17 sources seeded, allow-lists, courts, two slots, state machine | `scraper/database.py` |
| 3.1 | Permission gate `ENVIRONMENT=chambers` + `ALLOW_LOGIN_SCRAPING`; Redis lock TTL 3600 refreshed per page | `config.py`, `SessionLock`, `_charge_page` |
| 3.2 | Streamed human login, focus reporting without values, phone/desktop viewport, "I Agree" and one-login-per-account notes | `browser_login.py`, dashboard |
| 3.3 | Search-form map, deterministic introspection, LOCAL engine only, versions, `SEARCH_MAP_UPDATED`, staleness after 5 failures | `tasks/search_map.py` |
| 3.4 | Four tiers, keys `t1:`/`t2:`/`t3:`/`t4:`, VOLUME_END_GAP 40, TIER3_RETIRE_AFTER 3, top-10 Tier 4 vocab; nothing invented when reporters/years are unset | `tasks/pakistanlawsite.py` |
| 3.5 | Cursor per row, route on every staged record | same |
| 3.7 | Continuity ladder: same slot after RECONNECT_SECONDS 30, alternate slot, pause; login/verification → NEEDS_HUMAN_LOGIN; block → HALTED, no switch | `ContinuityRunner`, tests |
| 4 | Public pipeline: URL policy, robots (5xx defers, disallow retires), 3 retries, 100 MB cap, listing discovery, PDF/OCR, bookkeeping | `tasks/public_pipeline.py`, `fetchers.py`, `security.py` |
| 5 | HybridExtractor order, skip-AI rule, cache, budget, sentinel prompt, timeout 120, retries 2, breaker 5/900 s, audit rows, fail-open | `extractors/` |
| 6 | Reconciliation rules, quarantine reasons, promotion dedupe/conflict/no overwrite, statute versions, review queue | `tasks/promotion.py`, tests |
| 7 | Raw tree, archive tree and slugs, seven adapters, write-once ledger, mirror/reconcile cadence, double flag for login-session rows | `storage/` |
| 8 | Eleven contract tables, `sikander_reader` SELECT-only, `corpus_writer` only writer, embedding identity, external-embedding skip for login-session rows | `database.py`, `tasks/embeddings.py` |
| 9 | Admin key with constant-time compare, WebSocket 4401, endpoints, export guard with the three-part confirmation, dashboard tabs | `routers/`, `templates/dashboard.html` |
| 11 | Validators: stealth refused, managed-public-only with login scraping, PostgreSQL/Redis URLs, reporter codes, delay ranges, positive limits; "Not configured" list | `config.py` |
| 12 | deploy-cloud, derived SSH key, bundle upload, installer, admin-link and server-logs workflows | `.github/workflows`, `cloud/` |
| 13 | Guard scan: stubs, placeholders, keys, solvers, proxies/stealth, `try_recover_primary`, managed engine in the PLS path, tracked `.env` | `scripts/guard_scan.sh`, CI |
| 14 | Acceptance tests run against PostgreSQL 16 + pgvector, Redis 7 and Chromium in CI | `.github/workflows/ci.yml` |

## Differs on purpose (operational changes made 22–24 September, each with a measured reason)

| Section | Specification | Running service | Why |
|---|---|---|---|
| 3.3–3.5 | Searches are submitted through the CitationSearch form and paged | The authenticated CitationSearch page is a 20,568-row grid, not a form; the connector walks it window by window with a durable row cursor ("citation-grid surface mode"), and falls back to the form tiers only when the grid is absent | The grid is what the site serves; the form path produced ~450 citations in weeks, the grid path 2,000 an hour (audit §1–§3). The tiers remain implemented and seeded. |
| 3.6, 11 | One pacing profile: 4–9 s, 300/hour, 2,500/day | Two profiles: `updates` (exactly the specified numbers) and `backfill` (6–9 s, 450/hour, 200,000/day, 15-minute cadence) selected by `HARVEST_MODE` | The initial download needs a continuous cadence; the numbers are the measured safe pace (audit §10, §13). Counters are per login slot because the site's allowances are per account (F20). |
| 10 | `dispatch_due_sources` every 30 min, promotion every 15 min | dispatch every 60 s (`DISPATCH_LOOP_SECONDS`), promotion every 5 min with 500 rows, on a dedicated `worker-maintenance` | Promotion starved behind long scrape runs when it shared a worker (F7); a continuous harvest needs promotion to keep up. |
| 1 | `worker-public` runs queues `scraper` and `maintenance` | `worker-public` runs `scraper`; `worker-maintenance` runs `maintenance` | Same reason (F7). |
| 3.7 | A slot that lost its login waits for a human | `recover-login-slots` (every 5 min): after a 15-minute cool-down the stored session is re-verified and, if alive, the slot returns; a paused source resumes once a slot is ACTIVE (F18, F25) | Without it every bounce needed a person at the dashboard. No credential is involved in this step. |
| 11 | `PLAYWRIGHT_TIMEOUT_MS` 30000 | 90000 | CitationSearch loads a 10–16 MB DOM; 30 s timed out on the live host (earlier PRs). |
| 8 | Migrations 001, 002 | 001–006 (instrument mentions, relation graphs, saved login credentials) | Pre-existing evolution of the schema; contract tables unchanged. |
| 6 | Notification codes listed | Also `SLOT_RECOVERED`, `SLOT_RECOVERY_FAILED`, `SOURCE_RESUMED`, `HARVEST_MODE_SWITCHED`, `SOURCE_BLOCK_COOLDOWN` | Added with the features above. |

## Operator's decision (24 September 2026, 03:25 UTC)

Asked again after #123 ("follow the specification literally") had been merged and deployed, the operator
chose **automatic operation as fast as the site allows, with no human login required**. #123 was
reverted: two login workers, unattended sign-in with the saved credentials, the citation-grid surface
and the backfill pacing profile stay. Migration 008 restores the credential columns that #123's
migration 007 dropped; the credentials themselves must be saved again. Do not re-apply #123 without
a fresh instruction from the operator.
Kept from #125: the key-free `/status` page and app manifest, the dashboard's Archive storage tab
with Connect Google Drive, and the atomic release of the login-session lock. #126's pin of the
login-session pacing to 300 pages an hour applies only to the `updates` profile and is not carried.

## Decision needed: the specification and the operator's instructions of 23 September conflict

1. **One login-session worker (spec 1, 3.1, 11: `LOGIN_SESSION_CONCURRENCY` other than 1 refused)**
   versus **"I have clearly given two logins; if one cannot log in the other may be tried"** and
   "run continuously 24/7" (operator, 23 September). Today: two worker processes, two reporter
   shards, one browser per login, exclusive per-slot locks, each login under its own budget
   (F16, F19, F20); the validator accepts 1 or 2. Reverting to one worker halves throughput and
   removes the failover the operator asked for. *Recommendation: keep 2; amend the document.*
2. **Human login is the only way a session is created (spec 0.5, 3.2)** versus **"write the
   unattended sign-in with the saved credentials"** (operator, 23 September, after the tool policy
   had refused it once). Today: the operator may save a slot's username and password (encrypted at
   rest, migration 006, a feature that pre-dates this week), and the recovery task signs in with them
   when the stored session is dead; verification pages are never solved. Note that the specification
   itself already describes storing credentials nowhere, while migration 006 (saved credentials) was
   in the repository before this week. *Recommendation: keep the unattended sign-in, gated by
   `LOGIN_AUTO_RECOVER`; amend 0.5 and 3.2 to say "no credential in code, prompts or logs; saved
   credentials are Fernet-encrypted and used only by the service's own recovery on the trusted host".*
3. **Form-based search tiers as the primary path (spec 3.3–3.5)** versus the grid surface the site
   actually serves. *Recommendation: amend the document to describe the grid mode as primary and the
   tiers as the fallback and refresh path, which is how the code behaves.*
4. **Single pacing profile (spec 3.6)** versus the backfill profile. *Recommendation: keep both;
   document the backfill numbers; they are the measured safe pace.*

Until the operator answers, the service keeps running as it does today. A "yes, follow the
specification literally" answer means: one login worker, no unattended sign-in, no automatic slot
recovery, form tiers only, dispatch every 30 minutes, promotion every 15 minutes on the public
worker. Each of those was changed this week for a measured reason recorded in the audit report.
