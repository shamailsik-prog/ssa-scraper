"""The search harvest runs once an hour and shares the two PakistanLawSite logins with the two case-ID walks.
A tick that found both logins busy used to skip its whole hour; it now comes back a minute later, for a bounded
number of tries, so turning the walk on cannot starve the harvest (operator, 12 October 2026)."""

from __future__ import annotations

import scraper.tasks.pls_search_harvest as h


def _patch(monkeypatch, result):
    calls = []
    monkeypatch.setattr("scraper.tasks.deploy_hold.login_work_held", lambda: False)

    async def fake_inner(**kwargs):
        calls.append(("run", kwargs))
        return dict(result)

    monkeypatch.setattr(h, "_harvest_tick_body", fake_inner)
    monkeypatch.setattr(h.pls_search_harvest_tick, "apply_async", lambda **kw: calls.append(("requeue", kw)))
    return calls


def test_a_tick_that_finds_both_logins_busy_comes_back_a_minute_later(monkeypatch):
    calls = _patch(monkeypatch, {"skipped": True, "reason": "no free login slot"})
    out = h.pls_search_harvest_tick(priority_gaps=True)
    assert calls[0] == ("run", {"priority_gaps": True})
    kind, kw = calls[1]
    assert kind == "requeue" and kw["countdown"] == h.LOGIN_BUSY_RETRY_SECONDS
    assert kw["kwargs"] == {"priority_gaps": True, "login_busy_retry": 1}
    assert out["requeued"] is True


def test_a_held_session_lock_also_comes_back(monkeypatch):
    calls = _patch(monkeypatch, {"skipped": True, "reason": "login session lock held"})
    h.pls_search_harvest_tick(priority_gaps=True, login_busy_retry=3)
    assert calls[0] == ("run", {"priority_gaps": True})  # the retry counter never reaches run_search_harvest
    assert calls[1][1]["kwargs"]["login_busy_retry"] == 4


def test_the_retries_are_bounded(monkeypatch):
    calls = _patch(monkeypatch, {"skipped": True, "reason": "no free login slot"})
    out = h.pls_search_harvest_tick(login_busy_retry=h.LOGIN_BUSY_MAX_RETRIES)
    assert [c[0] for c in calls] == ["run"] and "requeued" not in out


def test_other_outcomes_are_not_requeued(monkeypatch):
    for result in ({"skipped": True, "reason": "no pending search harvest query"}, {"saved": 12}):
        calls = _patch(monkeypatch, result)
        h.pls_search_harvest_tick()
        assert [c[0] for c in calls] == ["run"]
