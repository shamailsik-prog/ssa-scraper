"""Idle-harvest root causes and guards (2026-10-08): reporter spelling vs dropdown values, a failing query
blocking the queue head, one query per hourly tick, the throughput watchdog's decisions, the coverage
numbers on the live dashboard and the Caddy/sslip.io config rendered from the repository."""

from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from scraper.coverage import (
    base_query_reporter_year,
    build_journal_rows,
    discovered_for,
    eta_days,
    journal_names,
    norm_reporter,
    pakistancode_transition,
)
from scraper.pls_search_harvest_core import (
    build_harvest_form_values,
    canonical_reporter_option,
    extra_offered_reporters,
    record_query_failure,
    reporter_not_offered_reason,
    unmapped_harvest_reason,
)
from scraper.tasks.pls_search_harvest import tick_should_continue
from scraper.tasks.pls_throughput_watchdog import append_history, classify_errors, plan_remediation

OPTIONS = ["CLC", "CLCN", "CLD", "GBLR", "MLD", "PLD", "PCRLJ", "PCRLJN", "PLC", "PLC N", "PLC(CS)", "PLC(CS)N", "PTD", "SCMR", "YLR", "YLRN"]
LABELS = [[o, o.replace(" ", "")] for o in OPTIONS]

# The live map v203 (dashboard Citation Search panel), trimmed to the roles the harvest uses.
DASHBOARD_MAP = {
    "fields": {
        "reporter": {"kind": "select", "selector": "#Citation_Category_Search_dropdown", "options": OPTIONS, "option_labels": LABELS},
        "year": {"kind": "text", "selector": "#Citation_Year_Search_input"},
        "court": {"kind": "text", "selector": "#Citation_Court_Search_input"},
        "page": {"kind": "text", "selector": "#Citation_Code_Or_Page_Search_input"},
        "judge": {"kind": "text", "selector": "#Citation_Judge_Search_input"},
        "keyword": {"kind": "text", "selector": "#Citation_Party_Search_input"},
        "submit": {"kind": "submit", "selector": ".Citation_Search_btn"},
    },
    "limits": {"surface": "query_form", "reporters_offered": OPTIONS, "dashboard_citation_fields": True},
}


# ----------------------------------------------------------------------------- reporter spelling
def test_harvest_form_values_use_the_dropdown_spelling():
    values = build_harvest_form_values(DASHBOARD_MAP, {"reporter": "PCrLJ", "year": 2026}, {"page": 1, "row_index": 0})
    assert values["reporter"] == "PCRLJ"
    assert values["year"] == "2026"
    assert canonical_reporter_option(DASHBOARD_MAP, "PLCN") == "PLC N"


def test_unoffered_reporter_fails_up_front():
    assert reporter_not_offered_reason(DASHBOARD_MAP, {"reporter": "PCrLJ", "year": 2026}) is None
    reason = unmapped_harvest_reason(DASHBOARD_MAP, {"reporter": "NLR", "year": 2026}, {"page": 1})
    assert reason and "not offered" in reason


def test_extra_offered_reporters_skips_respellings():
    extra = extra_offered_reporters(["PLD", "SCMR", "CLC", "PLC", "MLD", "YLR", "PCrLJ"], OPTIONS)
    assert "PCRLJ" not in extra and "PLD" not in extra
    assert extra[:4] == ["CLCN", "CLD", "GBLR", "PCRLJN"]


# ----------------------------------------------------------------------------- queue-head guard
def test_failing_query_is_demoted_then_failed():
    row = SimpleNamespace(cursor_json={}, priority=100, status="in_progress", last_error=None)
    assert record_query_failure(row, "BrowserDisconnected: select_option timeout", max_attempts=3) == "pending"
    assert row.priority == 200 and row.cursor_json["failures"] == 1
    record_query_failure(row, "again", max_attempts=3)
    assert record_query_failure(row, "third", max_attempts=3) == "failed"
    assert row.last_error == "third"


def test_permanent_failure_fails_at_once():
    row = SimpleNamespace(cursor_json=None, priority=100, status="in_progress", last_error=None)
    assert record_query_failure(row, "not offered", max_attempts=3, permanent=True) == "failed"


def test_tick_takes_several_queries_within_budget():
    assert tick_should_continue({"status": "done"}, elapsed_seconds=10, budget_seconds=1500, queries_run=1, max_queries=0)
    assert tick_should_continue({"failed": True}, elapsed_seconds=10, budget_seconds=1500, queries_run=1, max_queries=0)
    assert not tick_should_continue({"status": "done"}, elapsed_seconds=1600, budget_seconds=1500, queries_run=3, max_queries=0)
    assert not tick_should_continue({"skipped": True}, elapsed_seconds=1, budget_seconds=1500, queries_run=1, max_queries=0)
    assert not tick_should_continue({"paused": True}, elapsed_seconds=1, budget_seconds=1500, queries_run=1, max_queries=0)
    assert not tick_should_continue({"status": "done"}, elapsed_seconds=1, budget_seconds=1500, queries_run=2, max_queries=2)


# ----------------------------------------------------------------------------- throughput watchdog
NOW = datetime(2026, 10, 8, 0, 0, tzinfo=timezone.utc)


def _plan(**kw):
    base = dict(
        source_state="ACTIVE", pages=0, judgments=0, slot_states={1: "ACTIVE", 2: "ACTIVE"}, lock_keys=[], running_jobs=0,
        stale_running_jobs=0, pending_queries=250, transient_failed=0, search_ran_recently=False, map_ok=True,
        recent_errors=[], idle_checks=1, last_recreate_at=None, now=NOW, search_harvest_active=False,
    )
    base.update(kw)
    return plan_remediation(**base)


def test_watchdog_quiet_when_moving_or_not_active():
    assert _plan(judgments=3)["state"] == "ok"
    assert _plan(pages=5, pending_queries=0)["state"] == "ok"


def test_watchdog_acts_when_pages_move_but_no_judgment_lands_with_slots_active():
    """Oct 7 stall: one page per job, nothing staged, both slots ACTIVE -> still idle, still remediated."""
    plan = _plan(pages=40, judgments=0, idle_checks=3, recent_errors=["#Citation_Category_Search_dropdown not visible"] * 3)
    assert plan["state"] == "idle" and "recreate_worker" in plan["actions"]
    assert "40 PLS page(s) fetched" in plan["diagnosis"][0]
    assert _plan(source_state="PAUSED")["state"] == "skipped"
    assert _plan(source_state="PAUSED")["actions"] == []


def test_watchdog_kicks_search_harvest_when_idle():
    plan = _plan()
    assert plan["state"] == "idle" and "kick_search_harvest" in plan["actions"]
    assert "kick_search_harvest" not in _plan(search_ran_recently=True)["actions"]


def test_watchdog_recovers_slots_and_releases_stale_lock():
    plan = _plan(slot_states={1: "NEEDS_HUMAN_LOGIN", 2: "ACTIVE"}, lock_keys=["corpus:login_session_lock:PakistanLawSite"], running_jobs=0)
    assert "recover_slots" in plan["actions"] and "release_stale_lock" in plan["actions"]
    # a live job with a fresh heartbeat keeps its lock
    assert "release_stale_lock" not in _plan(lock_keys=["k"], running_jobs=1, stale_running_jobs=0)["actions"]
    assert "release_stale_lock" not in _plan(
        lock_keys=["corpus:login_session_lock:PakistanLawSite"], running_jobs=0, search_harvest_active=True
    )["actions"]


def test_watchdog_recreates_worker_on_crashes_with_cooldown():
    errs = ["disconnected: Page.select_option: Timeout 90000ms exceeded.", "Page.goto: Page crashed"]
    assert "recreate_worker" in _plan(recent_errors=errs)["actions"]
    assert "recreate_worker" not in _plan(recent_errors=errs, last_recreate_at=NOW - timedelta(minutes=30))["actions"]
    assert "recreate_worker" in _plan(idle_checks=3)["actions"]


def test_watchdog_seeds_empty_queue_and_reports_map():
    plan = _plan(pending_queries=0, map_ok=False)
    assert "seed_queue" in plan["actions"] and "kick_search_harvest" in plan["actions"]
    assert any("remap" in u for u in plan["unresolved"])


def test_classify_errors_and_history_bound():
    kinds = classify_errors(["Page crashed", "login surface URL (landed on /Login/MainPage)", "boom", ""])
    assert kinds == {"browser_crash": 1, "login": 1, "other": 1}
    watch = {}
    for i in range(40):
        append_history(watch, {"i": i})
    assert len(watch["history"]) == 30 and watch["history"][0] == {"i": 39}


# ----------------------------------------------------------------------------- coverage numbers
def test_journal_names_merge_spellings():
    assert journal_names(["PLD", "PCrLJ"], ["PCRLJ", "CLCN"], ["CLC"]) == ["PLD", "PCrLJ", "CLCN", "CLC"]
    assert norm_reporter("P.Cr.L.J") == "pcrlj"


def test_base_query_and_discovered():
    assert base_query_reporter_year({"reporter": "PLD", "year": 2025}) == ("PLD", 2025)
    assert base_query_reporter_year({"reporter": "PLD", "year": 2025, "keyword": "a"}) is None
    assert discovered_for(None, 9) == 9 and discovered_for(120, 9) == 120


def test_build_journal_rows_and_transition():
    cells = [
        {"reporter": "PLD", "year": 2026, "status": "done", "discovered": 9, "rows_new": 0},
        {"reporter": "PLD", "year": 2025, "status": "pending", "discovered": 0, "rows_new": 0},
        {"reporter": "PCrLJ", "year": 2026, "status": "failed", "discovered": 0, "rows_new": 0},
    ]
    rows = build_journal_rows(journals=["PLD", "CLC", "PCrLJ"], collected={"clc": 19931, "pld": 3}, grid_totals={"clc": 20568}, search_cells=cells)
    by = {r["journal"]: r for r in rows}
    assert by["CLC"]["discovered"] == 20568 and by["CLC"]["coverage_pct"] == 96.9
    assert by["PLD"]["discovered"] == 9 and by["PLD"]["years_done"] == 1 and by["PLD"]["years_pending"] == 1
    assert by["PCrLJ"]["years_failed"] == 1 and by["PCrLJ"]["coverage_pct"] is None
    # CLC alone at 96.9% must not trigger the PakistanCode switch while other journals are undiscovered
    t = pakistancode_transition(rows, required=["PLD", "CLC", "PCrLJ"], threshold=0.95)
    assert not t["ready"] and t["journals_without_discovery"] == ["PCrLJ"]
    full = build_journal_rows(journals=["PLD", "CLC"], collected={"clc": 19931, "pld": 9}, grid_totals={"clc": 20568}, search_cells=cells[:1])
    assert pakistancode_transition(full, required=["PLD", "CLC"], threshold=0.95)["ready"]
    assert eta_days(1000, 250) == 4.0 and eta_days(0, 0) == 0.0 and eta_days(10, 0) is None


# ----------------------------------------------------------------------------- live dashboard + scripts
def test_live_page_is_served(client):
    html = client.get("/live").text
    assert "/live.json" in html and "Self-healing watchdog" in html and "Asia/Karachi" in html


def test_live_json_shape(client):
    body = client.get("/live.json").json()
    assert set(body) >= {"totals", "rate", "journals", "grid", "watchdog", "drive", "embeddings", "pakistancode_transition", "slots"}
    text = str(body).lower()
    for secret in ("password", "api_key", "token"):
        assert secret not in text


def test_host_scripts_are_valid_bash():
    for script in ("scripts/render_caddyfile.sh", "scripts/pls_watchdog_host.sh", "scripts/auto_deploy.sh"):
        subprocess.run(["bash", "-n", script], check=True)


def test_render_caddyfile_sslip_and_ip_route(tmp_path):
    env = dict(os.environ, SSA_SCRAPER_DIR=str(tmp_path), PUBLIC_IP="165.227.163.113", PATH="/usr/bin:/bin")
    subprocess.run(["bash", str(Path("scripts/render_caddyfile.sh").resolve())], check=True, env=env, capture_output=True)
    text = (tmp_path / "state" / "Caddyfile").read_text()
    assert "165-227-163-113.sslip.io {" in text
    assert "redir @root /live 302" in text
    assert "https://165.227.163.113, https://localhost {" in text and "tls internal" in text
    # idempotent second run
    out = subprocess.run(["bash", str(Path("scripts/render_caddyfile.sh").resolve())], check=True, env=env, capture_output=True, text=True)
    assert "unchanged" in out.stdout


def test_auto_deploy_installs_watchdog_cron_and_prefers_github():
    deploy = Path("scripts/auto_deploy.sh").read_text()
    assert "scripts/pls_watchdog_host.sh" in deploy and "install_watchdog_cron" in deploy
    assert 'REMOTE="github"' in deploy
    host = Path("scripts/pls_watchdog_host.sh").read_text()
    assert "render_caddyfile.sh" in host and "pls_watchdog_request.json" in host


def test_beat_schedules_throughput_watchdog_on_maintenance():
    from scraper.tasks.celery_app import app

    entry = app.conf.beat_schedule["pls-throughput-watchdog"]
    assert entry["task"] == "scraper.tasks.pls_throughput_watchdog.pls_throughput_watchdog"
    assert app.conf.task_routes[entry["task"]]["queue"] == "maintenance"


# ----------------------------------------------------------------------------- search-first strategy (2026-10-08)
def test_dashboard_page_box_is_never_the_pagination_cursor():
    """The page box is the citation page: filling it with result page 1 limited every year to page-1 citations."""
    values = build_harvest_form_values(DASHBOARD_MAP, {"reporter": "PLD", "year": 2025}, {"page": 3, "row_index": 0})
    assert "page" not in values and values == {"reporter": "PLD", "year": "2025"}
    lookup = build_harvest_form_values(DASHBOARD_MAP, {"reporter": "SCMR", "year": 2019, "page": 123}, {"page": 1})
    assert lookup["page"] == "123" and lookup["reporter"] == "SCMR"


def test_citation_lookup_query_parses_reporter_year_page():
    from scraper.pls_search_harvest_core import citation_lookup_query

    assert citation_lookup_query("2019 SCMR 123") == {"reporter": "SCMR", "year": 2019, "page": 123}
    q = citation_lookup_query("PLD 2019 SC 1")
    assert q and q["reporter"] == "PLD" and q["year"] == 2019 and q["page"] == 1
    assert citation_lookup_query("not a citation") is None


def test_shard_reporters_are_disjoint_and_cover_all():
    from scraper.pls_search_harvest_core import shard_reporters

    allr = ["PLD", "SCMR", "CLC", "PLC", "MLD", "YLR", "PCrLJ", "CLCN", "CLD"]
    a, b = shard_reporters(allr, 0), shard_reporters(allr, 1)
    assert not set(a) & set(b) and sorted(a + b) == sorted(allr)
    assert shard_reporters(allr, None) is None


def test_capped_split_uses_courts_then_party_initial():
    from scraper.pls_search_harvest_core import FALLBACK_COURT_SPLITS, split_oversized_query

    roles = set(DASHBOARD_MAP["fields"]) | {"court", "keyword"}
    kids = split_oversized_query({"reporter": "PLD", "year": 2020}, 500, result_cap=500, court_options=[], available_roles=roles)
    assert [k["court"] for k in kids] == list(FALLBACK_COURT_SPLITS)
    grand = split_oversized_query(kids[0], 500, result_cap=500, court_options=[], available_roles=roles)
    assert grand and all("keyword" in g or "party_initial" in g for g in grand)


def test_tick_lock_keys_unsharded_takes_both():
    from scraper.tasks.pls_search_harvest import tick_lock_keys

    assert tick_lock_keys(0) == ["corpus:pls_search_run:0"]
    assert tick_lock_keys(None) == ["corpus:pls_search_run:0", "corpus:pls_search_run:1"]


def test_plan_priority_newest_first_subscribed_first(monkeypatch):
    from scraper.config import settings
    from scraper.tasks.pls_search_harvest import plan_earliest_year, plan_priority

    monkeypatch.setattr(settings, "PLS_SUBSCRIBED_REPORTERS", "PLD,SCMR")
    assert plan_priority({"reporter": "PLD", "year": 2026}, current_year=2026) == 100
    assert plan_priority({"reporter": "PLD", "year": 1947}, current_year=2026) == 179
    assert plan_priority({"reporter": "CLCN", "year": 2026}, current_year=2026) == 400
    assert plan_earliest_year(2026) <= 1947


def test_recheck_years_are_recent():
    from scraper.tasks.pls_throughput_watchdog import recheck_years

    assert recheck_years(NOW) == [2026, 2025]


def test_collected_judgments_are_not_discovery_evidence():
    """Legacy corpus rows alone must not satisfy the PakistanCode coverage gate (review on #166)."""
    rows = build_journal_rows(journals=["PLD", "SCMR"], collected={"pld": 50, "scmr": 40}, grid_totals={}, search_cells=[])
    assert all(r["discovery_evidence"] == 0 for r in rows)
    t = pakistancode_transition(rows, required=["PLD", "SCMR"], threshold=0.95)
    assert not t["ready"] and sorted(t["journals_without_discovery"]) == ["PLD", "SCMR"]


async def test_tick_run_key_marks_search_harvest_running():
    from scraper.tasks.pls_search_harvest import acquire_tick_run_locks, pls_search_tick_running, release_tick_keys

    assert await pls_search_tick_running() is False
    held = await acquire_tick_run_locks(1, ttl=120)
    try:
        assert await pls_search_tick_running() is True
    finally:
        await release_tick_keys(held or [])
    assert await pls_search_tick_running() is False


def test_worker_start_drops_dead_tick_markers():
    import redis

    from scraper.config import settings
    from scraper.tasks.celery_app import drop_dead_search_tick_markers

    r = redis.Redis.from_url(settings.REDIS_URL)
    try:
        r.set("corpus:pls_search_run:0", "dead", ex=3600)
        r.set("corpus:pls_search_tick_queued:all", "1", ex=3600)
        r.set("corpus:pls_search_keep", "1", ex=60)
        assert drop_dead_search_tick_markers() == 2
        assert r.exists("corpus:pls_search_run:0", "corpus:pls_search_tick_queued:all") == 0
        assert r.exists("corpus:pls_search_keep") == 1
    finally:
        r.delete("corpus:pls_search_keep")
        r.close()


def test_live_tick_walking_known_ground_is_not_killed():
    """No promotion for a while is normal on years already collected: a live tick without crashes keeps running."""
    assert _plan(queries_completed=2)["state"] == "ok"
    walking = _plan(idle_checks=5, search_harvest_active=True)
    assert walking["state"] == "idle" and "recreate_worker" not in walking["actions"]
    crashed = _plan(idle_checks=5, search_harvest_active=True, recent_errors=["Page.goto: Page crashed"] * 2)
    assert "recreate_worker" in crashed["actions"]
