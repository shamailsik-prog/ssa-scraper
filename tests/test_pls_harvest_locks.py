from __future__ import annotations

import pytest

from scraper.config import settings
from scraper.pls_harvest_locks import clear_stale_harvest_locks, refresh_harvest_locks


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.deleted: list[str] = []

    async def scan(self, cursor=0, match=None, count=200):
        keys = [k for k in self.store if match and _match(k, match)]
        return 0, keys

    async def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                self.deleted.append(k)
                n += 1
        return n

    async def aclose(self):
        return None


def _match(key: str, pattern: str) -> bool:
    if pattern.endswith("*"):
        return key.startswith(pattern[:-1])
    return key == pattern


@pytest.mark.asyncio
async def test_clear_harvest_locks_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "SSA_CLEAR_HARVEST_LOCKS_ON_START", False)
    r = _FakeRedis()
    r.store["corpus:login_session_lock:PakistanLawSite:search_harvest:slot1"] = "1"
    assert await clear_stale_harvest_locks(r) == 0
    assert r.store


@pytest.mark.asyncio
async def test_clear_harvest_locks_deletes_slot_and_query_keys(monkeypatch):
    monkeypatch.setattr(settings, "SSA_CLEAR_HARVEST_LOCKS_ON_START", True)
    r = _FakeRedis()
    r.store["corpus:login_session_lock:PakistanLawSite:search_harvest:slot1"] = "1"
    r.store["corpus:login_session_lock:PakistanLawSite:search_harvest:query:abc"] = "1"
    dropped = await clear_stale_harvest_locks(r)
    assert dropped == 2
    assert not r.store


@pytest.mark.asyncio
async def test_refresh_harvest_locks_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "SSA_HARVEST_LOCK_HEARTBEAT", False)
    called = []

    class Lock:
        async def refresh(self):
            called.append(True)

    await refresh_harvest_locks([Lock()])
    assert called == []
