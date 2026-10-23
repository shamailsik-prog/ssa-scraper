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

    def _missing(_pid, _sig):
        raise ProcessLookupError("ESRCH")

    monkeypatch.setattr(os, "kill", _missing)
    assert await lock_value_holder_stale(
        _HeartbeatRedis(False), "local-worker:123:0123456789abcdef"
    ) is True


@pytest.mark.asyncio
async def test_invalid_lock_holder_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "scraper.pls_lock_holder.lock_host_identity", lambda: "local-worker"
    )
    assert await lock_value_holder_stale(_HeartbeatRedis(False), "legacy-token") is False
