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
