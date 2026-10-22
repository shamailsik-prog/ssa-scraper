"""R3/R4: safe test database naming and refusal to truncate non-test DB names."""

from __future__ import annotations

import os

import pytest

from tests import xdist_db

pytestmark = pytest.mark.usefixtures("clean_db")


def test_worker_database_name_serial_keeps_base(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    assert xdist_db.worker_database_name("legal_scraper_test") == "legal_scraper_test"


def test_worker_database_name_xdist_appends_worker(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw2")
    assert xdist_db.worker_database_name("legal_scraper_test") == "legal_scraper_test_gw2"


def test_apply_worker_database_url_serial_does_not_rewrite(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    url = "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test"
    monkeypatch.setenv("DATABASE_URL", url)
    assert xdist_db.apply_worker_database_url() == url
    assert os.environ["DATABASE_URL"] == url


def test_apply_worker_database_url_xdist_rewrites(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    url = "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test"
    monkeypatch.setenv("DATABASE_URL", url)
    out = xdist_db.apply_worker_database_url()
    assert out.endswith("/legal_scraper_test_gw1")
    assert os.environ["DATABASE_URL"] == out


def test_apply_worker_database_url_xdist_rejects_unsafe_base(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.delenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")
    with pytest.raises(RuntimeError, match="does not look like a test database"):
        xdist_db.apply_worker_database_url()


def test_rolecheck_database_name_allowed_when_base_is_test_db():
    xdist_db.refuse_unsafe_database_url("postgresql://localhost/legal_scraper_test_rolecheck_deadbeef0123")


def test_apply_worker_database_url_rejects_evil_worker_db_name(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    url = "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper_test"
    monkeypatch.setenv("DATABASE_URL", url)

    def evil_name(base: str) -> str:
        return f"{base}_gw0_evil"

    monkeypatch.setattr(xdist_db, "worker_database_name", evil_name)
    with pytest.raises(RuntimeError, match="does not match expected"):
        xdist_db.apply_worker_database_url()


def test_refuse_unsafe_worker_database_rejects_tampered_derived_name(monkeypatch):
    """Mutant E1: derived name must match worker_database_name(base), not an arbitrary suffix."""
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    with pytest.raises(RuntimeError, match="does not match expected"):
        xdist_db.refuse_unsafe_worker_database_name("legal_scraper_test", "legal_scraper_test_gw0_evil")


def test_apply_worker_database_url_raises_for_production_db_name(monkeypatch):
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")
    with pytest.raises(RuntimeError, match="does not look like a test database"):
        xdist_db.apply_worker_database_url()


def test_refuses_legal_scraper_without_opt_in(monkeypatch):
    monkeypatch.delenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, raising=False)
    with pytest.raises(RuntimeError, match="does not look like a test database"):
        xdist_db.refuse_unsafe_database_url("postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")


def test_refuses_substring_test_false_positives(monkeypatch):
    monkeypatch.delenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, raising=False)
    for name in ("latest_prod", "contest", "attest_data"):
        with pytest.raises(RuntimeError, match="does not look like a test database"):
            xdist_db.refuse_unsafe_database_url(f"postgresql://localhost/{name}")


def test_accepts_test_prefix_and_test_suffix(monkeypatch):
    monkeypatch.delenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, raising=False)
    xdist_db.refuse_unsafe_database_url("postgresql://localhost/legal_scraper_test")
    xdist_db.refuse_unsafe_database_url("postgresql://localhost/test_legal_scraper")


def test_allows_opt_in_for_non_test_name(monkeypatch):
    monkeypatch.setenv(xdist_db.CORPUS_TEST_ALLOW_UNSAFE_DATABASE, "1")
    xdist_db.refuse_unsafe_database_url("postgresql+asyncpg://legal:legal@localhost:5432/legal_scraper")


def test_apply_worker_redis_url_xdist_uses_distinct_db(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    out = xdist_db.apply_worker_redis_url()
    assert out.endswith("/4")


def test_redis_logical_db_uses_base_plus_one_plus_worker_index():
    assert xdist_db.redis_logical_db_index_for_worker("gw3", "redis://localhost:6379/0") == 4
    assert xdist_db.redis_logical_db_index_for_worker("gw0", "redis://localhost:6379/2") == 3


def test_redis_logical_db_rejects_non_gw_worker_id():
    with pytest.raises(RuntimeError, match="not gwN"):
        xdist_db.redis_logical_db_index_for_worker("worker_a", "redis://localhost:6379/0")


def test_redis_logical_db_rejects_overflow_worker():
    with pytest.raises(RuntimeError, match="only 0–15"):
        xdist_db.redis_logical_db_index_for_worker("gw15", "redis://localhost:6379/0")


def test_apply_worker_redis_url_respects_redis_url_base(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw1")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/2")
    out = xdist_db.apply_worker_redis_url()
    assert out.endswith("/4")
