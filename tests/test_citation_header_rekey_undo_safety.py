"""Undo must never discard search-harvest queries without creation provenance."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from scraper.tasks.citation_header_rekey import (
    BACKUP_KEY,
    REFETCH_KEY,
    UNDO_LOG_KEY,
    _undo_run,
)


class _Result:
    def __init__(self, value):
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _FakeDB:
    def __init__(self, run_id):
        self.rows = {
            BACKUP_KEY.format(run_id=run_id): SimpleNamespace(key=BACKUP_KEY.format(run_id=run_id), value="[]"),
            UNDO_LOG_KEY.format(run_id=run_id): SimpleNamespace(key=UNDO_LOG_KEY.format(run_id=run_id), value="{}"),
            REFETCH_KEY.format(run_id=run_id): SimpleNamespace(
                key=REFETCH_KEY.format(run_id=run_id),
                value=json.dumps(["preexisting-query-key"]),
            ),
        }
        self.deleted_metadata = []
        self.commits = 0

    async def execute(self, stmt):
        sql = str(stmt.compile(compile_kwargs={"literal_binds": True}))
        # No search-harvest query may be selected or deleted on undo.
        assert "pls_search_harvest_query" not in sql.lower()
        assert "corpus_metadata" in sql.lower()
        for key, row in self.rows.items():
            if "'" + key + "'" in sql:
                return _Result(row)
        return _Result(None)

    async def delete(self, obj):
        assert obj.key in self.rows
        self.deleted_metadata.append(obj.key)
        del self.rows[obj.key]

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_undo_keeps_preexisting_pending_refetch_work():
    db = _FakeDB("test-run")
    result = await _undo_run(db, "test-run")
    assert result["undone"] is True
    assert result["refetch_removed"] == 0
    assert result["refetch_retained"] == 1
    assert db.commits == 1
    assert len(db.deleted_metadata) == 3
    assert not db.rows


@pytest.mark.asyncio
async def test_undo_missing_backup_has_no_side_effects():
    db = _FakeDB("test-run")
    del db.rows[BACKUP_KEY.format(run_id="test-run")]
    result = await _undo_run(db, "test-run")
    assert result["undone"] is False
    assert db.commits == 0
    assert db.deleted_metadata == []
