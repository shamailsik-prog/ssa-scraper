"""Settings guards (Amendment §15) and database role isolation (tests 35–37, 4)."""

from __future__ import annotations

import json
import logging

import asyncpg
import pytest
from pydantic import ValidationError

from scraper.config import Settings, settings
from scraper.models import CONTRACT_TABLES, INTERNAL_TABLES

BASE = dict(DATABASE_URL="postgresql+asyncpg://x:y@localhost/db", REDIS_URL="redis://localhost/0")


def test_login_scraping_requires_chambers():
    with pytest.raises(ValidationError):
        Settings(**BASE, ALLOW_LOGIN_SCRAPING=True, ENVIRONMENT="cloud")
    s = Settings(**BASE, ALLOW_LOGIN_SCRAPING=True, ENVIRONMENT="chambers")
    assert s.login_scraping_effective


def test_managed_public_only_cannot_be_disabled_with_login_sources():
    with pytest.raises(ValidationError):
        Settings(**BASE, ALLOW_LOGIN_SCRAPING=True, ENVIRONMENT="chambers", SGAI_MANAGED_PUBLIC_ONLY=False)


def test_stealth_is_refused():
    with pytest.raises(ValidationError):
        Settings(**BASE, SGAI_STEALTH_ALLOWED=True)


def test_local_mode_without_endpoint_requires_fail_open():
    with pytest.raises(ValidationError):
        Settings(**BASE, SGAI_MODE="scrapegraph_local", SGAI_FAIL_OPEN_TO_DETERMINISTIC=False)
    assert Settings(**BASE, SGAI_MODE="scrapegraph_local", SGAI_FAIL_OPEN_TO_DETERMINISTIC=True)


def test_login_session_concurrency_must_be_one_or_two():
    assert Settings(**BASE, LOGIN_SESSION_CONCURRENCY=1)
    assert Settings(**BASE, LOGIN_SESSION_CONCURRENCY=2)
    for value in (0, 3):
        with pytest.raises(ValidationError):
            Settings(**BASE, LOGIN_SESSION_CONCURRENCY=value)


def test_every_required_sgai_setting_exists():
    for name in ["SGAI_ENABLED", "SGAI_MODE", "SGAI_API_KEY", "SGAI_MANAGED_PUBLIC_ONLY", "SGAI_LOCAL_ENABLED", "SGAI_LOCAL_LLM_PROVIDER", "SGAI_LOCAL_LLM_MODEL", "SGAI_LOCAL_LLM_BASE_URL", "SGAI_TIMEOUT_SECONDS", "SGAI_MAX_RETRIES", "SGAI_DAILY_CREDIT_CAP", "SGAI_CACHE_ENABLED", "SGAI_SCHEMA_VERSION", "SGAI_STEALTH_ALLOWED", "SGAI_FAIL_OPEN_TO_DETERMINISTIC"]:
        assert hasattr(settings, name), name
    assert settings.SGAI_STEALTH_ALLOWED is False


def test_api_key_never_in_safe_dump_or_logs(caplog):
    """Test 4: SGAI_API_KEY is never returned by API/dashboard/logs."""
    s = Settings(**BASE, SGAI_API_KEY="sgai-SECRET-KEY-VALUE-123456")
    dump = json.dumps(s.model_dump_safe())
    assert "sgai-SECRET" not in dump
    assert s.model_dump_safe()["SGAI_API_KEY"] == "CONFIGURED"
    assert "sgai-SECRET" not in repr(s.SGAI_API_KEY) and "sgai-SECRET" not in str(s.SGAI_API_KEY)
    with caplog.at_level(logging.DEBUG):
        logging.getLogger("x").info("settings %s", s.model_dump_safe())
    assert "sgai-SECRET" not in caplog.text


def test_not_configured_lists_blank_firm_values():
    s = Settings(**BASE)
    nc = s.not_configured()
    assert "DEPLOY_REGION" in nc and "PLS_SUBSCRIBED_REPORTERS" in nc and "SGAI_API_KEY" in nc


# --------------------------------------------------------------------------- roles
def _dsn(user: str, pw: str) -> str:
    url = settings.DATABASE_URL.replace("postgresql+asyncpg://", "")
    hostpart = url.split("@", 1)[1]
    return f"postgresql://{user}:{pw}@{hostpart}"


async def test_reader_can_select_contract_tables():
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        for t in CONTRACT_TABLES:
            await conn.fetch(f'SELECT * FROM "{t}" LIMIT 1')
    finally:
        await conn.close()


async def test_reader_cannot_read_internal_tables():
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        for t in ("extraction_audit", "scraper_staging", "statutes_staging", "browser_session_slots", "archive_targets", "scrapegraph_cache", "scraper_sources", "crawl_frontier"):
            assert t in INTERNAL_TABLES
            with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
                await conn.fetch(f'SELECT * FROM "{t}" LIMIT 1')
    finally:
        await conn.close()


async def test_reader_cannot_write():
    conn = await asyncpg.connect(_dsn(settings.SIKANDER_READER_ROLE, settings.SIKANDER_READER_PASSWORD.get_secret_value()))
    try:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("INSERT INTO corpus_metadata(key, value) VALUES ('x', 'y')")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("UPDATE court SET name = name")
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("DELETE FROM judgment")
    finally:
        await conn.close()
