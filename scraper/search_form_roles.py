"""Harvest-facing search-form role resolution (dashboard party box vs keyword split queries)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional, Set

# Harvest ``keyword`` split tokens submit to the party text box on the live dashboard.
DEFAULT_HARVEST_ROLE_ALIASES: Dict[str, str] = {
    "keyword": "party",
}


def keyword_text_submit_role(search_map: Dict[str, Any]) -> Optional[str]:
    aliases = harvest_role_aliases(search_map)
    if aliases.get("keyword"):
        return aliases["keyword"]
    stored = dict(search_map.get("fields") or {})
    stored.pop("_all", None)
    if "keyword" in stored:
        return "keyword"
    if "party" in stored:
        return "party"
    resolved = resolve_harvest_fields(search_map)
    if "party" in resolved:
        return "party"
    if "keyword" in resolved:
        return "keyword"
    return None


def apply_harvest_query_keyword(values: Dict[str, str], search_map: Dict[str, Any], query_json: Dict[str, Any]) -> None:
    """Submit split keyword letters to the live party/keyword control."""
    kw = query_json.get("keyword")
    if not kw:
        return
    role = keyword_text_submit_role(search_map)
    if role:
        values[role] = str(kw)
        if role != "keyword":
            values.pop("keyword", None)


def apply_harvest_party_initial(values: Dict[str, str], search_map: Dict[str, Any], query_json: Dict[str, Any]) -> None:
    """Map party_initial splits onto party when the form has no dedicated role."""
    pi = query_json.get("party_initial")
    if pi is None:
        return
    fields = dict(search_map.get("fields") or {})
    fields.pop("_all", None)
    if "party_initial" in fields:
        values["party_initial"] = str(pi)
        return
    if "party" in fields:
        values["party"] = str(pi)
        return
    if not query_json.get("keyword"):
        apply_harvest_query_keyword(values, search_map, {"keyword": str(pi)})


def harvest_role_aliases(search_map: Dict[str, Any]) -> Dict[str, str]:
    limits = search_map.get("limits") or {}
    raw = limits.get("harvest_role_aliases")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    stored = dict(search_map.get("fields") or {})
    stored.pop("_all", None)
    if "party" in stored and "keyword" not in stored:
        return dict(DEFAULT_HARVEST_ROLE_ALIASES)
    return {}


def resolve_harvest_fields(search_map: Dict[str, Any]) -> Dict[str, Any]:
    base = dict(search_map.get("fields") or {})
    base.pop("_all", None)
    out = deepcopy(base)
    for alias_role, source_role in harvest_role_aliases(search_map).items():
        if alias_role in out:
            continue
        source = out.get(source_role)
        if source:
            out[alias_role] = deepcopy(source)
    return out


def harvest_expressible_roles(search_map: Dict[str, Any]) -> Set[str]:
    return set(resolve_harvest_fields(search_map).keys()) - {"submit"}


def enrich_fields_with_harvest_aliases(fields: Dict[str, Any]) -> Dict[str, str]:
    aliases: Dict[str, str] = {}
    if "party" in fields and "keyword" not in fields:
        aliases["keyword"] = "party"
    return aliases
