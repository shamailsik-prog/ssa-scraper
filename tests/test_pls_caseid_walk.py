"""PakistanLawSite case-ID walk: the planner (pure) and one walk against a scripted site."""

from __future__ import annotations

from scraper.config import settings
from scraper.pls_caseid import (
    case_url,
    group_order,
    known_by_group,
    looks_like_case_page,
    new_state,
    next_serial,
    parse_case_id,
    record,
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
