"""Idempotent foreign ``court`` rows for PLS mislabel backfill (court_id repoint)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.models import Court


def foreign_seed_canonical_names() -> frozenset[str]:
    return frozenset(spec["name"] for spec in FOREIGN_COURT_SEEDS)

# Slug / legacy names from prior court_name-only backfill runs.
COURT_NAME_ALIASES: Dict[str, str] = {
    "andhra-pradesh-high-court-india": "Andhra Pradesh High Court, India",
}

FOREIGN_COURT_SEEDS: List[Dict[str, Any]] = [
    {
        "name": "Supreme Court of India",
        "short_code": "SC_INDIA",
        "court_level": "supreme",
        "aliases": [],
    },
    {
        "name": "Supreme Court of UK",
        "short_code": "SC_UK",
        "court_level": "supreme",
        "aliases": ["Supreme Court of England"],
    },
    {
        "name": "Supreme Court of Bangladesh",
        "short_code": "SC_BANGLADESH",
        "court_level": "supreme",
        "aliases": [],
    },
    {
        "name": "Andhra Pradesh High Court, India",
        "short_code": "AP_HC_INDIA",
        "court_level": "high",
        "aliases": ["ANDHRA-PRADESH-HIGH-COURT-INDIA", "Andhra Pradesh High Court"],
    },
    {
        "name": "Supreme Court of South Africa",
        "short_code": "SC_SOUTH_AFRICA",
        "court_level": "supreme",
        "aliases": ["SOUTH-AFRICA-SUPREME-COURT", "South Africa Supreme Court"],
    },
    {
        "name": "High Court of Australia",
        "short_code": "HC_AUSTRALIA",
        "court_level": "high",
        "aliases": ["AUSTRALIA-HIGH-COURT", "High Court Australia"],
    },
]


def normalize_judgment_court_name(court_name: Optional[str]) -> Optional[str]:
    if not court_name or not str(court_name).strip():
        return None
    raw = str(court_name).strip()
    key = re.sub(r"\s+", " ", raw).lower()
    key = key.replace("_", "-")
    if key in COURT_NAME_ALIASES:
        return COURT_NAME_ALIASES[key]
    slug = re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    if slug in COURT_NAME_ALIASES:
        return COURT_NAME_ALIASES[slug]
    return re.sub(r"\s+", " ", raw).strip()


def _name_keys(name: str) -> set[str]:
    base = name.lower().replace(".", "").strip()
    keys = {base, base.replace(" ", "")}
    slug = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    keys.add(slug)
    return keys


async def _court_by_any_label(db: AsyncSession, label: str) -> Optional[Court]:
    if not label:
        return None
    want = _name_keys(label)
    for c in (await db.execute(select(Court))).scalars().all():
        names = {c.name, c.short_code, *(c.aliases or [])}
        for n in names:
            if _name_keys(n) & want:
                return c
    return None


async def resolve_court_row_for_name(
    db: AsyncSession,
    court_name: str,
    *,
    create: bool = True,
) -> Optional[Court]:
    """Match an existing ``court`` row; optionally create seeded foreign courts when missing."""
    canonical = normalize_judgment_court_name(court_name)
    if not canonical:
        return None
    existing = await _court_by_any_label(db, canonical)
    if existing is not None:
        return existing
    existing = await _court_by_any_label(db, court_name)
    if existing is not None:
        return existing
    if not create:
        return None
    return await ensure_foreign_court_row(db, court_name)


async def ensure_foreign_court_row(db: AsyncSession, court_name: str) -> Optional[Court]:
    """Create a seeded foreign ``court`` row when ``court_name`` matches a known foreign court."""
    canonical = normalize_judgment_court_name(court_name)
    if not canonical:
        return None
    existing = await _court_by_any_label(db, canonical)
    if existing is not None:
        return existing
    existing = await _court_by_any_label(db, court_name)
    if existing is not None:
        return existing
    seed = None
    for spec in FOREIGN_COURT_SEEDS:
        labels = {spec["name"], spec["short_code"], *(spec.get("aliases") or [])}
        if any(_name_keys(lbl) & _name_keys(canonical) for lbl in labels):
            seed = spec
            break
        if any(_name_keys(lbl) & _name_keys(court_name) for lbl in labels):
            seed = spec
            break
    if seed is None:
        return await ensure_link_court_row(db, court_name)
    row = Court(
        name=seed["name"],
        short_code=seed["short_code"],
        court_level=seed["court_level"],
        aliases=list(dict.fromkeys([*(seed.get("aliases") or []), court_name])) if court_name != seed["name"] else (seed.get("aliases") or None),
    )
    db.add(row)
    await db.flush()
    return row


async def ensure_link_court_row(db: AsyncSession, court_name: str) -> Optional[Court]:
    """Create specialty / election-tribunal rows from ``judgment_court_link_registry`` seeds."""
    from scraper.judgment_court_link_registry import (
        all_linkable_seed_specs,
        election_tribunal_seed_spec,
        pls_court_label_key,
    )

    canonical = normalize_judgment_court_name(court_name) or court_name
    existing = await _court_by_any_label(db, canonical)
    if existing is not None:
        return existing
    key = pls_court_label_key(court_name)
    seed = None
    for spec in all_linkable_seed_specs():
        labels = {spec["name"], spec["short_code"], *(spec.get("aliases") or [])}
        if any(pls_court_label_key(lbl) == key for lbl in labels):
            seed = spec
            break
    if seed is None and key.startswith("election tribunal"):
        seed = election_tribunal_seed_spec(canonical)
    if seed is None:
        return None
    row = Court(
        name=seed["name"],
        short_code=seed["short_code"],
        court_level=seed["court_level"],
        aliases=list(dict.fromkeys([*(seed.get("aliases") or []), court_name])) if court_name != seed["name"] else (seed.get("aliases") or None),
    )
    db.add(row)
    await db.flush()
    return row
