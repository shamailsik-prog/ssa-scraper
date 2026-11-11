"""A robots.txt transport failure stays HTTP 599 and keeps the exception class and message.

Job stats only see the RobotsUnavailable text. TLS, timeout and connection failures have to
be distinguishable there, and again when the short negative cache is replayed.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from scraper.security import ROBOTS_TRANSPORT_REASON_LIMIT, RobotsUnavailable, reset_robots_cache, robots_allows
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
        and "robots.txt fetch failed:" in record.getMessage()
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


def test_transport_reason_is_bounded_and_url_userinfo_is_redacted(monkeypatch, caplog):
    secret = "s3cret-token"
    user = "audit-user"
    tail = "TAILMARKER" + ("Z" * 40)
    detail = f"https://{user}:{secret}@na.gov.pk/robots.txt " + ("A" * 800) + " " + tail
    monkeypatch.setattr(httpx, "get", _boom(httpx.ConnectError(detail)))
    page = f"https://{user}:{secret}@na.gov.pk/en/acts-tenure.php"
    reset_robots_cache()
    with caplog.at_level(logging.WARNING, logger="scraper.security"):
        with pytest.raises(RobotsUnavailable) as first:
            robots_allows(page)
        with pytest.raises(RobotsUnavailable) as replayed:
            robots_allows(f"https://{user}:{secret}@na.gov.pk/en/bills.php")
    message = str(first.value)
    assert str(replayed.value) == message
    assert "ConnectError:" in message
    assert "HTTP 599" in message
    assert secret not in message
    assert user not in message
    assert tail not in message
    assert secret not in caplog.text
    assert user not in caplog.text
    assert "https://***@" in message
    assert "https://***@" in caplog.text
    reason = message.split("HTTP 599: ", 1)[1].split("); deferred", 1)[0]
    assert len(reason) <= ROBOTS_TRANSPORT_REASON_LIMIT
    assert 300 <= ROBOTS_TRANSPORT_REASON_LIMIT <= 500
    assert reason.endswith("...")
    stats: dict = {}
    note_public_fetch_problem(stats, first.value, deferred=True)
    assert secret not in stats["last_fetch_error"]
    assert user not in stats["last_fetch_error"]
    reset_robots_cache()


def test_fetcher_supplied_reason_is_redacted_before_it_is_cached():
    secret = "s3cret-token"
    user = "audit-user"
    reset_robots_cache()

    def fetcher(url):
        return "", 599, f"ConnectError: https://{user}:{secret}@example.com/robots.txt " + ("B" * 600)

    with pytest.raises(RobotsUnavailable) as first:
        robots_allows("https://example.com/page", fetcher=fetcher)
    with pytest.raises(RobotsUnavailable) as replayed:
        robots_allows("https://example.com/other", fetcher=fetcher)
    message = str(first.value)
    assert message == str(replayed.value)
    assert secret not in message and user not in message
    assert "https://***@" in message
    reason = message.split("HTTP 599: ", 1)[1].split("); deferred", 1)[0]
    assert len(reason) <= ROBOTS_TRANSPORT_REASON_LIMIT
    reset_robots_cache()


def _with_uncapped_tail(detail: str) -> tuple[str, str]:
    tail = "TAILMARKER" + ("Z" * 40)
    return f"{detail} " + ("A" * 800) + f" {tail}", tail


def _transport_failure(monkeypatch, caplog, detail: str):
    monkeypatch.setattr(httpx, "get", _boom(httpx.ConnectError(detail)))
    reset_robots_cache()
    with caplog.at_level(logging.WARNING, logger="scraper.security"):
        with pytest.raises(RobotsUnavailable) as first:
            robots_allows("https://na.gov.pk/en/acts-tenure.php")
        with pytest.raises(RobotsUnavailable) as replayed:
            robots_allows("https://na.gov.pk/en/bills.php")
    message = str(first.value)
    assert str(replayed.value) == message
    stats: dict = {}
    note_public_fetch_problem(stats, first.value, deferred=True)
    reason = message.split("HTTP 599: ", 1)[1].split("); deferred", 1)[0]
    logged = caplog.text
    reset_robots_cache()
    return message, reason, stats["last_fetch_error"], logged


def _assert_reason_capped(reason: str, tail: str, *surfaces: str) -> None:
    assert len(reason) <= ROBOTS_TRANSPORT_REASON_LIMIT
    assert 300 <= ROBOTS_TRANSPORT_REASON_LIMIT <= 500
    assert reason.endswith("...")
    assert tail not in reason
    for surface in surfaces:
        assert tail not in surface


def test_transport_reason_redacts_url_query_strings(monkeypatch, caplog):
    user = "audit-user"
    secret = "s3cret-token"
    query = "?token=abc123&access_token=xyz789"
    detail, tail = _with_uncapped_tail(
        f"https://{user}:{secret}@na.gov.pk/robots.txt{query} and again {query}"
    )
    message, reason, stored, logged = _transport_failure(monkeypatch, caplog, detail)
    assert "ConnectError:" in message
    assert "https://***@" in message
    assert "?***" in message
    assert "na.gov.pk/robots.txt?***" in message
    for leaked in (user, secret, "token=abc123", "access_token=xyz789", "abc123", "xyz789"):
        assert leaked not in message
        assert leaked not in stored
        assert leaked not in logged
    _assert_reason_capped(reason, tail, message, stored, logged)


def test_transport_reason_redacts_bearer_tokens(monkeypatch, caplog):
    jwt = (
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
        ".eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    detail, tail = _with_uncapped_tail(f"authorization: Bearer {jwt}")
    message, reason, stored, logged = _transport_failure(monkeypatch, caplog, detail)
    assert "ConnectError:" in message
    assert "Bearer ***" in message
    assert jwt not in message and jwt[:24] not in message
    assert jwt not in stored and jwt not in logged
    _assert_reason_capped(reason, tail, message, stored, logged)


def test_transport_reason_redacts_basic_tokens(monkeypatch, caplog):
    short_blob = "YQ=="
    # Twelve alphanumeric characters, no padding: bare "Basic <word>" must not need
    # punctuation, and a shorter Authorization value must not survive either.
    alphanumeric = "dXNlcjpwYXNz"
    short_alphanumeric = "YTpi"
    blob = "dXNlcjpzM2NyZXQtdG9rZW4="
    detail, tail = _with_uncapped_tail(
        "certificate verify failed: Basic Constraints of CA cert not marked critical; "
        f"Authorization: Basic {short_blob}; Authorization: Basic {short_alphanumeric}; "
        f"Basic {alphanumeric}; Authorization: Basic {blob}"
    )
    message, reason, stored, logged = _transport_failure(monkeypatch, caplog, detail)
    assert "ConnectError:" in message
    assert "Basic Constraints of CA cert not marked critical" in message
    assert "Basic ***" in message
    for leaked in (short_blob, alphanumeric, short_alphanumeric, blob):
        assert leaked not in message
        assert leaked not in stored
        assert leaked not in logged
    _assert_reason_capped(reason, tail, message, stored, logged)


def test_http_5xx_from_a_fetcher_stays_a_status_without_a_transport_reason():
    reset_robots_cache()

    def fetcher(url):
        return "", 503

    with pytest.raises(RobotsUnavailable) as caught:
        robots_allows("https://example.com/page", fetcher=fetcher)
    assert str(caught.value) == "robots.txt unavailable for https://example.com (HTTP 503); deferred, retry later"
    reset_robots_cache()
