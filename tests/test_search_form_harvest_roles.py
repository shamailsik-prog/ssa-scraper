from scraper.search_form_roles import apply_harvest_query_keyword, harvest_expressible_roles, resolve_harvest_fields


def test_party_box_receives_keyword_splits():
    smap = {
        "fields": {
            "reporter": {"selector": "#r"},
            "party": {"selector": "#p"},
        },
        "limits": {"harvest_role_aliases": {"keyword": "party"}},
    }
    values: dict = {}
    apply_harvest_query_keyword(values, smap, {"keyword": "m"})
    assert values == {"party": "m"}
    assert "keyword" not in values


def test_resolve_harvest_fields_adds_keyword_alias_selector():
    smap = {
        "fields": {"party": {"selector": "#Citation_Party_Search_input"}},
        "limits": {"harvest_role_aliases": {"keyword": "party"}},
    }
    resolved = resolve_harvest_fields(smap)
    assert resolved["party"]["selector"] == "#Citation_Party_Search_input"
    assert resolved["keyword"]["selector"] == "#Citation_Party_Search_input"
    assert harvest_expressible_roles(smap) >= {"party", "keyword"}


def test_harvest_refuses_colliding_keyword_and_party_filters():
    from scraper.pls_search_harvest_core import build_harvest_form_values, unmapped_harvest_reason

    smap = {
        "fields": {
            "reporter": {"selector": "#reporter"},
            "party": {"selector": "#party"},
        },
        "limits": {"harvest_role_aliases": {"keyword": "party"}},
    }
    query = {"reporter": "SCMR", "party": "Alpha", "keyword": "b"}
    # Reproduces the unsafe submission: the keyword split is overwritten.
    assert build_harvest_form_values(smap, query, {"page": 1})["party"] == "Alpha"
    reason = unmapped_harvest_reason(smap, query, {"page": 1})
    assert reason and "both target party control" in reason


def test_harvest_refuses_colliding_keyword_and_party_initial():
    from scraper.pls_search_harvest_core import unmapped_harvest_reason

    smap = {
        "fields": {"reporter": {"selector": "#reporter"}, "party": {"selector": "#party"}},
        "limits": {"harvest_role_aliases": {"keyword": "party"}},
    }
    reason = unmapped_harvest_reason(
        smap, {"reporter": "SCMR", "party_initial": "a", "keyword": "b"}, {"page": 1}
    )
    assert reason and "both target party control" in reason


def test_harvest_refuses_colliding_party_and_party_initial():
    from scraper.pls_search_harvest_core import unmapped_harvest_reason

    smap = {"fields": {"reporter": {"selector": "#reporter"}, "party": {"selector": "#party"}}}
    reason = unmapped_harvest_reason(
        smap, {"reporter": "SCMR", "party": "Alpha", "party_initial": "b"}, {"page": 1}
    )
    assert reason and "both target party control" in reason


def test_harvest_allows_independent_keyword_and_party_controls():
    from scraper.pls_search_harvest_core import unmapped_harvest_reason

    smap = {
        "fields": {
            "reporter": {"selector": "#reporter"},
            "keyword": {"selector": "#keyword"},
            "party": {"selector": "#party"},
        }
    }
    reason = unmapped_harvest_reason(
        smap, {"reporter": "SCMR", "party": "Alpha", "keyword": "b"}, {"page": 1}
    )
    assert reason is None
