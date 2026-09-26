from datetime import datetime, timedelta, timezone

import pytest

from scraper.pls_grid_health import signatures_equal, stall_signature


def test_stall_signature_changes_when_cursor_moves():
    cfg = {"citation_grid_cursor": {"row_offset": 10, "last_total_rows": 100, "updated_at": "t"}}
    a = stall_signature(cfg, 50)
    cfg2 = {"citation_grid_cursor": {"row_offset": 11, "last_total_rows": 100, "updated_at": "t2"}}
    b = stall_signature(cfg2, 50)
    assert not signatures_equal(a, b)


def test_stall_watchdog_signature_unchanged_implies_stall_window():
    from scraper.tasks.pls_self_healing import STALL_AFTER

    now = datetime.now(timezone.utc)
    old = now - STALL_AFTER - timedelta(minutes=5)
    cfg = {"citation_grid_cursor": {"row_offset": 8344, "last_total_rows": 20568}}
    sig = stall_signature(cfg, 7872)
    prev_sig = sig
    prev_at = old
    stalled = signatures_equal(prev_sig, sig) and (now - prev_at) >= STALL_AFTER
    assert stalled is True
