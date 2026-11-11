"""A robots.txt transport failure stays HTTP 599 and keeps the exception class and message.

Job stats only see the RobotsUnavailable text. TLS, timeout and connection failures have to
be distinguishable there, and again when the short negative cache is replayed.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from scraper.security import RobotsUnavailable, reset_robots_cache, robots_allows
from scraper.tasks.public_pipeline import note_public_fetch_problem


def _boom(exc: Exception):
    def get(*args, **kwargs):
        raise exc

    return get


def test_tls_failure_records_exception_class_and_message(monkeypatch, caplog):
    monkeypatch.setattr(
        httpx,
        "get",
        _boom(
            httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate"
            )
        ),
    )
    reset_robots_cache()
    with caplog.at_level(logging.WARNING, logger="scraper.security"):
        with pytest.raises(RobotsUnavailable) as first:
            robots_allows("https://na.gov.pk/en/acts-tenure.php")
        with pytest.raises(RobotsUnavailable) as replayed:
            robots_allows("https://na.gov.pk/en/bills.php")
    message = str(first.value)
    assert "robots.txt unavailable for https://na.gov.pk" in message
    assert "HTTP 599" in message
    assert "ConnectError:" in message
    assert "unable to get local issuer certificate" in message
    assert str(replayed.value) == message
    assert any(
        record.levelno == logging.WARNING
        and "robots.txt fetch failed for https://na.gov.pk/robots.txt" in record.getMessage()
        and "ConnectError:" in record.getMessage()
        and "unable to get local issuer certificate" in record.getMessage()
        for record in caplog.records
    )
    stats: dict = {}
    note_public_fetch_problem(stats, first.value, deferred=True)
    assert stats["deferred"] == 1
    assert "ConnectError:" in stats["last_fetch_error"]
    assert "unable to get local issuer certificate" in stats["last_fetch_error"]
    reset_robots_cache()


def test_timeout_failure_is_a_different_recorded_reason(monkeypatch, caplog):
    monkeypatch.setattr(httpx, "get", _boom(httpx.ConnectTimeout("timed out")))
    reset_robots_cache()
    with caplog.at_level(logging.WARNING, logger="scraper.security"):
        with pytest.raises(RobotsUnavailable) as caught:
            robots_allows("https://punjablaws.gov.pk/acts")
    message = str(caught.value)
    assert "HTTP 599" in message
    assert "ConnectTimeout:" in message
    assert "timed out" in message
    assert "ConnectError:" not in message
    assert "unable to get local issuer certificate" not in message
    assert any("ConnectTimeout: timed out" in record.getMessage() for record in caplog.records)
    reset_robots_cache()


def test_http_5xx_from_a_fetcher_stays_a_status_without_a_transport_reason():
    reset_robots_cache()

    def fetcher(url):
        return "", 503

    with pytest.raises(RobotsUnavailable) as caught:
        robots_allows("https://example.com/page", fetcher=fetcher)
    assert str(caught.value) == "robots.txt unavailable for https://example.com (HTTP 503); deferred, retry later"
    reset_robots_cache()
