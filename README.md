# SIKANDER AI — Corpus Service (scraper)

Pakistani legal corpus service: harvests reported judgments, statutes (with versioned sections) and
legislative instruments into PostgreSQL 16 + pgvector, mirrors source documents to archive storage,
and hands the SIKANDER AI application **one read-only connection string** (`sikander_reader`).
Nothing else crosses the boundary: no shared runtime, no shared store, no credential.

Built to FINAL_SCRAPER_PROMPT_3 as amended by the ScrapeGraphAI Integration Amendment D-15
(September 2026). `CORRECTION_LOG.md` records how the governing contract was reconstructed and
every correction applied to the earlier build.

## How it works

```
Celery Beat ─► dispatcher ─► connector per source
                               │
               ┌───────────────┴───────────────┐
          Playwright                          HTTPX
   (PakistanLawSite, human-login          (public courts, PakistanCode,
    storage state, search forms)           assemblies, Gazette, PDFs)
               └───────────────┬───────────────┘
                    RAW-FIRST STAGING  (SHA-256 → source_provenance → raw bytes → staging row)
                               │
                    EXTRACTION ORCHESTRATOR  (scraper/extractors/hybrid_extractor.py)
            deterministic parsers ─► ScrapeGraph managed (PUBLIC only) / local (on-prem) ─► PDF/OCR
                               │
                    VALIDATE / RECONCILE  (citations, court, date, statutes must exist in the raw)
                               │
                    DEDUPLICATE / PROMOTE  (judgment · citation · treatment · statute · statute_section ·
                                            statute_section_version · instrument · court · judge · source_provenance)
                       ┌───────┴────────┐
              sikander_reader       ARCHIVE MIRROR (7 target types, write-once, _index CSVs)
```

**Raw first, AI second.** Every page is hashed, preserved and staged before any extraction runs.
If ScrapeGraph times out, errors, hits its credit cap or returns invalid JSON, the deterministic
parser continues and the record is never lost.

**Deterministic parsers are authoritative.** A citation, statute, date, court or judge proposed by
an AI engine enters the corpus only when it is present in the raw source; otherwise it is rejected,
logged as a conflict and lowers confidence. Records below the per-source threshold go to the review
queue. `full_text` is always the preserved source text, never an AI rewrite.

**PakistanLawSite never leaves the firm.** Login-session material uses deterministic parsers and,
if configured, a LOCAL model (Ollama / on-prem OpenAI-compatible endpoint). The managed ScrapeGraph
API, MCP and external treatment/embedding models are refused for it by code, configuration
validators and tests.

### PakistanLawSite — human login and four tiers

1. **Human login.** On the dashboard's *Human login* tab the service opens a server-side Chromium and
   streams it to you. You type the username, password and any verification yourself. The service
   never sees, stores or echoes the password. On *Complete* it verifies the page is authenticated,
   encrypts the browser storage state (cookies + localStorage) with `ENCRYPTION_KEY` and stores it
   in slot 1 or 2. Scraping then runs headless inside that session. CAPTCHA / verification pages are
   never solved by code.
2. **Search-form map.** Playwright renders the search page; deterministic introspection (optionally
   refined by the LOCAL engine, always re-verified against the DOM) records fields, result layout,
   pagination and a DOM hash. Five consecutive unparseable result pages mark the map stale and alert.
3. **Tier 1** — citation enumeration per subscribed reporter × year; `VOLUME_END_GAP` (40) consecutive
   misses close the volume. **Tier 2** — one query per promoted statute section. **Tier 3** —
   vocabulary sweep ranked by yield and retired after repeated zero yield. **Tier 4** — current-year
   daily wave. `crawl_frontier` / `crawl_coverage` are the truth for progress.
4. **Continuity.** Disconnect → wait exactly `RECONNECT_SECONDS` (30) → reconnect the **same** slot →
   resume the **same** cursor; only if that fails may the alternate slot continue the same cursor.
   Never from page one; no automatic recovery to the primary. Verification/login expiry → slot
   `NEEDS_HUMAN_LOGIN`, notify, pause if no valid slot. HTTP 403 / "account suspended" / "automated
   access" / CAPTCHA → source **HALTED**, notify, no slot switch, no proxy, no stealth, admin review.
5. One login-session worker at a time (Redis lock; a second worker is refused).

Login scraping runs only when `ENVIRONMENT=chambers` **and** `ALLOW_LOGIN_SCRAPING=true`; the
settings validator refuses any other combination.

## Services (docker-compose)

| service | role |
|---|---|
| `api` | FastAPI: `/health`, `/dashboard`, `/admin/*`, `/export/*`, `/api/*` |
| `worker-scraper` | Celery, queue `login_session`, concurrency 1 (PakistanLawSite) |
| `worker-public` | Celery, queues `scraper`, `maintenance` (public sources, promotion, treatment, archive, dispatch) |
| `worker-embed` | Celery, queue `embeddings` |
| `celery-beat` | schedules (see `scraper/tasks/celery_app.py`) |
| `celery-flower` | Celery monitoring on the compose network |
| `postgres` | `pgvector/pgvector:pg16` |
| `redis` | broker, result backend, login-session lock |
| `ollama` | optional, `--profile local-ai`: on-prem model for the LOCAL engine |

Every service has a health check. The main stack starts without the `local-ai` profile; the
deterministic fallback is then in force for PakistanLawSite.

## How to start

**Cloud server, no terminal** — add a DigitalOcean API token as the repository secret `DO_TOKEN`,
then run the **deploy-cloud** workflow from the Actions tab; the summary shows the dashboard address
and admin key. Details in `docs/CLOUD_DEPLOYMENT.md`.

**Cloud server, from a terminal** — one command on a fresh Ubuntu/Debian server, HTTPS included:

```bash
curl -fsSL https://raw.githubusercontent.com/shamailsik-prog/ssa-scraper/main/cloud/install.sh \
  | sudo bash -s -- --domain corpus.example.com --reporters PLD,SCMR,CLC --earliest-year 1990 --region Frankfurt
```

While the repository is private, prefix the download with a read-only GitHub token as shown in
`docs/CLOUD_DEPLOYMENT.md`, which also covers server sizing and first use. The server the firm controls is
the trusted host (`ENVIRONMENT=chambers`): login-session scraping runs only there.

**Local machine**

```bash
git clone <this repository> && cd <repo>
./deploy.sh            # installs Docker if needed, writes .env with fresh keys, builds, starts
# or manually:
cp .env.example .env   # then fill in: ENCRYPTION_KEY, ADMIN_API_KEY, SIKANDER_READER_PASSWORD, CORPUS_WRITER_PASSWORD
docker compose up -d --build
curl -s localhost:8000/health
```

Then open the dashboard (`https://<domain>/dashboard` on the cloud, `http://localhost:8000/dashboard`
locally), enter the `ADMIN_API_KEY`, and:

1. **Overview** — check *Not configured*: set `DEPLOY_REGION`, `PLS_SUBSCRIBED_REPORTERS`,
   `PLS_EARLIEST_YEAR`, `SGAI_API_KEY` (public sources), `SGAI_LOCAL_LLM_BASE_URL/MODEL`
   (PakistanLawSite AI assist), `SGAI_DAILY_CREDIT_CAP`, `OPENAI_API_KEY` (embeddings) in `.env`
   as the firm decides. Blank values stay conservative.
2. **Human login** — start a login on slot 1, log in inside the stream, press *Complete*.
3. **Sources** — *Run* PakistanLawSite (trusted host only) and any public source; Beat also dispatches
   on schedule. Extraction mode, AI enable and confidence threshold are per source.
4. **Coverage** — tiers, reporter volumes, search maps, frontier.
5. **Review queue / Check viewer** — promote or reject quarantined records with the raw text, the
   deterministic, AI and reconciled extractions and the audit trail side by side.
6. **Archive storage** — add targets (`google_drive`, `dropbox`, `onedrive`, `s3_compatible`, `sftp`,
   `smb`, `local_path`); configuration is encrypted at rest and never echoed. *Mirror now* /
   *Reconcile storage*.
7. **ScrapeGraph** — engine status, breakers, budget, usage ledger, public test URL.

Put the API behind TLS and network restrictions before exposing it; `/admin/*`, `/api/*` and the
dashboard require `X-API-Key`.

## Database contract

Contract tables readable by `sikander_reader`: `court`, `judge`, `judgment`, `citation`, `treatment`,
`statute`, `statute_section`, `statute_section_version`, `instrument`, `source_provenance`,
`corpus_metadata`. Every internal table (staging, session state, extraction audit, cache, archive
configuration, frontier, jobs, notifications) is revoked from the reader. `corpus_metadata` carries
`embedding_model`, `embedding_dim`, `deploy_region`; the embedding worker refuses to run on a
mismatch. Migrations live in `migrations/` and run at start-up (`001_initial`,
`002_scrapegraph_integration`).

Connection string for the application: `postgresql://sikander_reader:<SIKANDER_READER_PASSWORD>@<host>:5432/legal_scraper`.

## ScrapeGraphAI integration (Amendment D-15)

- `scraper/extractors/schemas.py` — strict Pydantic v2 schemas: `JudgmentExtraction`,
  `StatuteExtraction`/`StatuteSectionExtraction`, `InstrumentExtraction`, `SearchResultExtraction`,
  `SearchFormMapExtraction`.
- `scraper/extractors/prompts.py` — fixed server-side templates: page text is DATA, no inference
  without evidence, null/[] for absent fields, exact full text, JSON only.
- `scrapegraph_managed.py` (official `scrapegraph-py` SDK, PUBLIC only; `cookies`, `headers`,
  `stealth` never passed), `scrapegraph_local.py` (Ollama / OpenAI-compatible on-prem endpoint, or
  the `scrapegraphai` library), `hybrid_extractor.py`, `validation.py`, `cache.py`.
- Per source: `extraction_mode` (deterministic | hybrid | scrapegraph_managed | scrapegraph_local),
  `ai_extract_enabled`, `extraction_min_confidence`, `scrapegraph_schema_version`.
- Cost/reliability: content-hash cache, deterministic-first threshold, daily credit cap
  (`SGAI_BUDGET_EXHAUSTED` → deterministic continues), timeout, retries, circuit breaker, fail-open.
- Observability: `/admin/scrapegraph/status`, `/admin/scrapegraph/usage`,
  `/admin/scrapegraph/test-public` (configured PUBLIC URL only), `extraction_audit` table.
- MCP is development tooling only: `docs/MCP_DEVELOPMENT.md`.

## Tests

```bash
pip install -r requirements.txt && playwright install chromium
DATABASE_URL=postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper REDIS_URL=redis://localhost:6379/0 \
SIKANDER_READER_PASSWORD=readerpw CORPUS_WRITER_PASSWORD=writerpw pytest -q
bash scripts/guard_scan.sh      # no stubs, no secrets, no bypass logic
```

The suite runs against a real PostgreSQL (pgvector) and Redis and covers privacy, raw-first,
validation, cache/cost, human login (real Playwright), reconnect, block handling, four-tier
coverage, public discovery, prompt injection/SSRF, PDF/OCR, archive mirror and database roles.

## Layout

```
scraper/config.py            settings + validators         scraper/tasks/pakistanlawsite.py  four tiers, continuity
scraper/database.py          engine, migrations, roles     scraper/tasks/search_map.py       search-form map
scraper/models.py            contract + internal tables    scraper/tasks/public_pipeline.py  shared public pipeline
scraper/fetchers.py          HTTP fetch, raw preservation  scraper/tasks/superior_courts.py  superior-court dispatcher (SC, LHC, SHC, PHC, BHC, IHC, AJK HC, AJK SC, SAC-GB, FSC)
                                                          scraper/tasks/supreme_court.py         Supreme Court Pakistan listings + bounded POST result harvesting
                                                          scraper/tasks/ajk_high_court.py        AJK HC listing + POST result discovery
                                                          scraper/tasks/sindh_high_court.py      SHC caselaw result-grid + file-view discovery
                                                          scraper/tasks/peshawar_high_court.py   PHC reported-judgments POST discovery + direct PDF routing
                                                          scraper/tasks/ajk_supreme_court.py     AJK SC listings/posts + anchor/data/script discovery
                                                          scraper/tasks/supreme_appellate_court_gb.py  SAC-GB listings + anchor/data/script discovery
                                                          scraper/tasks/federal_shariat_court.py  FSC multi-page + anchor/data/script discovery
scraper/security.py          allow-list, SSRF, robots,     scraper/tasks/pakistancode.py     PakistanCode statutes
                             block detection, scrubbing    scraper/tasks/legislatures.py     NA, Senate, 4 assemblies, Gazette
scraper/auth/session_manager.py  slots, lock, reconnect    scraper/tasks/nasirlawsite.py     NasirLawSite
scraper/auth/browser_login.py    streamed human login      scraper/tasks/promotion.py        validate, dedupe, promote
scraper/parsers/*            deterministic legal parsers   scraper/tasks/treatment.py        treatment classification
scraper/extractors/*         ScrapeGraph subsystem         scraper/tasks/embeddings.py       embeddings (B-6)
scraper/storage/*            archive adapters + mirror     scraper/tasks/archive_mirror.py   mirror / reconcile tasks
scraper/routers/*            admin API, export, sessions   scraper/templates/dashboard.html  operator console
migrations/                  forward migrations            tests/                            pytest suite
```
