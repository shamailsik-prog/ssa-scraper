"""Existing write-once archive objects must be content-verified, not size-verified."""

import pytest

from scraper.storage.adapters import ObjectExists
from scraper.storage.archive import ArchiveMirror


class _MemoryAdapter:
    def __init__(self, remote=None, *, race=False, unreadable=False):
        self.remote = remote
        self.race = race
        self.unreadable = unreadable
        self.put_calls = 0

    def exists(self, key):
        return self.remote is not None and not self.race

    def size(self, key):
        return len(self.remote) if self.remote is not None else None

    def get(self, key):
        if self.unreadable:
            raise PermissionError("simulated read denial")
        return self.remote

    def put(self, key, data, content_type="application/octet-stream"):
        self.put_calls += 1
        if self.race:
            raise ObjectExists(key)
        self.remote = data


def _obj(kind="text"):
    return {"key": "Judgments/test/judgment.txt", "data": b"AAAA", "kind": kind}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "rendered_copy", "original_pdf"])
async def test_equal_size_wrong_bytes_are_mismatch(kind):
    outcome = await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"BBBB"), _obj(kind))
    assert outcome.status == "mismatch"
    assert outcome.stored_size == 4


@pytest.mark.asyncio
async def test_matching_existing_bytes_are_written():
    outcome = await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"AAAA"), _obj())
    assert outcome.status == "written"
    assert outcome.stored_size == 4


@pytest.mark.asyncio
async def test_existing_different_size_is_mismatch():
    outcome = await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"BBBBB"), _obj())
    assert outcome.status == "mismatch"


@pytest.mark.asyncio
async def test_object_exists_race_different_bytes_is_mismatch():
    outcome = await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"BBBB", race=True), _obj())
    assert outcome.status == "mismatch"
    assert outcome.stored_size == 4


@pytest.mark.asyncio
async def test_object_exists_race_matching_bytes_is_written():
    outcome = await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"AAAA", race=True), _obj())
    assert outcome.status == "written"


@pytest.mark.asyncio
async def test_new_object_still_written():
    adapter = _MemoryAdapter()
    outcome = await ArchiveMirror(None)._adapter_store(adapter, _obj())
    assert outcome.status == "written"
    assert adapter.remote == b"AAAA"
    assert adapter.put_calls == 1


@pytest.mark.asyncio
async def test_existing_object_read_error_never_claimed_written(monkeypatch):
    async def no_retry(fn, *args, **kwargs):
        return await fn(*args)

    monkeypatch.setattr("scraper.storage.archive._retry", no_retry)
    with pytest.raises(PermissionError):
        await ArchiveMirror(None)._adapter_store(_MemoryAdapter(b"AAAA", unreadable=True), _obj())
