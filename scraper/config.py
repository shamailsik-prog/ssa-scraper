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
    "GOOGLE_APPLICATION_CREDENTIALS_JSON",
    "PLS_PASS",
    "PLS_PASS_B",
    "ARCHIVE_S3_SECRET_KEY",
    "ARCHIVE_SFTP_PASSWORD",
    "ARCHIVE_SMB_PASSWORD",
    "ARCHIVE_DROPBOX_TOKEN",
    "ARCHIVE_ONEDRIVE_TOKEN",
    "LOCAL_TREATMENT_API_KEY",
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
    SIKANDER_READER_ROLE: str = Field(default="sikander_reader")
    SIKANDER_READER_PASSWORD: SecretStr = Field(default=SecretStr(""), description="Blank = role not created; dashboard shows NOT CONFIGURED.")
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
    LOGIN_SESSION_CONCURRENCY: int = Field(default=1)
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
    BACKFILL_LOGIN_DELAY_MIN: float = Field(default=0.4, description="Backfill mode minimum delay between login-session page fetches.")
    BACKFILL_LOGIN_DELAY_MAX: float = Field(default=1.0, description="Backfill mode maximum delay between login-session page fetches.")
    BACKFILL_PAGES_PER_HOUR: int = Field(default=10000, description="Backfill mode login-session page budget per hour.")
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
    PLS_SEARCH_URL: str = Field(default="https://www.pakistanlawsite.com/Login/CitationSearch")
    PLS_ARCHIVED_GRID_MAX_ROWS: int = Field(default=200, description="Maximum rows to materialize from #archivedpatientGrid when compacting CitationSearch HTML (keep low — full DOM walks hang).")
    PLS_CITATION_GRID_MAX_DETAIL: int = Field(default=40, description="Max detail pages to fetch per citation-grid login_session run.")
    PLS_SUBSCRIBED_REPORTERS: str = Field(default="", description="Comma list. Firm value. Blank = NOT CONFIGURED; Tier 1 idles.")
    PLS_EARLIEST_YEAR: int = Field(default=0, description="Firm value. 0 = NOT CONFIGURED; Tier 1 covers current year only.")
    PLS_TIER3_VOCABULARY: str = Field(default="", description="Optional comma list seeding the Tier 3 vocabulary sweep.")
    PLS_MAX_FAILOVER_RETRIES: int = Field(default=0)  # legacy; automatic failover past a block is forbidden
    PLS_SESSION_TTL_MINUTES: int = Field(default=720)
    # Legacy automated-login names. Accepted so old .env files do not break; never used to log in.
    PLS_USER: str = Field(default="")
    PLS_PASS: SecretStr = Field(default=SecretStr(""))
    PLS_JOURNALS: str = Field(default="")
    PLS_USER_B: str = Field(default="")
    PLS_PASS_B: SecretStr = Field(default=SecretStr(""))
    PLS_JOURNALS_B: str = Field(default="")

    # ----------------------------------------------------------- pdf/storage
    PDF_STORAGE_PATH: str = Field(default="/app/live")
    STATE_STORAGE_PATH: str = Field(default="/app/state")
    RAW_STORAGE_PATH: str = Field(default="/app/raw", description="Raw HTML/text/PDF preservation root.")
    PDF_MAX_SIZE_MB: int = Field(default=100)
    OCR_ENABLED: bool = Field(default=True)
    OCR_LANGUAGE: str = Field(default="eng")
    OCR_DPI: int = Field(default=300)

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
        "PLS_CITATION_GRID_MAX_DETAIL",
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
        if self.PLS_ARCHIVED_GRID_MAX_ROWS <= 0:
            raise ValueError("PLS_ARCHIVED_GRID_MAX_ROWS must be positive")
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
