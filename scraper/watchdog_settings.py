"""Settings for the PakistanLawSite throughput watchdog, multi-query search-harvest ticks and the PakistanCode
auto-transition. Read from the environment (.env via docker compose env_file) like scraper.config."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class PlsWatchdogSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=True)

    PLS_SEARCH_HARVEST_TICK_BUDGET_SECONDS: int = Field(
        default=1500, description="One search-harvest tick keeps taking queries until this many seconds have passed (0 = one query per tick)."
    )
    PLS_SEARCH_HARVEST_MAX_QUERIES_PER_TICK: int = Field(default=0, description="Optional cap on queries per tick (0 = time budget only).")
    PLS_SEARCH_HARVEST_MAX_ATTEMPTS: int = Field(
        default=3, description="A search query failing this many times is marked failed (the watchdog requeues transient failures later)."
    )
    PLS_SEARCH_HARVEST_ALL_OFFERED_REPORTERS: bool = Field(
        default=True, description="Seed journal x year queries for every journal the search form offers, after the subscribed ones."
    )
    PLS_HARVEST_STRATEGY: str = Field(
        default="search",
        description="search = journal x year searches on both slots are the main PLS path and the citation grid is only a "
        "reconciliation pass every PLS_GRID_RECONCILE_HOURS; grid = the old grid-first behaviour.",
    )
    PLS_GRID_RECONCILE_HOURS: float = Field(default=24.0, description="With the search strategy, one citation-grid job at most this often.")
    PLS_SEARCH_HARVEST_EARLIEST_YEAR: int = Field(default=1947, description="First year of the journal x year search plan.")
    PLS_SEARCH_RECHECK_DONE_DAYS: int = Field(default=7, description="Done cells of the current and previous year are searched again after this many days.")
    PLS_THROUGHPUT_WATCHDOG_SECONDS: int = Field(default=600, description="Beat cadence of the PLS throughput watchdog.")
    PLS_THROUGHPUT_IDLE_MINUTES: int = Field(default=30, description="PLS is idle after this many minutes with no page fetched and no judgment promoted.")
    PLS_WATCHDOG_STALE_JOB_MINUTES: int = Field(default=75, description="A running PLS job not updated for this long is treated as dead.")
    PLS_AUTO_UNPAUSE_PAKISTANCODE: bool = Field(
        default=False,
        description="When true, the watchdog moves PakistanCode from PAUSED to ACTIVE once PLS citation coverage reaches PLS_AUTO_UNPAUSE_COVERAGE.",
    )
    PLS_AUTO_UNPAUSE_COVERAGE: float = Field(
        default=0.95, description="Collected / discovered share (every subscribed journal discovered) that triggers the PakistanCode switch."
    )


wsettings = PlsWatchdogSettings()
