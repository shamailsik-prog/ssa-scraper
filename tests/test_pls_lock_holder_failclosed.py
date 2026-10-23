"""Fail-closed PLS lock holder regression tests for PR #264's host identity."""
from __future__ import annotations

import os

import pytest

from scraper.pls_lock_holder import local_pid_alive, lock_value_holder_stale


class _HeartbeatRedis:
    def __init__(self, present: bool):
        self.present = present
        self.queries = []

    async def exists(self, key: str):
        self.queries.append(key)
        return int(self.present)


@pytest.mark.asyncio
@pytest.mark.parametrize("heartbeat_present", [False, True])
async def test_remote_lock_never_stale_solely_from_heartbeat(
    monkeypatch, heartbeat_present
):
    """An expired heartbeat is not proof that a remote worker has stopped."""
    monkeypatch.setattr(
        "scraper.pls_lock_holder.lock_host_identity", lambda: "local-worker"
    )
    redis = _HeartbeatRedis(heartbeat_present)
    assert await lock_value_holder_stale(
        redis, "remote-worker:123:0123456789abcdef"
    ) is False


def test_local_pid_permission_denied_is_not_dead(monkeypatch):
    def _denied(_pid, _sig):
        raise PermissionError("EPERM")

    monkeypatch.setattr(os, "kill", _denied)
    assert local_pid_alive(123) is True


def test_local_pid_unknown_os_error_is_not_dead(monkeypatch):
    def _unknown(_pid, _sig):
        raise OSError("unknown")

    monkeypatch.setattr(os, "kill", _unknown)
    assert local_pid_alive(123) is True


def test_local_pid_missing_is_dead(monkeypatch):
    def _missing(_pid, _sig):
        raise ProcessLookupError("ESRCH")

    monkeypatch.setattr(os, "kill", _missing)
    assert local_pid_alive(123) is False


@pytest.mark.asyncio
async def test_definitively_missing_local_holder_is_stale(monkeypatch):
    monkeypatch.setattr(
        "scraper.pls_lock_holder.lock_host_identity", lambda: "local-worker"
    )
    monkeypatch.setattr(
        "scraper.pls_lock_holder.pid_namespace_identity",
        lambda: "aaaaaaaaaaaaaaaa",
    )

    def _missing(_pid, _sig):
        raise ProcessLookupError("ESRCH")

    monkeypatch.setattr(os, "kill", _missing)
    assert await lock_value_holder_stale(
        _HeartbeatRedis(False),
        "local-worker:123:ns-aaaaaaaaaaaaaaaa-0123456789abcdef",
    ) is True


@pytest.mark.asyncio
async def test_invalid_lock_holder_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "scraper.pls_lock_holder.lock_host_identity", lambda: "local-worker"
    )
    assert await lock_value_holder_stale(_HeartbeatRedis(False), "legacy-token") is False


@pytest.mark.asyncio
async def test_same_celery_hostname_other_pid_namespace_is_not_local(monkeypatch):
    """A live container's PID can be ESRCH in another container with the same worker hostname."""
    monkeypatch.setattr("scraper.pls_lock_holder.lock_host_identity", lambda: "shared-worker")
    monkeypatch.setattr("scraper.pls_lock_holder.pid_namespace_identity", lambda: "aaaaaaaaaaaaaaaa")

    def _missing(_pid, _sig):
        raise AssertionError("must not inspect a PID outside our namespace")

    monkeypatch.setattr(os, "kill", _missing)
    assert await lock_value_holder_stale(
        _HeartbeatRedis(False),
        "shared-worker:47:ns-bbbbbbbbbbbbbbbb-0123456789abcdef",
    ) is False


@pytest.mark.asyncio
async def test_untagged_same_host_token_cannot_be_taken_over(monkeypatch):
    """Legacy hostname+PID tokens have no evidence of a shared PID namespace."""
    monkeypatch.setattr("scraper.pls_lock_holder.lock_host_identity", lambda: "shared-worker")
    monkeypatch.setattr("scraper.pls_lock_holder.pid_namespace_identity", lambda: "aaaaaaaaaaaaaaaa")

    def _missing(_pid, _sig):
        raise AssertionError("must not inspect a PID without namespace provenance")

    monkeypatch.setattr(os, "kill", _missing)
    assert await lock_value_holder_stale(
        _HeartbeatRedis(False), "shared-worker:47:0123456789abcdef"
    ) is False


@pytest.mark.asyncio
async def test_unreadable_pid_namespace_fails_closed(monkeypatch):
    monkeypatch.setattr("scraper.pls_lock_holder.lock_host_identity", lambda: "shared-worker")
    monkeypatch.setattr("scraper.pls_lock_holder.pid_namespace_identity", lambda: None)
    assert await lock_value_holder_stale(
        _HeartbeatRedis(False),
        "shared-worker:47:ns-aaaaaaaaaaaaaaaa-0123456789abcdef",
    ) is False


def test_generated_lock_token_carries_pid_namespace_provenance(monkeypatch):
    from scraper.pls_lock_holder import lock_holder_token, parse_lock_holder
    monkeypatch.setattr("scraper.pls_lock_holder.pid_namespace_identity", lambda: "aaaaaaaaaaaaaaaa")
    token = lock_holder_token()
    parsed = parse_lock_holder(token)
    assert parsed is not None
    assert parsed[2].startswith("ns-aaaaaaaaaaaaaaaa-")
    assert len(parsed[2]) == 36
