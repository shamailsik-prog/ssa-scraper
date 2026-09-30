"""Collection-strategy guards layered on the #147 search harvester: split order, cap-is-never-complete,
seed families, snowball candidates, page continuity."""

from __future__ import annotations

from scraper.pls_search_harvest_core import (
    SPLIT_ORDER,
    cited_citations_in_text,
    is_capped,
    iter_extended_plan_queries,
    make_query_key,
    page_continuity_gaps,
    split_oversized_query,
    unmapped_harvest_reason,
)

ROLES = {"reporter", "year", "month", "court", "bench", "party_initial", "keyword"}


def test_split_order_month_then_court_then_bench_then_party_then_keyword():
    q = {"reporter": "PLD", "year": 2020}
    kw = dict(result_cap=500, court_options=["SC", "LHC"], available_roles=ROLES, bench_options=["single", "division"])
    step1 = split_oversized_query(q, 500, **kw)
    assert {c["month"] for c in step1} == set(range(1, 13))
    step2 = split_oversized_query(step1[0], 500, **kw)
    assert [c["court"] for c in step2] == ["SC", "LHC"]
    step3 = split_oversized_query(step2[0], 500, **kw)
    assert [c["bench"] for c in step3] == ["single", "division"]
    step4 = split_oversized_query(step3[0], 500, **kw)
    assert step4[0]["party_initial"] == "a" and len(step4) == 26
    step5 = split_oversized_query(step4[0], 500, **kw)
    assert step5[0]["keyword"] == "a"
    assert split_oversized_query(step5[0], 500, **kw) == []  # exhausted: caller must fail the job, not mark it done


def test_split_skips_dimensions_the_form_cannot_express():
    kids = split_oversized_query({"reporter": "PLD", "year": 2020}, 900, result_cap=500, court_options=["SC"], available_roles={"court", "keyword"})
    assert kids and all("court" in k and "month" not in k for k in kids)


def test_total_equal_to_cap_is_capped():
    assert is_capped(500, 500, 500) is True
    assert is_capped(499, 499, 500) is False
    assert is_capped(None, 500, 500) is True
    assert split_oversized_query({"reporter": "PLD", "year": 2020}, 500, result_cap=500, court_options=["SC"]) != []


def test_extended_seed_families_use_only_supplied_values():
    qs = list(iter_extended_plan_queries(years=[2024], courts=["SC"], judges=["Ijaz Ahmad"], statutes=[{"statute": "Contract Act", "sections": ["10", "11"]}], keywords=["bail"], parties=["Bank"]))
    keys = {make_query_key(q) for q in qs}
    assert {"search:year=2024|court=SC", "search:year=2024|judge=Ijaz Ahmad", "search:court=SC|keyword=bail", "search:year=2024|party=Bank"} <= keys
    assert sum(1 for q in qs if q.get("statute")) == 2


def test_snowball_only_returns_citations_present_in_text():
    text = "relied on 2019 CLC 55 and PLD 2015 SC 100 but not on nothing"
    assert cited_citations_in_text(text) == ["2019 CLC 55", "PLD 2015 SC 100"]
    assert cited_citations_in_text("no citations here") == []


def test_page_continuity_flags_holes():
    assert page_continuity_gaps([1, 10, 30, 500, 520]) == [(30, 500)]
    assert page_continuity_gaps([]) == []


def test_unmapped_reason_for_new_roles():
    smap = {"fields": {"reporter": {}, "year": {}, "keyword": {}}}
    assert "month" in (unmapped_harvest_reason(smap, {"reporter": "PLD", "year": 2020, "month": 3}, {}) or "")
    assert unmapped_harvest_reason(smap, {"judge": "X", "year": 2020}, {}) is None
    assert SPLIT_ORDER == ("month", "court", "bench", "party_initial", "keyword")


def test_year_only_seed_submits_the_year():
    from scraper.pls_search_harvest_core import build_harvest_form_values

    smap = {"fields": {"court": {}, "year": {}, "keyword": {}}}
    vals = build_harvest_form_values(smap, {"court": "SC", "year": 2020}, {})
    assert vals.get("year") == "2020" and vals.get("court") == "SC"
    no_year = {"fields": {"court": {}, "keyword": {}}}
    assert "year" in (unmapped_harvest_reason(no_year, {"court": "SC", "year": 2020}, {}) or "")
