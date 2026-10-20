"""
Central configuration for the SIKANDER AI corpus service.

Every value comes from the environment or `.env`. Firm values that require a business
decision (subscribed reporters, earliest year, deploy region, archive targets, credentials)
have no default: they stay blank, the dashboard shows them as NOT CONFIGURED, and the code
uses the most conservative behaviour it can while logging that it did so.

Guards enforced here (Amendment D-15 §15, Cursor command §5):
  * ALLOW_LOGIN_SCRAPING=true requires ENVIRONMENT=chambers.
  * SGAI_MANAGED_PUBLIC_ONLY=false is refused while any login_session source is enabled.
  * SGAI_STEALTH_ALLOWED=true is refused for PakistanLawSite (and is refused globally unless
    an explicit partner decision adds it — the default deployment never needs stealth).
  * A local/private AI engine selected without an endpoint/model is refused unless
    SGAI_FAIL_OPEN_TO_DETERMINISTIC=true.
  * SGAI_API_KEY is a SecretStr and is never logged or serialised.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Any, Dict, List, Literal, Optional

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

KNOWN_REPORTERS = ("PLD", "SCMR", "CLC", "PCrLJ", "PTD", "PLC", "CLD", "YLR", "MLD", "GBLR", "PLJ", "NLR", "KLR", "PCRLJ")
ARCHIVE_TARGET_TYPES = ("google_drive", "dropbox", "onedrive", "s3_compatible", "sftp", "smb", "local_path")
EXTRACTION_MODES = ("deterministic", "hybrid", "scrapegraph_managed", "scrapegraph_local")
LOGIN_SESSION_SOURCE_NAMES = ("PakistanLawSite",)
HARVEST_MODES = ("backfill", "updates")

SECRET_FIELD_NAMES = (
    "ENCRYPTION_KEY",
    "ADMIN_API_KEY",
    "SECRET_KEY",
    "OPENAI_API_KEY",
    "SGAI_API_KEY",
    "SIKANDER_READER_PASSWORD",
    "CORPUS_WRITER_PASSWORD",
    "CORPUS_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "PLS_PASS",
    "PLS_PASS_B",
    "ARCHIVE_S3_SECRET_KEY",
    "ARCHIVE_SFTP_PASSWORD",
    "ARCHIVE_SMB_PASSWORD",
    "ARCHIVE_DROPBOX_TOKEN",
    "ARCHIVE_ONEDRIVE_TOKEN",
    "LOCAL_TREATMENT_API_KEY",
    "READER_API_KEY",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
        env_prefix="",
    )

    # ------------------------------------------------------------------ core
    APP_ENV: Literal["development", "staging", "production"] = Field(default="production")
    ENVIRONMENT: str = Field(
        default="cloud",
        description="Deployment class. 'chambers' = the firm's own trusted host (chambers PC or a firm-controlled cloud server); login-session scraping runs only there.",
    )
    PROJECT_NAME: str = Field(default="SIKANDER AI Corpus Service")
    API_V1_PREFIX: str = Field(default="/api/v1")
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    TIMEZONE: str = Field(default="Asia/Karachi")
    DEBUG: bool = Field(default=False)
    DEPLOY_REGION: str = Field(default="", description="Fixed by the firm's data-residency decision (L17.13a). Blank = NOT CONFIGURED.")
    DATA_RESIDENCY_NOTE: str = Field(default="", description="Free-text note recorded with the region decision.")

    # -------------------------------------------------------------- database
    DATABASE_URL: str = Field(default="postgresql+asyncpg://legal:legal@postgres:5432/legal_scraper")
    DATABASE_POOL_SIZE: int = Field(default=10)
    DATABASE_MAX_OVERFLOW: int = Field(default=20)
    DATABASE_POOL_TIMEOUT: int = Field(default=30)
    DATABASE_ECHO: bool = Field(default=False)
    # Postgres ends an app session left idle inside a transaction this long (0 disables). A leaked mirror
    # connection held archive_targets row locks for over an hour on 9 October 2026 and stalled every later
    # mirror run; 30 minutes matches the dispatcher's heartbeat rule, so no live job idles that long.
    DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS: int = 1800
    SIKANDER_READER_ROLE: str = Field(default="sikander_reader")
    SIKANDER_READER_PASSWORD: SecretStr = Field(default=SecretStr(""), description="Blank = role not created; dashboard shows NOT CONFIGURED.")
    CORPUS_API_KEY: str = Field(
        default="",
        description="Key for the read-only corpus API (/corpus/v1) used by the legal research agent; sent as X-Corpus-Key. Separate from ADMIN_API_KEY. Blank = the API answers 503 not configured.",
    )
    CORPUS_WRITER_ROLE: str = Field(default="corpus_writer")
    CORPUS_WRITER_PASSWORD: SecretStr = Field(default=SecretStr(""))

    # ----------------------------------------------------------- redis/celery
    REDIS_URL: str = Field(default="redis://redis:6379/0")
    CELERY_BROKER_URL: Optional[str] = Field(default=None)
    CELERY_RESULT_BACKEND: Optional[str] = Field(default=None)
    REDIS_CACHE_TTL_SECONDS: int = Field(default=3600)
    DISPATCH_LOOP_SECONDS: int = Field(default=60, description="Celery Beat cadence for dispatch_due_sources.")

    # --------------------------------------------------------------- secrets
    ENCRYPTION_KEY: str = Field(default="", description="Fernet key for browser storage state and archive credentials. Required.")
    ADMIN_API_KEY: str = Field(default="", description="X-API-Key for /admin routes. Required.")
    SECRET_KEY: str = Field(default="")

    # ------------------------------------------------------------ embeddings
    OPENAI_API_KEY: Optional[SecretStr] = Field(default=None)
    OPENAI_EMBEDDING_MODEL: str = Field(default="")  # legacy alias of EMBEDDING_MODEL, kept for compatibility
    OPENAI_EMBEDDING_DIMENSIONS: int = Field(default=0)  # legacy alias of EMBEDDING_DIM
    EMBEDDING_MODEL: str = Field(default="text-embedding-3-small")
    EMBEDDING_DIM: int = Field(default=1536)
    EMBEDDING_BATCH_SIZE: int = Field(default=50)
    EMBEDDING_RPM_LIMIT: int = Field(default=100)
    EMBEDDING_TPM_LIMIT: int = Field(default=1_000_000)
    OPENAI_TIMEOUT_SECONDS: int = Field(default=60)
    EMBED_LOGIN_SESSION_ROWS_EXTERNALLY: bool = Field(
        default=False,
        description="Login-session text never leaves the firm unless a partner decision sets this true.",
    )

    # --------------------------------------------------------------- scraper
    SCRAPER_DELAY_MIN: float = Field(default=1.2)
    SCRAPER_DELAY_MAX: float = Field(default=2.5)
    SCRAPER_TIMEOUT_SECONDS: int = Field(default=30)
    SCRAPER_RETRY_ATTEMPTS: int = Field(default=3)
    SCRAPER_RETRY_BACKOFF: float = Field(default=2.0)
    SCRAPER_USER_AGENT: str = Field(
        default="SikanderCorpusBot/1.0 (+legal research corpus; contact via firm; honours robots.txt)"
    )
    SCRAPER_CONCURRENT_REQUESTS: int = Field(default=2)
    INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE: int = Field(default=200)
    INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS: int = Field(default=168)
    INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS: int = Field(default=3600)
    SCRAPER_RESPECT_ROBOTS: bool = Field(default=True)
    PLAYWRIGHT_ENABLED: bool = Field(default=True)
    PLAYWRIGHT_HEADLESS: bool = Field(default=True)
    PLAYWRIGHT_TIMEOUT_MS: int = Field(default=90000)
    PLS_BROWSER_OP_TIMEOUT_SECONDS: int = Field(
        default=180,
        description="Watchdog: a PakistanLawSite page operation (open a judgment, download a PDF, submit a search) that makes no progress this long is a frozen browser; it is restarted on the same slot and the operation resumes. 0 disables.",
    )
    PLS_GRID_OP_TIMEOUT_SECONDS: int = Field(
        default=600,
        description="Watchdog limit for opening the citation grid, which loads a 10-16 MB page and seeks to the cursor row. 0 disables.",
    )
    PLAYWRIGHT_EXECUTABLE_PATH: str = Field(default="", description="Optional Chromium executable; blank = Playwright's bundled browser.")
    PLAYWRIGHT_MAX_HTML_BYTES: int = Field(default=2_000_000, description="Guard: skip full page.content() when HTML responses are larger than this many bytes.")
    PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD: int = Field(default=5000, description="Guard: skip full page.content() when the DOM input count indicates a huge datatable surface.")

    # ------------------------------------------------- login-session sources
    ALLOW_LOGIN_SCRAPING: bool = Field(default=False)
    RECONNECT_SECONDS: int = Field(default=30)
    VOLUME_END_GAP: int = Field(default=40, description="Tier 1: consecutive misses that close a reporter volume.")
    SEARCH_MAP_STALE_FAILURES: int = Field(default=5)
    LOGIN_DELAY_MIN: float = Field(default=4.0, description="Seconds between login-session page fetches (minimum).")
    LOGIN_DELAY_MAX: float = Field(default=9.0, description="Seconds between login-session page fetches (maximum).")
    PAGES_PER_HOUR: int = Field(default=300, description="Login-session page budget per hour; the run pauses when spent.")
    PAGES_PER_DAY: int = Field(default=2500, description="Login-session page budget per day; the run pauses when spent.")
    LOGIN_SESSION_CONCURRENCY: int = Field(default=1, description="Login-session worker processes (1-2). With two ACTIVE slots (two logins) and 2, the reporter shards run in parallel, one per login.")
    LOGIN_AUTO_RECOVER: bool = Field(
        default=True,
        description="Recover a lost login slot without a human: LOGIN_RECOVERY_COOLDOWN_MINUTES after the site bounced a "
        "slot, re-open the stored session and put the slot back if the search page renders; if it is dead, sign in again "
        "with the credentials the operator saved for the slot and store the new session. Verification/CAPTCHA pages are "
        "never solved: the slot then waits for a human.",
    )
    LOGIN_RECOVERY_COOLDOWN_MINUTES: int = Field(default=3, description="Minutes after a lost login before the first automatic recovery attempt (owner's rule of 13 October 2026: after a logout, wait 3 minutes, then log back in).")
    LOGIN_RECOVERY_SCHEDULE_SECONDS: int = Field(default=60, description="Celery Beat cadence of the login-slot recovery task; every minute, so the 3-minute wait signs in at 3-4 minutes.")
    LOGIN_RECOVERY_SUBMIT_WAIT_SECONDS: float = Field(default=3.0, description="Seconds to let the site answer a submitted sign-in before the session is checked.")
    PLS_KEEPALIVE_SCHEDULE_SECONDS: int = Field(default=3600, description="Celery Beat cadence for PLS slot keepalive (probe ACTIVE slots; recover unhealthy only).")
    PLS_STALL_WATCHDOG_SECONDS: int = Field(default=1800, description="Celery Beat cadence for citation-grid stall watchdog.")
    PLS_STALL_NO_PROMOTION_HOURS: float = Field(default=6.0, description="/status stalled=true (and a PLS_NO_NEW_JUDGMENTS notification) when no PakistanLawSite judgment was promoted for this many hours.")
    PLS_SATURATED_RECHECK_HOURS: float = Field(default=12.0, description="A citation-grid shard whose last full lap staged nothing new is not scheduled again for this many hours.")
    PLS_GRID_JOURNAL_ROTATION: bool = Field(default=True, description="Walk the citation grid once per journal: select each journal in the site's citation-search form and give it its own cursor. Off, or when the form offers no journal choice, the default grid (the site's last or default search) is walked.")
    PLS_GRID_JOURNALS: str = Field(default="", description="Comma-separated journals for the per-journal grid walk, in order; blank = PLS_SUBSCRIBED_REPORTERS.")
    PLS_GRID_JOURNAL_MIN_MATCH: float = Field(default=0.5, description="Share of a journal grid's rows that must carry the requested journal's citations; below it the journal is marked unsupported (the form did not filter) and skipped until the saturation recheck.")
    PLS_CITATION_GRID_SKIP_FLUSH_EVERY: int = Field(default=50, description="Commit the grid cursor every N rows that are only skipped (known, already staged, other shard); rows that fetch a detail page still commit every PLS_CITATION_GRID_FLUSH_EVERY.")
    MIRROR_BACKLOG_PER_RUN: int = Field(default=300, description="Oldest-first backlog judgments the archive mirror writes per target per run, in addition to the newest ones.")
    MIRROR_RUN_BUDGET_SECONDS: int = Field(default=900, description="An archive mirror run stops starting new objects after this many seconds, commits, and queues the next run.")
    RECONCILE_RUN_BUDGET_SECONDS: int = Field(default=600, description="A reconcile_storage run stops checking objects after this many seconds; the next run carries on from the least recently verified.")
    RECONCILE_BATCH_SIZE: int = Field(default=2000, description="At most this many stored objects per target are checked in one reconcile_storage run.")
    ARCHIVE_BATCH_BYTES: int = Field(
        default=1024**3,
        description="Batch transfer: a batched archive target (ARCHIVE_BATCH_TARGET_TYPES) receives nothing until about this many bytes of judgments are waiting for it, then takes that whole snapshot in one transfer. 0 = copy every record as it is promoted.",
    )
    ARCHIVE_BATCH_MAX_WAIT_HOURS: float = Field(default=24.0, description="A batch also starts when the oldest judgment waiting for a batched target has waited this long, so a slow trickle still reaches it.")
    ARCHIVE_BATCH_TARGET_TYPES: str = Field(default="google_drive", description="Comma list of archive target types that receive batches (the rest are written record by record).")
    MIRROR_LAG_ALERT_HOURS: float = Field(default=3.0, description="Alert (ARCHIVE_MIRROR_LAG) when a target has unmirrored judgments and its newest mirrored judgment is older than this.")
    EMBED_QUEUE_ALERT_HOURS: float = Field(default=6.0, description="Alert (EMBED_QUEUE_STALLED) when pending embeddings are older than this and none were embedded in that time.")
    SPOT_CHECK_SCHEDULE_SECONDS: int = Field(default=1800, description="Every 30 minutes a few promoted records are re-fetched and compared with the corpus.")
    SPOT_CHECK_JUDGMENTS: int = Field(default=3, description="Promoted PakistanLawSite judgments re-fetched per spot-check run.")
    PLS_VERIFIER_SLOT: int = Field(default=2, ge=1, le=2, description="Dedicated independent PakistanLawSite verification account/slot. Harvesting must not use this slot for verifier work.")
    PLS_COVERAGE_CHALLENGES: int = Field(default=1, ge=0, le=10, description="Completed search queries independently re-run by the verifier each spot-check cycle to look for judgments the harvester missed.")
    SPOT_CHECK_STATUTES: int = Field(default=2, description="Current statute sections re-fetched per spot-check run.")
    RETIRED_SOURCES: str = Field(
        default="NasirLawSite,BalochistanHighCourt",
        description="Comma list of sources switched off for good on the owner's instruction (12 Oct 2026): both blocked this service (HTTP 403 / access denied) since mid-September. Kept as DISABLED rows so their records and history stay; never dispatched. Remove a name here to bring it back.",
    )
    UNREPORTED_JUDGMENTS: bool = Field(
        default=True,
        description="A judgment from a court's own website that no law report cites enters the corpus marked unreported (identity: court, case number, decision date) instead of being held for review. PakistanLawSite is never affected.",
    )
    UNREPORTED_MIN_TEXT_CHARS: int = Field(default=400, description="An unreported judgment needs at least this much text; shorter captures (cause lists, stubs) stay held.")
    UNREPORTED_RELEASE_BATCH: int = Field(default=500, description="Held 'no citation' judgments released for promotion as unreported per promotion run.")
    READER_MODEL: str = Field(default="", description="Model the reading agent uses on unreported judgments (OpenAI Chat-Completions-compatible API with JSON-object response mode). Blank = TREATMENT_MODEL; both blank, or off, = the agent does nothing.")
    READER_BASE_URL: str = Field(default="", description="OpenAI Chat-Completions-compatible endpoint supporting response_format=json_object. Blank = https://api.openai.com/v1 with OPENAI_API_KEY.")
    READER_API_KEY: SecretStr = Field(default=SecretStr(""), description="Key for READER_BASE_URL. Blank = OPENAI_API_KEY.")
    READER_BATCH: int = Field(default=20, description="Unreported judgments the reading agent reads per run.")
    READER_SCHEDULE_SECONDS: int = Field(default=600, description="How often the reading agent runs.")
    READER_MAX_CHARS: int = Field(default=24000, description="Maximum characters per reader-model chunk; long judgments are read in overlapping chunks, never head/tail truncated.")
    READER_CHUNK_OVERLAP_CHARS: int = Field(default=1200, description="Overlap between adjacent reader chunks so propositions crossing a boundary are not lost.")
    READER_RETRY_HOURS: float = Field(default=24.0, description="Base delay before retrying a failed judgment after transient/validation failures.")
    READER_HTTP_RETRIES: int = Field(default=2, description="Immediate retries for transient reader-provider HTTP/network failures before recording a failed attempt.")
    READER_AUTH_CIRCUIT_SECONDS: int = Field(default=3600, description="Circuit-breaker period after reader-provider authentication/authorization failures.")
    READER_TRANSIENT_CIRCUIT_SECONDS: int = Field(default=120, description="Circuit-breaker period after repeated provider/network failures.")
    READER_FRESH_SHARE: float = Field(default=0.2, ge=0.0, le=1.0, description="Share of each reader batch reserved for newest eligible judgments; the rest drains historical backlog.")
    DEPLOY_ROLE: Literal["all", "pls", "public"] = Field(
        default="all",
        description="What this server harvests: all (one server does everything), pls (PakistanLawSite only) or public (the free sites only). See scraper/deploy_role.py.",
    )
    HARVEST_MODE: str = Field(default="updates", description="Global scheduler mode: backfill (continuous) or updates (steady state).")
    HARVEST_AUTO_SWITCH: bool = Field(
        default=True,
        description="When true, backfill mode automatically switches to updates after the frontier is drained and targets are met.",
    )
    UPDATE_CADENCE_HOURS: int = Field(default=6, description="Default per-source scrape interval in updates mode.")
    BACKFILL_SOURCE_FREQUENCY_MINUTES: int = Field(default=15, description="Default per-source scrape interval in backfill mode.")
    BACKFILL_TARGET_JUDGMENTS: int = Field(
        default=0,
        description="Optional backfill completion target; 0 means no judgment-count threshold is required.",
    )
    BACKFILL_TARGET_STATUTES: int = Field(
        default=0,
        description="Optional backfill completion target; 0 means no statute-count threshold is required.",
    )
    BACKFILL_LOGIN_DELAY_MIN: float = Field(
        default=6.0,
        description="Backfill mode minimum delay between login-session page fetches. PakistanLawSite ends a "
        "login after roughly 500-600 page views in an hour (observed 22-23 Sep 2026: 532 and 373 pages, "
        "26 and 15 minutes after login, at 0.4-1.0s pacing); 6-9s keeps the account near 380 pages/hour.",
    )
    BACKFILL_LOGIN_DELAY_MAX: float = Field(default=9.0, description="Backfill mode maximum delay between login-session page fetches.")
    BACKFILL_PAGES_PER_HOUR: int = Field(
        default=450,
        description="Backfill mode login-session page budget per clock hour: a backstop under the site's observed "
        "session quota, so the run pauses itself (PacingBudgetExceeded) instead of losing the human login.",
    )
    BACKFILL_PAGES_PER_DAY: int = Field(default=200000, description="Backfill mode login-session page budget per day.")
    BACKFILL_LOGIN_SESSION_CONCURRENCY: int = Field(default=2, description="Backfill-mode login-session worker concurrency target.")
    BLOCK_RETRY_COOLDOWN_MINUTES: int = Field(
        default=120,
        description="Default cooldown before a blocked public source is retried when auto-retry is enabled.",
    )
    BLOCK_RETRY_MAX_ATTEMPTS: int = Field(
        default=3,
        description="Default number of cooldown retries for public-source explicit blocks before HALTED.",
    )
    MIRROR_LOGIN_SESSION_ROWS: bool = Field(default=False, description="Whether login_session judgments may be mirrored to archive targets.")
    EXPORT_LOGIN_SESSION_FULL_TEXT: bool = Field(default=False)
    PLS_BASE_URL: str = Field(default="https://www.pakistanlawsite.com")
    PLS_LOGIN_URL: str = Field(default="https://www.pakistanlawsite.com/")
    PLS_CHECK_URL: str = Field(default="https://www.pakistanlawsite.com/Login/Check")
    PLS_SEARCH_URL: str = Field(default="https://www.pakistanlawsite.com/Login/CitationSearch")
    PLS_ARCHIVED_GRID_MAX_ROWS: int = Field(default=400, description="Maximum rows materialized per compact #archivedpatientGrid snapshot (one window). The snapshot reads cell text only; on a snapshot timeout the window is halved and retried before the page is treated as disconnected.")
    PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_SECONDS: float = Field(default=20.0, description="Hard timeout for compact #archivedpatientGrid snapshots so CitationSearch cannot hold the login lock for a full Playwright navigation timeout.")
    PLS_CITATION_GRID_MAX_DETAIL: int = Field(default=120, description="Updates mode: max detail pages fetched per citation-grid window.")
    PLS_CITATION_GRID_SCAN_WINDOW: int = Field(default=200, description="Updates mode: rows scanned per citation-grid window (can exceed detail fetch cap; known rows are fast-forwarded).")
    BACKFILL_PLS_CITATION_GRID_MAX_DETAIL: int = Field(default=300, description="Backfill mode: max detail pages fetched per citation-grid window.")
    BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW: int = Field(default=600, description="Backfill mode: rows scanned per citation-grid window.")
    PLS_RUN_MAX_MINUTES: int = Field(default=0, description="Updates mode: keep harvesting consecutive citation-grid windows inside one login_session job for up to this many minutes (0 = one window per job).")
    BACKFILL_PLS_RUN_MAX_MINUTES: int = Field(default=50, description="Backfill mode: keep harvesting consecutive citation-grid windows inside one login_session job for up to this many minutes, so the session is not idle between Beat kicks (0 = one window per job). Keep it under the login-session lock TTL (60 min) and the job stale cut-off.")
    PLS_CITATION_GRID_SKIP_STAGED: bool = Field(default=True, description="Skip citation-grid rows whose citation already has a staging row (extracted, promoted, duplicate or quarantined) so a wrap of the grid does not re-download pages that were already preserved.")
    PLS_CASE_DESCRIPTION_WAIT_SECONDS: float = Field(default=6.0, description="How long to wait for the 'Case Description' control to render on a ReferenceCaseLawSearch page before the page is classified as headnote-only.")
    PROMOTE_PREFERRED_SOURCE: str = Field(
        default="PakistanLawSite",
        description="Judgment source that reserves a share of each promote batch so oldest-first public rows cannot starve login harvest.",
    )
    PROMOTE_PREFERRED_SHARE: float = Field(
        default=0.5,
        description="Fraction of each promote batch reserved for PROMOTE_PREFERRED_SOURCE when that source has pending rows.",
    )
    CORPUS_CHAIN_ENABLED: bool = Field(
        default=True,
        description="Event-driven chain: a committed fetch batch enqueues promotion, and a promotion that saved something enqueues the archive mirror (debounced). Beat stays the safety net.",
    )
    DISPATCH_DEDUPE_ENABLED: bool = Field(
        default=True,
        description="The dispatcher does not queue a public source again while a job it queued for that source has not started yet.",
    )
    DISPATCH_QUEUED_TTL_SECONDS: int = Field(
        default=10800,
        description="How long a 'job queued, not started' mark lasts; after it expires the source can be queued again even if the job was lost.",
    )
    DISPATCH_LEGACY_SKIP_SECONDS: int = Field(
        default=21600,
        description="A queued public job with no queue time (sent before queue times were recorded) is skipped when its source started a job this recently.",
    )
    PUBLIC_BACKLOG_CONTINUE_SECONDS: int = Field(
        default=60,
        description="A public source whose run stopped at its page limit, completed work and still has frontier rows runs again this soon instead of waiting its cadence. 0 = always wait the cadence.",
    )
    PLS_SUBSCRIBED_REPORTERS: str = Field(default="", description="Comma list. Firm value. Blank = NOT CONFIGURED; Tier 1 idles.")
    PLS_EARLIEST_YEAR: int = Field(default=0, description="Firm value. 0 = NOT CONFIGURED; Tier 1 covers current year only.")
    PLS_TIER3_VOCABULARY: str = Field(default="", description="Optional comma list seeding the Tier 3 vocabulary sweep.")
    PLS_SEARCH_HARVEST_ENABLED: bool = Field(
        default=False,
        description="When true, Celery Beat may schedule pls_search_harvest_tick (one search query per tick).",
    )
    PLS_SEARCH_HARVEST_SCHEDULE_SECONDS: int = Field(
        default=3600,
        description="Beat interval for each login slot's pls_search_harvest_tick when PLS_SEARCH_HARVEST_ENABLED=1, capped at PLS_SEARCH_HARVEST_TICK_SECONDS (a tick that worked queues its slot's next tick itself; the beat restarts a slot whose tick was skipped).",
    )
    PLS_SEARCH_HARVEST_SLOT1_REPORTERS: str = Field(
        default="PLD,SCMR,CLC,YLR,CLD,GBLR",
        description="Comma list: the reporters whose search-harvest ledger rows login slot 1 works. Slot 2 works every other reporter (PLC, PLC(CS), PCrLJ, MLD, PTD, PLJ and the rest, and rows with no reporter), so the two shares never overlap (handbook R3.1; owner's decision of 13 October 2026).",
    )
    PLS_SEARCH_HARVEST_TAKEOVER_MINUTES: int = Field(
        default=30,
        description="A login slot that has been logged out (not ACTIVE) this long has its share of the search-harvest ledger taken over by the other slot, which keeps its own share as well.",
    )
    PLS_SEARCH_HARVEST_CONTINUE: bool = Field(
        default=True,
        description="A slot's search-harvest tick that did work queues the next tick for the same slot when it ends, so each login harvests continuously instead of once per beat interval; the per-slot lock keeps it to one tick per slot.",
    )
    PLS_CASEID_WALK_ENABLED: bool = Field(
        default=True,
        description="Walk PakistanLawSite judgments by the site's own case serial (year + court letter + serial, e.g. 2026S701); see scraper/pls_caseid.py.",
    )
    PLS_CASEID_WALK_SCHEDULE_SECONDS: int = Field(default=600, description="Beat interval for pls_caseid_walk_tick.")
    PLS_CASEID_WALK_PROBES_PER_TICK: int = Field(default=60, description="Case serials asked for per tick, at the login pacing.")
    PLS_CASEID_WALK_MISS_STREAK: int = Field(default=40, description="Misses in a row after which each further miss jumps to the next block start (x01).")
    PLS_CASEID_WALK_CEILING: int = Field(default=6000, description="Lowest serial ceiling of a (year, court) group; a group ends past max(this, highest held serial + PAD).")
    PLS_CASEID_WALK_CEILING_PAD: int = Field(default=1000, description="How far past the highest held or found serial a group keeps probing block starts.")
    PLS_CASEID_WALK_RECHECK_HOURS: float = Field(default=12.0, description="After a failed calibration on held judgments, wait this long before trying again.")
    PLS_SEARCH_RESULT_CAP: int = Field(
        default=500,
        description="PakistanLawSite CitationSearch result cap observed in production; queries above this are split by court/keyword.",
    )
    PLS_SEARCH_HARVEST_TICK_SECONDS: int = Field(
        default=1200,
        description="A search-harvest tick stops after this long (between rows or pages) and resumes from its cursor on the next tick, so one long query cannot hold the single login worker for an hour while case-ID walk ticks expire. 0 = no limit.",
    )
    SSA_ORDER_SHEET_TITLE: bool = Field(
        default=True,
        description="Default on. PakistanLawSite case_title is taken from the order-sheet header instead of the first versus line in the text window. Set SSA_ORDER_SHEET_TITLE=0 or false to disable.",
    )
    SSA_ENCODING_GUARD: bool = Field(
        default=True,
        description="Default on. Classify ???? runs: quarantine when the English body is damaged; flag Urdu headnote-only damage (auditor 2026-10-18: 6,708 rows). Set SSA_ENCODING_GUARD=0 or false to disable.",
    )
    SSA_COURT_HEADER_GUARD: bool = Field(
        default=True,
        description="Default on. Fail closed when stored court disagrees with the judgment header (e.g. SC default vs HC header). Set SSA_COURT_HEADER_GUARD=0 or false to disable.",
    )
    SSA_PLD_CITATION_RELABEL: bool = Field(
        default=True,
        description="Default on. Relabel PLD citations missing a court token using the judgment header. Set SSA_PLD_CITATION_RELABEL=0 or false to disable.",
    )
    SSA_SHORT_TEXT_GUARD: bool = Field(
        default=True,
        description="Default on. Quarantine PLS judgments shorter than 500 characters of preserved text. Set SSA_SHORT_TEXT_GUARD=0 or false to disable.",
    )
    PLS_SEARCH_HARVEST_MAX_PAGES_PER_QUERY: int = Field(
        default=0,
        description="Optional per-query page ceiling for one runner invocation (0 = enumerate all pages).",
    )
    PLS_BROWSER_RECYCLE_WINDOWS: int = Field(
        default=12,
        description="Recycle the Playwright browser after this many search-harvest windows/pages.",
    )
    PLS_BROWSER_RSS_LIMIT_MB: float = Field(
        default=900.0,
        description="Soft RSS limit (MiB): recycle browser when exceeded.",
    )
    PLS_BROWSER_RSS_HARD_MB: float = Field(
        default=1200.0,
        description="Hard RSS limit (MiB): stop the harvest run cleanly before OOM.",
    )
    PLS_BLOCK_HEAVY_RESOURCES: bool = Field(
        default=True,
        description="Block image/font/media requests in Playwright to reduce CitationSearch memory use.",
    )
    PLS_MAX_FAILOVER_RETRIES: int = Field(default=0)  # legacy; automatic failover past a block is forbidden
    PLS_SESSION_TTL_MINUTES: int = Field(default=720)
    # Legacy automated-login names. Accepted so old .env files do not break; never used to log in.
    PLS_USER: str = Field(default="")
    PLS_PASS: SecretStr = Field(default=SecretStr(""))
    PLS_JOURNALS: str = Field(default="")
    PLS_USER_B: str = Field(default="")
    PLS_PASS_B: SecretStr = Field(default=SecretStr(""))
    PLS_JOURNALS_B: str = Field(default="")

    SSA_STATUTE_NO_MENTION_STUBS: bool = Field(
        default=True,
        description="Default on. Instrument mentions link only to existing statutes (no sectionless stub inserts). Set SSA_STATUTE_NO_MENTION_STUBS=0 or false to restore legacy stub creation.",
    )
    SSA_STATUTE_PDF_FOLLOW: bool = Field(
        default=True,
        description="Default on. Statute ingest follows a linked PDF when its text is longer than the HTML listing. Set SSA_STATUTE_PDF_FOLLOW=0 or false to disable.",
    )
    SSA_STATUTE_NAME_QUALITY: bool = Field(
        default=True,
        description="Default on. Promotion quarantines fragment/garbage names, missing year_enacted, and duplicate statute names. Set SSA_STATUTE_NAME_QUALITY=0 or false to disable.",
    )

    # ----------------------------------------------------------- pdf/storage
    PDF_STORAGE_PATH: str = Field(default="/app/live")
    STATE_STORAGE_PATH: str = Field(default="/app/state")
    RAW_STORAGE_PATH: str = Field(default="/app/raw", description="Raw HTML/text/PDF preservation root.")
    PDF_MAX_SIZE_MB: int = Field(default=100)
    OCR_ENABLED: bool = Field(default=True)
    OCR_LANGUAGE: str = Field(default="eng")
    OCR_DPI: int = Field(default=300)
    OCR_PAGE_TIMEOUT_SECONDS: int = Field(default=120, description="Tesseract gives up on one page after this long; the page is skipped.")
    OCR_DOCUMENT_BUDGET_SECONDS: int = Field(
        default=900,
        description="OCR of one PDF stops after this long and the document yields no text (held for review), so one long scan cannot hold a public worker for hours. 0 = no limit.",
    )
    OCR_MAX_PAGE_PIXELS: int = Field(
        default=12_000_000,
        description=(
            "Largest image OCR renders for one page. A page that would exceed it at 300 DPI is rendered at a lower DPI. "
            "A legal-size page at 300 DPI is 10.7 MP, so ordinary pages are unaffected; a 34 x 50 inch photo page "
            "(senate.gov.pk, 16 October 2026) would be 151 MP and drove a public worker to 1.9 GB."
        ),
    )

    # --------------------------------------------------------------- archive
    GOOGLE_DRIVE_ENABLED: bool = Field(default=False)
    DRIVE_FOLDER_ROOT: str = Field(default="")
    GOOGLE_APPLICATION_CREDENTIALS_JSON: Optional[SecretStr] = Field(default=None)
    DRIVE_UPLOAD_CONCURRENCY: int = Field(default=2)
    DRIVE_CHUNK_SIZE_MB: int = Field(default=8)
    ARCHIVE_ENABLED: bool = Field(default=True)
    ARCHIVE_LOCAL_PATH: str = Field(default="", description="local_path target root. Blank = not configured.")
    ARCHIVE_S3_ENDPOINT: str = Field(default="")
    ARCHIVE_S3_BUCKET: str = Field(default="")
    ARCHIVE_S3_ACCESS_KEY: str = Field(default="")
    ARCHIVE_S3_SECRET_KEY: SecretStr = Field(default=SecretStr(""))
    ARCHIVE_S3_REGION: str = Field(default="")
    ARCHIVE_SFTP_HOST: str = Field(default="")
    ARCHIVE_SFTP_PORT: int = Field(default=22)
    ARCHIVE_SFTP_USER: str = Field(default="")
    ARCHIVE_SFTP_PASSWORD: SecretStr = Field(default=SecretStr(""))
    ARCHIVE_SFTP_ROOT: str = Field(default="")
    ARCHIVE_SMB_SERVER: str = Field(default="")
    ARCHIVE_SMB_SHARE: str = Field(default="")
    ARCHIVE_SMB_USER: str = Field(default="")
    ARCHIVE_SMB_PASSWORD: SecretStr = Field(default=SecretStr(""))
    ARCHIVE_SMB_ROOT: str = Field(default="")
    ARCHIVE_DROPBOX_TOKEN: SecretStr = Field(default=SecretStr(""))
    ARCHIVE_DROPBOX_ROOT: str = Field(default="")
    ARCHIVE_ONEDRIVE_TOKEN: SecretStr = Field(default=SecretStr(""))
    ARCHIVE_ONEDRIVE_ROOT: str = Field(default="")

    # ------------------------------------------------------------- treatment
    TREATMENT_MODEL: str = Field(default="", description="External classifier for PUBLIC residue. Blank = deterministic only.")
    TREATMENT_MIN_CONFIDENCE: float = Field(default=0.7)
    LOCAL_TREATMENT_BASE_URL: str = Field(default="", description="On-prem OpenAI-compatible endpoint for login_session residue.")
    LOCAL_TREATMENT_MODEL: str = Field(default="")
    LOCAL_TREATMENT_API_KEY: SecretStr = Field(default=SecretStr(""))
    TREATMENT_RECONCILE_ENABLED: bool = Field(default=True, description="Run periodic reconciliation for unresolved treatment citation links.")
    TREATMENT_RECONCILE_LOOKBACK_HOURS: int = Field(default=168, description="Only unresolved treatments newer than this lookback are scanned.")
    TREATMENT_RECONCILE_BATCH_SIZE: int = Field(default=500, description="Maximum unresolved treatments scanned per reconcile pass.")
    TREATMENT_RECONCILE_INTERVAL_SECONDS: int = Field(default=3600, description="Celery beat interval for treatment citation-link reconciliation.")
    JUDGMENT_CITATION_RECONCILE_ENABLED: bool = Field(
        default=True,
        description="Run periodic reconciliation for judgment citation graph edges extracted from judgment text.",
    )
    JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS: int = Field(
        default=168,
        description="Only judgments newer than this lookback are scanned for citation-edge reconciliation.",
    )
    JUDGMENT_CITATION_RECONCILE_BATCH_SIZE: int = Field(
        default=300,
        description="Maximum judgments scanned per judgment citation relation reconciliation pass.",
    )
    JUDGMENT_CITATION_RECONCILE_INTERVAL_SECONDS: int = Field(
        default=3600,
        description="Celery beat interval for judgment citation relation reconciliation.",
    )

    # ----------------------------------------------------------- scrapegraph
    SGAI_ENABLED: bool = Field(default=True)
    SGAI_MODE: str = Field(default="hybrid")
    SGAI_API_KEY: SecretStr = Field(default=SecretStr(""))
    SGAI_MANAGED_PUBLIC_ONLY: bool = Field(default=True)
    SGAI_LOCAL_ENABLED: bool = Field(default=True)
    SGAI_LOCAL_LLM_PROVIDER: str = Field(default="ollama")
    SGAI_LOCAL_LLM_MODEL: str = Field(default="")
    SGAI_LOCAL_LLM_BASE_URL: str = Field(default="")
    SGAI_LOCAL_TRUSTED_HOSTS: str = Field(
        default="",
        description="Comma list of on-prem hostnames the local engine may use besides private/loopback addresses. Login-session text never goes elsewhere.",
    )
    SGAI_TIMEOUT_SECONDS: int = Field(default=120)
    SGAI_MAX_RETRIES: int = Field(default=2)
    SGAI_DAILY_CREDIT_CAP: Optional[int] = Field(default=None)
    SGAI_CACHE_ENABLED: bool = Field(default=True)
    SGAI_SCHEMA_VERSION: int = Field(default=1)
    SGAI_STEALTH_ALLOWED: bool = Field(default=False)
    SGAI_FAIL_OPEN_TO_DETERMINISTIC: bool = Field(default=True)
    SGAI_CIRCUIT_BREAKER_FAILURES: int = Field(default=5)
    SGAI_CIRCUIT_BREAKER_COOLDOWN_SECONDS: int = Field(default=900)
    SGAI_PUBLIC_TEST_URL: str = Field(default="", description="The only URL /admin/scrapegraph/test-public may fetch. Blank = endpoint disabled.")
    SGAI_DEFAULT_MIN_CONFIDENCE: float = Field(default=0.85)

    # ------------------------------------------------------------ validators
    @field_validator("SGAI_DAILY_CREDIT_CAP", "OPENAI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS_JSON", mode="before")
    @classmethod
    def _blank_is_none(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator(
        "PLS_EARLIEST_YEAR",
        "EMBEDDING_DIM",
        "OPENAI_EMBEDDING_DIMENSIONS",
        "SGAI_SCHEMA_VERSION",
        "SGAI_TIMEOUT_SECONDS",
        "SGAI_MAX_RETRIES",
        "VOLUME_END_GAP",
        "RECONNECT_SECONDS",
        "INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE",
        "INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS",
        "INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS",
        "TREATMENT_RECONCILE_LOOKBACK_HOURS",
        "TREATMENT_RECONCILE_BATCH_SIZE",
        "TREATMENT_RECONCILE_INTERVAL_SECONDS",
        "JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS",
        "JUDGMENT_CITATION_RECONCILE_BATCH_SIZE",
        "JUDGMENT_CITATION_RECONCILE_INTERVAL_SECONDS",
        "PLAYWRIGHT_MAX_HTML_BYTES",
        "PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD",
        "PLS_ARCHIVED_GRID_MAX_ROWS",
        "PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_SECONDS",
        "PLS_CITATION_GRID_MAX_DETAIL",
        "PLS_CITATION_GRID_SCAN_WINDOW",
        "BACKFILL_PLS_CITATION_GRID_MAX_DETAIL",
        "BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW",
        "PLS_RUN_MAX_MINUTES",
        "BACKFILL_PLS_RUN_MAX_MINUTES",
        "PLS_CASE_DESCRIPTION_WAIT_SECONDS",
        mode="before",
    )
    @classmethod
    def _blank_int_is_default(cls, v: Any, info) -> Any:
        if isinstance(v, str) and not v.strip():
            return cls.model_fields[info.field_name].default
        return v

    @field_validator("LOG_LEVEL", mode="before")
    @classmethod
    def _normalise_log_level(cls, v: Any) -> Any:
        return v.upper() if isinstance(v, str) else v

    @field_validator("ENVIRONMENT", mode="before")
    @classmethod
    def _normalise_environment(cls, v: Any) -> Any:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("ENCRYPTION_KEY")
    @classmethod
    def _validate_fernet_key(cls, v: str) -> str:
        if not v:
            return v
        try:
            Fernet(v.encode())
        except Exception as exc:  # pragma: no cover - message path
            raise ValueError("ENCRYPTION_KEY must be a urlsafe base64 32-byte Fernet key") from exc
        return v

    @field_validator("DATABASE_URL")
    @classmethod
    def _validate_database_url(cls, v: str) -> str:
        if not v.startswith("postgresql"):
            raise ValueError("DATABASE_URL must be a PostgreSQL URL")
        return v

    @field_validator("REDIS_URL")
    @classmethod
    def _validate_redis_url(cls, v: str) -> str:
        if not v.startswith(("redis://", "rediss://")):
            raise ValueError("REDIS_URL must start with redis:// or rediss://")
        return v

    @field_validator("SGAI_MODE")
    @classmethod
    def _validate_sgai_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in EXTRACTION_MODES:
            raise ValueError(f"SGAI_MODE must be one of {EXTRACTION_MODES}")
        return v

    @field_validator("HARVEST_MODE")
    @classmethod
    def _validate_harvest_mode(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in HARVEST_MODES:
            raise ValueError(f"HARVEST_MODE must be one of {HARVEST_MODES}")
        return v

    @field_validator("PLS_SUBSCRIBED_REPORTERS", "PLS_JOURNALS", "PLS_JOURNALS_B")
    @classmethod
    def _validate_reporters(cls, v: str) -> str:
        if not v:
            return ""
        items = [x.strip() for x in v.split(",") if x.strip()]
        bad = [x for x in items if not re.fullmatch(r"[A-Za-z]{2,8}", x)]
        if bad:
            raise ValueError(f"Reporter codes must be alphabetic: {bad}")
        return ",".join(items)

    @model_validator(mode="after")
    def _cross_field_guards(self) -> "Settings":
        if self.SCRAPER_DELAY_MIN < 0 or self.SCRAPER_DELAY_MAX < self.SCRAPER_DELAY_MIN:
            raise ValueError("SCRAPER_DELAY_MIN/MAX must be non-negative with MAX >= MIN")
        if self.ALLOW_LOGIN_SCRAPING and self.ENVIRONMENT != "chambers":
            raise ValueError("ALLOW_LOGIN_SCRAPING=true requires ENVIRONMENT=chambers")
        if self.ALLOW_LOGIN_SCRAPING and not self.SGAI_MANAGED_PUBLIC_ONLY:
            raise ValueError("SGAI_MANAGED_PUBLIC_ONLY=false is not permitted while login_session sources are enabled")
        if self.SGAI_STEALTH_ALLOWED:
            raise ValueError("SGAI_STEALTH_ALLOWED=true is not permitted (never for PakistanLawSite; no partner decision recorded)")
        if self.SGAI_MODE == "scrapegraph_local" and self.SGAI_ENABLED and not (self.SGAI_LOCAL_LLM_BASE_URL and self.SGAI_LOCAL_LLM_MODEL):
            if not self.SGAI_FAIL_OPEN_TO_DETERMINISTIC:
                raise ValueError("scrapegraph_local selected without SGAI_LOCAL_LLM_BASE_URL/SGAI_LOCAL_LLM_MODEL and fail-open disabled")
        if self.LOGIN_DELAY_MIN < 0 or self.LOGIN_DELAY_MAX < self.LOGIN_DELAY_MIN:
            raise ValueError("LOGIN_DELAY_MIN/MAX must be non-negative with MAX >= MIN")
        if self.PAGES_PER_HOUR <= 0 or self.PAGES_PER_DAY <= 0:
            raise ValueError("PAGES_PER_HOUR and PAGES_PER_DAY must be positive")
        if self.PLAYWRIGHT_MAX_HTML_BYTES <= 0 or self.PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD <= 0:
            raise ValueError("PLAYWRIGHT_MAX_HTML_BYTES and PLAYWRIGHT_OVERSIZE_INPUT_THRESHOLD must be positive")
        if self.LOGIN_SESSION_CONCURRENCY not in (1, 2):
            raise ValueError("LOGIN_SESSION_CONCURRENCY must be 1 or 2")
        if self.LOGIN_RECOVERY_COOLDOWN_MINUTES < 0 or self.LOGIN_RECOVERY_SCHEDULE_SECONDS <= 0 or self.LOGIN_RECOVERY_SUBMIT_WAIT_SECONDS < 0:
            raise ValueError("LOGIN_RECOVERY_* settings must be non-negative (schedule positive)")
        if self.SPOT_CHECK_SCHEDULE_SECONDS <= 0 or self.SPOT_CHECK_JUDGMENTS < 0 or self.SPOT_CHECK_STATUTES < 0:
            raise ValueError("SPOT_CHECK_SCHEDULE_SECONDS must be positive and SPOT_CHECK_JUDGMENTS/STATUTES >= 0")
        if self.PLS_SEARCH_HARVEST_TAKEOVER_MINUTES < 0:
            raise ValueError("PLS_SEARCH_HARVEST_TAKEOVER_MINUTES must be >= 0")
        if self.PLS_KEEPALIVE_SCHEDULE_SECONDS <= 0 or self.PLS_STALL_WATCHDOG_SECONDS <= 0:
            raise ValueError("PLS_KEEPALIVE_SCHEDULE_SECONDS and PLS_STALL_WATCHDOG_SECONDS must be positive")
        if self.PLS_ARCHIVED_GRID_MAX_ROWS <= 0:
            raise ValueError("PLS_ARCHIVED_GRID_MAX_ROWS must be positive")
        if self.PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_SECONDS <= 0:
            raise ValueError("PLS_ARCHIVED_GRID_SNAPSHOT_TIMEOUT_SECONDS must be positive")
        if self.PLS_CITATION_GRID_MAX_DETAIL <= 0:
            raise ValueError("PLS_CITATION_GRID_MAX_DETAIL must be positive")
        if self.PLS_CITATION_GRID_SCAN_WINDOW <= 0:
            raise ValueError("PLS_CITATION_GRID_SCAN_WINDOW must be positive")
        if self.BACKFILL_PLS_CITATION_GRID_MAX_DETAIL <= 0:
            raise ValueError("BACKFILL_PLS_CITATION_GRID_MAX_DETAIL must be positive")
        if self.BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW <= 0:
            raise ValueError("BACKFILL_PLS_CITATION_GRID_SCAN_WINDOW must be positive")
        if self.PLS_RUN_MAX_MINUTES < 0 or self.BACKFILL_PLS_RUN_MAX_MINUTES < 0:
            raise ValueError("PLS_RUN_MAX_MINUTES and BACKFILL_PLS_RUN_MAX_MINUTES must be >= 0")
        if self.PLS_CASE_DESCRIPTION_WAIT_SECONDS < 0:
            raise ValueError("PLS_CASE_DESCRIPTION_WAIT_SECONDS must be >= 0")
        if not 0 < float(self.PROMOTE_PREFERRED_SHARE) <= 1:
            raise ValueError("PROMOTE_PREFERRED_SHARE must be in (0, 1]")
        if self.BACKFILL_LOGIN_DELAY_MIN < 0 or self.BACKFILL_LOGIN_DELAY_MAX < self.BACKFILL_LOGIN_DELAY_MIN:
            raise ValueError("BACKFILL_LOGIN_DELAY_MIN/MAX must be non-negative with MAX >= MIN")
        if self.BACKFILL_PAGES_PER_HOUR <= 0 or self.BACKFILL_PAGES_PER_DAY <= 0:
            raise ValueError("BACKFILL_PAGES_PER_HOUR and BACKFILL_PAGES_PER_DAY must be positive")
        if self.BACKFILL_LOGIN_SESSION_CONCURRENCY not in (1, 2):
            raise ValueError("BACKFILL_LOGIN_SESSION_CONCURRENCY must be 1 or 2")
        if (
            self.DISPATCH_LOOP_SECONDS <= 0
            or self.UPDATE_CADENCE_HOURS <= 0
            or self.BACKFILL_SOURCE_FREQUENCY_MINUTES <= 0
            or self.BLOCK_RETRY_COOLDOWN_MINUTES <= 0
            or self.BLOCK_RETRY_MAX_ATTEMPTS <= 0
        ):
            raise ValueError("dispatch, cadence, and block-retry settings must be positive")
        if self.BACKFILL_TARGET_JUDGMENTS < 0 or self.BACKFILL_TARGET_STATUTES < 0:
            raise ValueError("BACKFILL_TARGET_JUDGMENTS/STATUTES must be >= 0")
        if self.EMBEDDING_DIM <= 0:
            raise ValueError("EMBEDDING_DIM must be positive")
        if self.INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE <= 0:
            raise ValueError("INSTRUMENT_RELATION_RECONCILE_BATCH_SIZE must be positive")
        if self.INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS <= 0:
            raise ValueError("INSTRUMENT_RELATION_RECONCILE_WINDOW_HOURS must be positive")
        if self.INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS <= 0:
            raise ValueError("INSTRUMENT_RELATION_RECONCILE_SCHEDULE_SECONDS must be positive")
        if (
            self.TREATMENT_RECONCILE_LOOKBACK_HOURS <= 0
            or self.TREATMENT_RECONCILE_BATCH_SIZE <= 0
            or self.TREATMENT_RECONCILE_INTERVAL_SECONDS <= 0
        ):
            raise ValueError("TREATMENT_RECONCILE_LOOKBACK_HOURS, TREATMENT_RECONCILE_BATCH_SIZE and TREATMENT_RECONCILE_INTERVAL_SECONDS must be positive")
        if (
            self.JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS <= 0
            or self.JUDGMENT_CITATION_RECONCILE_BATCH_SIZE <= 0
            or self.JUDGMENT_CITATION_RECONCILE_INTERVAL_SECONDS <= 0
        ):
            raise ValueError(
                "JUDGMENT_CITATION_RECONCILE_LOOKBACK_HOURS, JUDGMENT_CITATION_RECONCILE_BATCH_SIZE and "
                "JUDGMENT_CITATION_RECONCILE_INTERVAL_SECONDS must be positive"
            )
        if self.PLS_USER or self.PLS_PASS.get_secret_value() or self.PLS_USER_B or self.PLS_PASS_B.get_secret_value():
            logger.warning("PLS_USER/PLS_PASS are deprecated and ignored: PakistanLawSite uses human login only")
        # legacy aliases
        if self.OPENAI_EMBEDDING_MODEL and self.OPENAI_EMBEDDING_MODEL != self.EMBEDDING_MODEL:
            object.__setattr__(self, "EMBEDDING_MODEL", self.OPENAI_EMBEDDING_MODEL)
        if self.OPENAI_EMBEDDING_DIMENSIONS and self.OPENAI_EMBEDDING_DIMENSIONS != self.EMBEDDING_DIM:
            object.__setattr__(self, "EMBEDDING_DIM", self.OPENAI_EMBEDDING_DIMENSIONS)
        return self

    # ------------------------------------------------------------ properties
    @property
    def celery_broker_url(self) -> str:
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def celery_result_backend(self) -> str:
        return self.CELERY_RESULT_BACKEND or self.REDIS_URL

    @property
    def database_sync_url(self) -> str:
        return self.DATABASE_URL.replace("+asyncpg", "+psycopg2").replace("+asyncpg", "")

    @property
    def subscribed_reporters(self) -> List[str]:
        return [x for x in self.PLS_SUBSCRIBED_REPORTERS.split(",") if x]

    @property
    def grid_journals(self) -> List[str]:
        raw = self.PLS_GRID_JOURNALS or self.PLS_SUBSCRIBED_REPORTERS
        return list(dict.fromkeys(x.strip() for x in (raw or "").split(",") if x.strip()))

    @property
    def tier3_vocabulary(self) -> List[str]:
        return [x.strip() for x in self.PLS_TIER3_VOCABULARY.split(",") if x.strip()]

    @property
    def sgai_managed_configured(self) -> bool:
        return bool(self.SGAI_ENABLED and self.SGAI_API_KEY.get_secret_value())

    @property
    def sgai_local_configured(self) -> bool:
        return bool(self.SGAI_ENABLED and self.SGAI_LOCAL_ENABLED and self.SGAI_LOCAL_LLM_BASE_URL and self.SGAI_LOCAL_LLM_MODEL)

    @property
    def login_scraping_effective(self) -> bool:
        return self.ALLOW_LOGIN_SCRAPING and self.ENVIRONMENT == "chambers"

    @property
    def reader_role_configured(self) -> bool:
        return bool(self.SIKANDER_READER_PASSWORD.get_secret_value())

    def get_fernet(self) -> Fernet:
        if not self.ENCRYPTION_KEY:
            raise RuntimeError("ENCRYPTION_KEY is not configured")
        return Fernet(self.ENCRYPTION_KEY.encode())

    def encrypt_value(self, plaintext: str) -> str:
        return self.get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt_value(self, token: str) -> str:
        return self.get_fernet().decrypt(token.encode("ascii")).decode("utf-8")

    def not_configured(self) -> List[str]:
        """Firm values that are blank. Shown on the dashboard as NOT CONFIGURED."""
        missing: List[str] = []
        if not self.DEPLOY_REGION:
            missing.append("DEPLOY_REGION")
        if not self.PLS_SUBSCRIBED_REPORTERS:
            missing.append("PLS_SUBSCRIBED_REPORTERS")
        if not self.PLS_EARLIEST_YEAR:
            missing.append("PLS_EARLIEST_YEAR")
        if not self.SGAI_API_KEY.get_secret_value():
            missing.append("SGAI_API_KEY")
        if not (self.SGAI_LOCAL_LLM_BASE_URL and self.SGAI_LOCAL_LLM_MODEL):
            missing.append("SGAI_LOCAL_LLM_BASE_URL/SGAI_LOCAL_LLM_MODEL")
        if self.SGAI_DAILY_CREDIT_CAP is None:
            missing.append("SGAI_DAILY_CREDIT_CAP")
        if not self.SIKANDER_READER_PASSWORD.get_secret_value():
            missing.append("SIKANDER_READER_PASSWORD")
        if not self.SGAI_PUBLIC_TEST_URL:
            missing.append("SGAI_PUBLIC_TEST_URL")
        if not self.OPENAI_API_KEY:
            missing.append("OPENAI_API_KEY")
        if not (self.READER_MODEL or self.TREATMENT_MODEL):
            missing.append("READER_MODEL (the reading agent for unreported judgments)")
        return missing

    def model_dump_safe(self) -> Dict[str, Any]:
        """Dump every setting with secrets masked. Used by the dashboard and logs."""
        out: Dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if name in SECRET_FIELD_NAMES or isinstance(value, SecretStr):
                raw = value.get_secret_value() if isinstance(value, SecretStr) else value
                out[name] = "CONFIGURED" if raw else "NOT CONFIGURED"
            else:
                out[name] = value
        return out


def redact_secrets(text: str, settings: "Settings") -> str:
    """Remove any configured secret value from free text before logging or prompting."""
    if not text:
        return text
    values: List[str] = []
    for name in SECRET_FIELD_NAMES:
        v = getattr(settings, name, None)
        if isinstance(v, SecretStr):
            v = v.get_secret_value()
        if v and len(str(v)) >= 6:
            values.append(str(v))
    for v in sorted(values, key=len, reverse=True):
        text = text.replace(v, "[REDACTED]")
    return text


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

