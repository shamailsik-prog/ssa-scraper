"""Read-only search harvest probe must not persist ledger or slot state."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from scraper.models import PlsSearchHarvestQuery
from scraper.tasks import pls_search_harvest as harvest


class _FakeLock:
    def __init__(self, *args, **kwargs):
        pass

    async def acquire(self):
        return None

    async def release(self):
        return None


class _FakeRunner:
    probe_mode = True
    harvest_mode = "updates"
    pacing_profile = {}

    def __init__(self, *args, **kwargs):
        self.pipeline = SimpleNamespace(
            harvest_mode="updates",
            pacing_profile={},
            runner=SimpleNamespace(
                close=lambda: None,
                preferred_slot_number=None,
                exclusive_slot=False,
            ),
            _session_lock=None,
            _slot_lock=None,
            _run_locks=[],
            _assert_permitted=lambda: None,
        )

    async def run_query(self, probe_query, search_map, *, max_pages=1):
        return {
            "query_key": probe_query.query_key,
            "status": probe_query.status,
            "site_total_results": 10,
            "pages_enumerated": 1,
            "rows_seen": 2,
            "rows_known": 1,
            "rows_new": 1,
            "gap_size": 1,
            "dry_run": True,
            "out_of_time": False,
            "read_only_probe": True,
            "form_values": {"reporter": "PLD", "year": "2025"},
            "rows_on_page": 2,
            "probe_rows": [
                {"citation": "PLD 2025 SC 1", "known": False},
                {"citation": "PLD 2025 SC 2", "known": True},
            ],
        }


class _NoopPgLock:
    async def __aenter__(self):
        return True

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_search_harvest_probe_makes_no_db_writes(db, login_source, monkeypatch):
    login_source.state = "ACTIVE"
    await db.flush()
    qk = "search:reporter=PLD|year=2025|probe-test"
    row = PlsSearchHarvestQuery(
        source_name="PakistanLawSite",
        query_key=qk,
        query_json={"reporter": "PLD", "year": 2025},
        status="pending",
        pages_enumerated=0,
        rows_seen=0,
    )
    db.add(row)
    await db.commit()
    status_before = row.status
    pages_before = row.pages_enumerated

    commits: list[str] = []

    async def track_commit():
        commits.append("commit")

    monkeypatch.setattr(db, "commit", track_commit)

    async def fake_active_map(db, name):
        return SimpleNamespace(
            map_version=1,
            fields={"reporter": {}, "year": {}},
            limits={"surface": "query_form"},
            result_layout={},
            page_size=None,
            pagination={},
            detail_layout={},
            stale=False,
        )

    async def _active_slot(db, n):
        return "ACTIVE"

    async def _acquire(lock):
        return None

    async def _harvest_mode(db):
        return "updates"

    monkeypatch.setattr(harvest, "active_map", fake_active_map)
    monkeypatch.setattr(harvest, "map_as_dict", lambda m: {"fields": {"reporter": {}, "year": {}}, "limits": {"surface": "query_form"}})
    monkeypatch.setattr(harvest, "_slot_state", _active_slot)
    monkeypatch.setattr(harvest, "_acquire_harvest_lock", _acquire)
    monkeypatch.setattr(harvest, "harvest_run_pg_lock", lambda slot: _NoopPgLock())
    monkeypatch.setattr(harvest, "get_harvest_mode", _harvest_mode)
    monkeypatch.setattr(harvest, "SearchHarvestRunner", _FakeRunner)
    monkeypatch.setattr(harvest, "SessionLock", _FakeLock)

    out = await harvest.run_search_harvest_probe(db, query_key=qk, max_pages=1)
    row_after = (
        await db.execute(
            select(PlsSearchHarvestQuery).where(
                PlsSearchHarvestQuery.source_name == "PakistanLawSite",
                PlsSearchHarvestQuery.query_key == qk,
            )
        )
    ).scalars().first()

    assert out.get("read_only_probe") is True
    assert out.get("probe_rows")
    assert commits == []
    assert row_after is not None
    assert row_after.status == status_before
    assert row_after.pages_enumerated == pages_before
    assert not db.new
    assert not db.dirty
