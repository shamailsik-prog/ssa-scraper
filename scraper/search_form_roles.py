"""Harvest-facing search-form role resolution (dashboard party box vs keyword queries)."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Iterable, Set

# Harvest query roles that may be satisfied by another mapped control (party text box → keyword splits).
DEFAULT_HARVEST_ROLE_ALIASES: Dict[str, str] = {
    "keyword": "party",
}


def harvest_role_aliases(search_map: Dict[str, Any]) -> Dict[str, str]:
    limits = search_map.get("limits") or {}
    raw = limits.get("harvest_role_aliases")
    if isinstance(raw, dict) and raw:
        return {str(k): str(v) for k, v in raw.items()}
    return dict(DEFAULT_HARVEST_ROLE_ALIASES)


def resolve_harvest_fields(search_map: Dict[str, Any]) -> Dict[str, Any]:
    """Return a fields dict that includes alias roles (e.g. keyword → party selector)."""
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
    """Mutate fields in place for persistence; return the alias map written to limits."""
    aliases: Dict[str, str] = {}
    if "party" in fields and "keyword" not in fields:
        fields["keyword"] = deepcopy(fields["party"])
        aliases["keyword"] = "party"
    elif "keyword" in fields and "party" not in fields:
        aliases.setdefault("party", "keyword")
    return aliases


def with_resolved_harvest_map(search_map: Dict[str, Any]) -> Dict[str, Any]:
    """Shallow copy of search_map with resolved harvest fields for build/fill helpers."""
    out = dict(search_map)
    resolved = resolve_harvest_fields(search_map)
    merged = dict(search_map.get("fields") or {})
    merged.update(resolved)
    out["fields"] = merged
    return out


def verify_harvest_role_samples(search_map: Dict[str, Any]) -> Dict[str, Any]:
    """Live/dry-run proof that SCMR-style journal-year court and keyword children map to form values."""
    from scraper.pls_search_harvest_core import build_harvest_form_values, unmapped_harvest_reason

    smap = with_resolved_harvest_map(search_map)
    cursor = {"page": 1}
    samples = [
        {"reporter": "SCMR", "year": 2025, "keyword": "a"},
        {"reporter": "SCMR", "year": 2025, "court": "SC"},
        {"reporter": "YLR", "year": 2024},
    ]
    checks = []
    for query in samples:
        reason = unmapped_harvest_reason(smap, query, cursor)
        values = build_harvest_form_values(smap, query, cursor)
        checks.append(
            {
                "query": query,
                "ok": reason is None and bool(values),
                "reason": reason,
                "value_roles": sorted(values.keys()),
            }
        )
    roles = sorted(harvest_expressible_roles(search_map))
    return {
        "expressible_roles": roles,
        "court_mapped": "court" in roles,
        "keyword_mapped": "keyword" in roles,
        "samples": checks,
        "ok": all(c["ok"] for c in checks[:2]),
    }
