"""PakistanLawSite case-ID walk: the planner (pure) and one walk against a scripted site."""

from __future__ import annotations

from scraper.config import settings
from scraper.pls_caseid import (
    case_url,
    ceiling,
    group_key,
    group_order,
    known_by_group,
    looks_like_case_page,
    new_state,
    next_serial,
    parse_case_id,
    record,
    split_group_key,
)
from scraper.tasks import pls_caseid_walk
from scraper.tasks.pls_caseid_walk import STATE_KEY, CaseIdWalker
from tests.fixtures import BrowserScript, judgment_html
from tests.test_auth_playwright import _activate, _nosleep

BASE = "https://www.pakistanlawsite.com"


def test_parse_case_id_from_live_links():
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2026S701&court=&Row=0&bookName=undefined") == (2026, "S", 701)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=1980L238&&court= &&Row=0") == (1980, "L", 238)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=x") is None
    assert parse_case_id(None) is None


def test_parse_case_id_extended_court_tokens_and_hyphenated_codes():
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2023HCAJK1&Row=0") == (2023, "HCAJK", 1)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2023SCAJK1&Row=0") == (2023, "SCAJK", 1)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2019NIRC42&Row=0") == (2019, "NIRC", 42)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2020PLAT7&Row=0") == (2020, "PLAT", 7)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2021SCUK99&Row=0") == (2021, "SCUK", 99)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024LAT-SINDH12&Row=0") == (2024, "LAT-SINDH", 12)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2025HC-A3&Row=0") == (2025, "HC-A", 3)
    assert parse_case_id(f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2026SC-A701&Row=0") == (2026, "SC-A", 701)


def test_hyphenated_group_key_split_and_ceiling():
    url = f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024LAT-SINDH50&Row=0"
    year, court = parse_case_id(url)[:2]
    key = group_key(year, court)
    assert key == "2024LAT-SINDH"
    assert split_group_key(key) == (2024, "LAT-SINDH")
    known = known_by_group([url])
    assert known == {key: {50}}
    state = new_state()
    assert ceiling(state, known[key], floor=1000, pad=100) == 1000
    assert ceiling(state, known[key], floor=10, pad=100) == 150
    state["next"] = 51
    assert next_serial(state, known[key], floor=1000, pad=100) == 51
    built = case_url(BASE, key, 51)
    assert "CaseName=2024LAT-SINDH51&" in built
    assert parse_case_id(built) == (2024, "LAT-SINDH", 51)


def test_known_groups_and_order_dense_first_then_empty_years_newest_first():
    known = known_by_group(
        [
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=1980L238",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=1980L307",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2026S701",
            "https://example.org/other",
        ]
    )
    assert known == {"1980L": {238, 307}, "2026S": {701}}
    order = group_order(known, first_year=2025, last_year=2026)
    assert order[:2] == ["1980L", "2026S"]
    assert order[2:] == ["2026L", "2025L", "2025S"]


def test_group_order_skips_extended_courts_for_empty_years_by_default(monkeypatch):
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_EXTENDED_COURTS", False)
    known = known_by_group(
        [
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2026S701",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024LAT-SINDH12",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024HCAJK3",
        ]
    )
    order = group_order(known, first_year=2025, last_year=2026)
    assert "2024LAT-SINDH" not in order
    assert "2024HCAJK" not in order
    assert "2026LAT-SINDH" not in order
    assert "2025LAT-SINDH" not in order
    assert "2025S" in order
    assert order.index("2026S") < order.index("2025S")


def test_group_order_empty_years_for_extended_courts_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_EXTENDED_COURTS", True)
    known = known_by_group(
        [
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2026S701",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024LAT-SINDH12",
            f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=2024HCAJK3",
        ]
    )
    order = group_order(known, first_year=2025, last_year=2026)
    assert "2024LAT-SINDH" in order
    assert "2024HCAJK" in order
    assert "2026LAT-SINDH" in order
    assert "2025LAT-SINDH" in order


def test_caseid_walk_status_counts_exclude_extended_groups_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_EXTENDED_COURTS", False)
    from scraper.dashboard_metrics import _caseid_walk_progress
    from scraper.tasks.pls_caseid_walk import state_key

    keys = ["2026S", "2025L", "2024LAT-SINDH", "2024HCAJK"]
    legacy = {"calibrated_at": "x", "groups": {k: {"hits": 2, "misses": 1} for k in keys}}
    off = _caseid_walk_progress({state_key(0): legacy})
    assert off["groups_started"] == 2
    assert off["found"] == 4

    monkeypatch.setattr(settings, "PLS_CASEID_WALK_EXTENDED_COURTS", True)
    on = _caseid_walk_progress({state_key(0): legacy})
    assert on["groups_started"] == 4
    assert on["found"] == 8


def test_walk_skips_held_serials_and_jumps_to_block_starts_after_a_miss_streak():
    held = {5, 6, 701}
    g = new_state()
    seen = []
    for _ in range(12):
        n = next_serial(g, held, floor=1000, pad=100)
        seen.append(n)
        record(g, n, False, held, miss_streak=3)
    # 1,2,3 miss -> streak; jump to next block (101) but never past a held serial: 5 comes first,
    # held 5 and 6 are skipped (streak restarts), 7,8,9 miss, then block starts 101, 201, ...
    assert seen[:3] == [1, 2, 3]
    assert seen[3:6] == [7, 8, 9]
    assert seen[6:] == [101, 201, 301, 401, 501, 601]
    n = next_serial(g, held, floor=1000, pad=100)
    assert n == 702  # 701 is held: skipped, and the walk carries on after it


def test_hit_resets_the_streak_and_walk_ends_past_the_ceiling():
    held: set = set()
    g = new_state()
    for serial in (1, 2):
        assert next_serial(g, held, floor=10, pad=0) == serial
        record(g, serial, serial == 2, held, miss_streak=2)
    assert g["streak"] == 0 and g["last_hit"] == 2 and 2 in held
    while next_serial(g, held, floor=10, pad=0) is not None:
        record(g, g["next"], False, held, miss_streak=2)
    assert g["done"] is True


def test_case_page_needs_text_and_a_citation_of_its_own_year():
    assert looks_like_case_page("x" * 50, 2026) is False
    page = "AMBREEN AKRAM versus ASAD ULLAH KHAN. 2026 SCMR 1. " + "The petition is dismissed. " * 20
    assert looks_like_case_page(page, 2026) is True
    assert looks_like_case_page(page, 2025) is False


async def test_walk_calibrates_on_held_judgments_then_finds_the_next_serial(db, login_source, monkeypatch):
    await _activate(db, login_source, slots=(1,))
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_MISS_STREAK", 3)
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING", 800)
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING_PAD", 0)
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2026)  # no empty years to walk after 2026S

    async def _known(_db):
        return {"2026S": {701, 702}}

    monkeypatch.setattr(pls_caseid_walk, "load_known", _known)
    sc = BrowserScript()
    for serial, cite in ((701, "2026 SCMR 1"), (702, "2026 SCMR 9"), (703, "2026 SCMR 17")):
        sc.page(("goto", case_url(BASE, "2026S", serial)), judgment_html(cite, title=f"Party {serial} versus State"))
    walker = CaseIdWalker(db, login_source, browser_factory=sc.factory(), sleep=_nosleep)
    stats = await walker.run(probes=20)

    state = (login_source.config_json or {})[STATE_KEY]
    assert state["calibrated_at"] and [c["ok"] for c in state["calibration"]] == [True, True]
    group = state["groups"]["2026S"]
    # 1,2,3 miss; block starts 101..601 miss; held 701 and 702 skipped; 703 is a judgment; 704..706 miss,
    # and the jump to 801 passes the ceiling (800), so the group is done
    assert stats["hits"] == 1 and stats["staged"] == 1
    assert group["hits"] == 1 and group["last_hit"] == 703 and group["done"] is True
    assert stats["probes"] == 3 + 6 + 1 + 3
    asked = [k[1] for k, _ in sc.log if k[0] == "goto"]
    assert asked[:2] == [case_url(BASE, "2026S", 701), case_url(BASE, "2026S", 702)]  # calibration
    assert case_url(BASE, "2026S", 703) in asked
    assert case_url(BASE, "2026S", 702) not in asked[2:]  # a held serial is never walked


async def test_walk_stops_when_held_judgments_do_not_read_as_judgments(db, login_source, monkeypatch):
    await _activate(db, login_source, slots=(1,))

    async def _known(_db):
        return {"2026S": {701, 702}}

    monkeypatch.setattr(pls_caseid_walk, "load_known", _known)
    sc = BrowserScript()  # every page is the site's empty shell
    walker = CaseIdWalker(db, login_source, browser_factory=sc.factory(), sleep=_nosleep)
    result = await walker.run(probes=20)
    assert result["stopped"] == "calibration failed"
    state = (login_source.config_json or {})[STATE_KEY]
    assert state["calibration_failed_at"] and not state.get("calibrated_at")
    assert len([k for k, _ in sc.log if k[0] == "goto"]) == 2  # only the two checks, no walk
    again = await CaseIdWalker(db, login_source, browser_factory=sc.factory(), sleep=_nosleep).run(probes=20)
    assert "waiting for the recheck" in again["skipped"]


def test_same_judgment_matches_the_held_text_and_rejects_another_page():
    from scraper.pls_caseid import same_judgment

    held = " ".join(f"word{i} of the held judgment body sentence" for i in range(120))
    assert same_judgment("Menu Home Login\n" + held + "\nfooter", held)
    other = " ".join(f"word{i} of another judgment entirely different" for i in range(120))
    assert not same_judgment(other, held)
    assert not same_judgment("", held) and not same_judgment(held, "too short")


async def test_walk_calibrates_on_held_text_when_the_citation_is_unreadable(db, login_source, monkeypatch):
    """8 October 2026: held 1984 Sindh judgments (1984K201, 1984K202) came back as full pages but their
    old-style citation is not parsed, so the citation-only check kept the walk off. A stored failure
    from that check is retried at once, and the held judgment's own text now decides."""
    from datetime import datetime, timezone

    from scraper.models import Judgment

    await _activate(db, login_source, slots=(1,))
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING", 0)
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING_PAD", 0)
    body = {s: " ".join(f"sentence {i} of judgment {s} in the Sindh High Court reported in 1984" for i in range(150)) for s in (201, 202)}
    for s in (201, 202):
        db.add(Judgment(canonical_citation=f"1984 C L C {s}", full_text=body[s], full_text_hash=f"h{s}", source_name="PakistanLawSite",
                        access_method="login_session", source_url=case_url(BASE, "1984K", s), case_title=f"A v B {s}"))
    login_source.config_json = {**(login_source.config_json or {}), STATE_KEY: {
        "calibration_failed_at": datetime.now(timezone.utc).isoformat(),
        "calibration": [{"ok": False, "case": "1984K201", "chars": 16072}],
    }}
    await db.commit()

    async def _known(_db):
        return {"1984K": {201, 202}}

    monkeypatch.setattr(pls_caseid_walk, "load_known", _known)
    sc = BrowserScript()
    for s in (201, 202):
        sc.page(("goto", case_url(BASE, "1984K", s)), f"<html><body><div>1984 C L C {s}</div><p>{body[s]}</p></body></html>")
    walker = CaseIdWalker(db, login_source, browser_factory=sc.factory(), sleep=_nosleep)
    await walker.run(probes=2)
    state = (login_source.config_json or {})[STATE_KEY]
    assert state.get("calibrated_at"), state.get("calibration")
    assert [(c["ok"], c["matched_by"]) for c in state["calibration"]] == [(True, "held_text"), (True, "held_text")]
    assert all("has_case_content" in c and "citation_years" in c for c in state["calibration"])


async def test_held_text_lookup_matches_the_whole_case_name(db, login_source):
    from scraper.models import Judgment

    db.add_all([
        Judgment(canonical_citation="1984 CLC 2010", full_text="text of 1984K2010", full_text_hash="ha", source_name="PakistanLawSite",
                 access_method="login_session", source_url=f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=1984K2010&court=", case_title="X v Y"),
        Judgment(canonical_citation="1984 CLC 7", full_text="text of 1984K7", full_text_hash="hb", source_name="PakistanLawSite",
                 access_method="login_session", source_url=f"{BASE}/Login/ReferenceCaseLawSearch?CaseName=1984K7", case_title="P v Q"),
    ])
    await db.commit()
    walker = CaseIdWalker(db, login_source, browser_factory=BrowserScript().factory(), sleep=_nosleep)
    assert await walker._held_text("1984K", 201) == ""  # 1984K2010 is another case
    assert await walker._held_text("1984K", 2010) == "text of 1984K2010"
    assert await walker._held_text("1984K", 7) == "text of 1984K7"  # case name at the end of the link


async def test_two_walks_split_the_groups_and_keep_their_own_progress(db, login_source, monkeypatch):
    """11 Oct 2026: one walk at a time, at 6-9 s a page, read about 300 pages an hour while login 2 idled.
    Two walks run side by side, one per login: each walks only its own groups and records its own cursor."""
    from scraper.pls_caseid import parse_case_id
    from scraper.tasks.pls_caseid_walk import partition_of, state_key

    await _activate(db, login_source, slots=(1,))
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_MISS_STREAK", 2)
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING", 3)
    monkeypatch.setattr(settings, "PLS_CASEID_WALK_CEILING_PAD", 0)
    monkeypatch.setattr(settings, "PLS_EARLIEST_YEAR", 2020)
    known = {f"{y}{c}": {900} for y in (2020, 2021, 2022) for c in ("S", "L")}

    async def _known(_db):
        return {k: set(v) for k, v in known.items()}

    monkeypatch.setattr(pls_caseid_walk, "load_known", _known)
    assert {partition_of(k) for k in known} == {0, 1}
    # the single walk before the split: calibrated, and part-way through a group of partition 1
    p1_group = next(k for k in sorted(known) if partition_of(k) == 1)
    login_source.config_json = {**(login_source.config_json or {}), STATE_KEY: {
        "calibrated_at": "2026-10-10T00:00:00+00:00", "calibration": [{"ok": True, "matched_by": "held_text"}],
        "groups": {p1_group: {**new_state(), "next": 2}},
    }}
    await db.flush()

    sc = BrowserScript()  # nothing is a judgment: every probe is a miss
    stats = await CaseIdWalker(db, login_source, browser_factory=sc.factory(), sleep=_nosleep, partition=1).run(probes=50)
    asked = [parse_case_id(k[1]) for k, _ in sc.log if k[0] == "goto"]
    assert asked and stats["probes"] == len(asked)  # no calibration: it carried over from the single walk
    assert {partition_of(f"{y}{c}") for y, c, _ in asked} == {1}
    assert (int(p1_group[:4]), p1_group[4], 1) not in asked  # resumed where the single walk stopped
    cfg = login_source.config_json or {}
    walked = set(cfg[state_key(1)]["groups"])
    assert {k for k in known if partition_of(k) == 1} <= walked and {partition_of(k) for k in walked} == {1}
    assert state_key(0) == STATE_KEY and set(cfg[STATE_KEY]["groups"]) == {p1_group}  # partition 0's record untouched

    sc0 = BrowserScript()
    await CaseIdWalker(db, login_source, browser_factory=sc0.factory(), sleep=_nosleep, partition=0).run(probes=50)
    asked0 = [parse_case_id(k[1]) for k, _ in sc0.log if k[0] == "goto"]
    assert asked0 and {partition_of(f"{y}{c}") for y, c, _ in asked0} == {0}


def test_both_walk_partitions_are_scheduled_and_lock_separately():
    from pathlib import Path

    from scraper.deploy_role import LOGIN_ONLY_BEAT_ENTRIES
    from scraper.tasks.celery_app import app

    entries = {k: v for k, v in app.conf.beat_schedule.items() if v["task"] == "scraper.tasks.pls_caseid_walk.pls_caseid_walk_tick"}
    assert sorted((v.get("kwargs") or {}).get("partition", 0) for v in entries.values()) == [0, 1]
    assert set(entries) <= LOGIN_ONLY_BEAT_ENTRIES
    assert ':caseid_walk:p{partition}"' in Path("scraper/tasks/pls_caseid_walk.py").read_text(encoding="utf-8")


async def test_the_second_walk_steps_aside_while_the_grid_has_work(db, login_source, monkeypatch):
    """The second walk only uses a login the citation-grid shards do not need: while the grid is not saturated,
    it skips and leaves that login to the grid, as before the split."""
    from scraper.tasks.pls_caseid_walk import run_caseid_walk

    await _activate(db, login_source, slots=(1, 2))
    login_source.state, login_source.is_active = "ACTIVE", True
    await db.flush()
    monkeypatch.setattr(type(settings), "login_scraping_effective", property(lambda self: True))
    monkeypatch.setattr(pls_caseid_walk, "dispatch_saturated", lambda cfg, shard, now=None: shard == 0)
    out = await run_caseid_walk(db, partition=1)
    assert out == {"skipped": "the citation grid has work; the second walk leaves this login to it"}


def test_status_counts_every_group_before_and_after_the_split():
    from scraper.dashboard_metrics import _caseid_walk_progress
    from scraper.tasks.pls_caseid_walk import partition_of, state_key

    keys = [f"{y}{c}" for y in (2020, 2021, 2022) for c in ("S", "L")]
    p1 = [k for k in keys if partition_of(k) == 1]
    legacy = {"calibrated_at": "x", "groups": {k: {"hits": 2, "misses": 1} for k in keys}}
    before = _caseid_walk_progress({state_key(0): legacy})  # partition 1 has not run yet
    assert before["groups_started"] == len(keys) and before["found"] == 2 * len(keys)
    after = _caseid_walk_progress({state_key(0): legacy, state_key(1): {"groups": {k: {"hits": 5, "misses": 0} for k in p1}}})
    assert after["groups_started"] == len(keys)
    assert after["found"] == 2 * (len(keys) - len(p1)) + 5 * len(p1)  # partition 1's own record wins for its groups
