"""Real coverage numbers for the live dashboard: what the site has shown us (discovered) against what the
corpus holds (collected), per journal and per journal x year, with the 24h / 7-day rate and an ETA.

Key-free like /status: counts, times and states only."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.watchdog_settings import wsettings

SOURCE_NAME = "PakistanLawSite"
WATCHDOG_KEY = "pls_throughput_watchdog"
HOST_STATE_FILE = "pls_watchdog_host.json"


def norm_reporter(name: Any) -> str:
    """"PCrLJ", "PCRLJ", "P.Cr.L.J" -> "pcrlj" (case, dots and spaces never split one journal in two)."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def journal_names(*sources: Iterable[Any]) -> List[str]:
    """Union of journal names in first-seen order, one spelling per journal (the first one wins)."""
    seen: Dict[str, str] = {}
    for src in sources:
        for name in src or []:
            text = str(name or "").strip()
            key = norm_reporter(text)
            if key and key not in seen:
                seen[key] = text
    return list(seen.values())


def base_query_reporter_year(query_json: Dict[str, Any]) -> Optional[Tuple[str, int]]:
    """(reporter, year) for a base journal x year search query; None for split children and other families."""
    q = dict(query_json or {})
    if set(q) != {"reporter", "year"}:
        return None
    try:
        return str(q["reporter"]), int(q["year"])
    except (TypeError, ValueError):
        return None


def discovered_for(site_total: Optional[int], rows_seen: Optional[int]) -> int:
    """Citations a finished query showed us: the site's own total when printed, else the rows we enumerated."""
    if site_total is not None and int(site_total) > 0:
        return int(site_total)
    return max(0, int(rows_seen or 0))


def build_journal_rows(
    *,
    journals: List[str],
    collected: Dict[str, int],
    grid_totals: Dict[str, int],
    search_cells: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """One row per journal: collected judgments, citations discovered (largest of the journal's grid size and
    the sum of its finished journal x year searches; never below collected), coverage and search progress."""
    by_journal: Dict[str, Dict[str, int]] = {}
    for cell in search_cells:
        key = norm_reporter(cell["reporter"])
        agg = by_journal.setdefault(key, {"done": 0, "pending": 0, "failed": 0, "discovered": 0, "rows_new": 0})
        status = cell.get("status")
        if status in ("done", "split"):
            agg["done"] += 1
            agg["discovered"] += int(cell.get("discovered") or 0)
        elif status == "failed":
            agg["failed"] += 1
        else:
            agg["pending"] += 1
        agg["rows_new"] += int(cell.get("rows_new") or 0)
    rows = []
    for journal in journals:
        key = norm_reporter(journal)
        have = int(collected.get(key, 0))
        agg = by_journal.get(key, {})
        grid = int(grid_totals.get(key, 0))
        evidence = max(grid, int(agg.get("discovered", 0)))  # citations actually seen on the site
        discovered = max(evidence, have)
        rows.append(
            {
                "journal": journal,
                "collected": have,
                "discovered": discovered,
                "discovery_evidence": evidence,
                "grid_rows": grid or None,
                "coverage_pct": round(100.0 * have / discovered, 1) if discovered else None,
                "years_done": int(agg.get("done", 0)),
                "years_pending": int(agg.get("pending", 0)),
                "years_failed": int(agg.get("failed", 0)),
                "search_rows_new": int(agg.get("rows_new", 0)),
            }
        )
    rows.sort(key=lambda r: (-r["collected"], r["journal"]))
    return rows


def eta_days(remaining: int, per_day: float) -> Optional[float]:
    if remaining <= 0:
        return 0.0
    if per_day <= 0:
        return None
    return round(remaining / per_day, 1)


def pakistancode_transition(
    journal_rows: List[Dict[str, Any]],
    *,
    required: List[str],
    threshold: float,
) -> Dict[str, Any]:
    """Whether citation coverage is high enough to move the harvest on to PakistanCode statutes.

    Every required journal must have something discovered (an undiscovered journal is not "covered"), and
    collected / discovered over those journals must reach `threshold`."""
    by_key = {norm_reporter(r["journal"]): r for r in journal_rows}
    # discovery must come from the site (grid or finished searches), never from the collected count alone
    missing = [
        j for j in required
        if int((by_key.get(norm_reporter(j)) or {}).get("discovery_evidence", (by_key.get(norm_reporter(j)) or {}).get("discovered")) or 0) <= 0
    ]
    disc = sum(int((by_key.get(norm_reporter(j)) or {}).get("discovered") or 0) for j in required)
    have = sum(int((by_key.get(norm_reporter(j)) or {}).get("collected") or 0) for j in required)
    coverage = (have / disc) if disc else 0.0
    ready = bool(required) and not missing and coverage >= threshold
    return {
        "coverage": round(coverage, 4),
        "threshold": threshold,
        "journals_without_discovery": missing,
        "ready": ready,
    }


def read_host_state(state_dir: Optional[str] = None) -> Dict[str, Any]:
    path = Path(state_dir or getattr(settings, "STATE_STORAGE_PATH", "") or "/app/state") / HOST_STATE_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


async def coverage_payload(db: AsyncSession, *, now: Optional[datetime] = None) -> Dict[str, Any]:
    from scraper.models import ArchiveObject, ArchiveTarget, EmbeddingQueue, Judgment, PlsSearchHarvestQuery, ScraperSource, SearchFormMap, SourceProvenance
    from scraper.pls_grid_health import JOURNAL_CURSOR_PREFIX

    now = now or datetime.now(timezone.utc)
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    cfg = dict(source.config_json or {}) if source else {}
    # collected, by journal and by journal x year
    collected: Dict[str, int] = {}
    collected_ry: Dict[Tuple[str, int], int] = {}
    corpus_names: List[str] = []
    for rep, yr, n in (await db.execute(select(Judgment.reporter, Judgment.year, func.count()).group_by(Judgment.reporter, Judgment.year))).all():
        if not rep:
            continue
        corpus_names.append(rep)
        collected[norm_reporter(rep)] = collected.get(norm_reporter(rep), 0) + int(n)
        if yr is not None:
            collected_ry[(norm_reporter(rep), int(yr))] = collected_ry.get((norm_reporter(rep), int(yr)), 0) + int(n)
    total_judgments = int((await db.execute(select(func.count()).select_from(Judgment))).scalar() or 0)
    # journals the form offers
    offered: List[str] = []
    m = (
        await db.execute(
            select(SearchFormMap).where(SearchFormMap.source_name == SOURCE_NAME, SearchFormMap.is_active.is_(True)).order_by(SearchFormMap.created_at.desc()).limit(1)
        )
    ).scalars().first()
    if m is not None:
        offered = list((m.limits or {}).get("reporters_offered") or ((m.fields or {}).get("reporter") or {}).get("options") or [])
    journals = journal_names(settings.subscribed_reporters, offered, corpus_names)
    # grid sizes per journal (per-journal cursors) and the default grid
    grid_totals: Dict[str, int] = {}
    for key, cur in cfg.items():
        if isinstance(key, str) and key.startswith(JOURNAL_CURSOR_PREFIX) and isinstance(cur, dict):
            total = int(cur.get("last_total_rows") or (cur.get("lap") or {}).get("total_rows") or 0)
            grid_totals[norm_reporter(key[len(JOURNAL_CURSOR_PREFIX):])] = total
    default_cur = cfg.get("citation_grid_cursor") if isinstance(cfg.get("citation_grid_cursor"), dict) else {}
    default_grid_rows = int(default_cur.get("last_total_rows") or (default_cur.get("lap") or {}).get("total_rows") or 0)
    # search harvest journal x year cells
    cells: List[Dict[str, Any]] = []
    queue: Dict[str, int] = {}
    last_query_at = None
    for row in (await db.execute(select(PlsSearchHarvestQuery).where(PlsSearchHarvestQuery.source_name == SOURCE_NAME))).scalars().all():
        queue[row.status] = queue.get(row.status, 0) + 1
        if row.last_run_at and (last_query_at is None or row.last_run_at > last_query_at):
            last_query_at = row.last_run_at
        ry = base_query_reporter_year(row.query_json or {})
        if ry is None:
            continue
        rep, yr = ry
        cells.append(
            {
                "reporter": rep,
                "year": yr,
                "status": row.status,
                "discovered": discovered_for(row.site_total_results, row.rows_seen),
                "rows_seen": int(row.rows_seen or 0),
                "rows_new": int(row.rows_new or 0),
                "collected": collected_ry.get((norm_reporter(rep), yr), 0),
                "error": (row.last_error or "")[:160] or None,
            }
        )
    journal_rows = build_journal_rows(journals=journals, collected=collected, grid_totals=grid_totals, search_cells=cells)
    discovered_total = sum(r["discovered"] for r in journal_rows)
    collected_total = sum(r["collected"] for r in journal_rows)
    # rates
    day = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.promoted_at >= now - timedelta(hours=24)))).scalar() or 0)
    hour = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.promoted_at >= now - timedelta(hours=1)))).scalar() or 0)
    week = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.promoted_at >= now - timedelta(days=7)))).scalar() or 0)
    pages_1h = int(
        (await db.execute(select(func.count()).select_from(SourceProvenance).where(SourceProvenance.source_name == SOURCE_NAME, SourceProvenance.fetched_at >= now - timedelta(hours=1)))).scalar() or 0
    )
    pages_24h = int(
        (await db.execute(select(func.count()).select_from(SourceProvenance).where(SourceProvenance.source_name == SOURCE_NAME, SourceProvenance.fetched_at >= now - timedelta(hours=24)))).scalar() or 0
    )
    per_day = max(float(day), week / 7.0)
    remaining = max(0, discovered_total - collected_total)
    # Drive mirror and embeddings
    drive = []
    for t in (await db.execute(select(ArchiveTarget).where(ArchiveTarget.enabled.is_(True), ArchiveTarget.target_type == "google_drive"))).scalars().all():
        n = int(
            (
                await db.execute(
                    select(func.count(func.distinct(ArchiveObject.judgment_id))).where(
                        ArchiveObject.target_id == t.id, ArchiveObject.status == "written", ArchiveObject.judgment_id.isnot(None)
                    )
                )
            ).scalar()
            or 0
        )
        drive.append({"target": t.name, "judgments_mirrored": n, "last_ok_at": t.last_ok_at.isoformat() if t.last_ok_at else None})
    emb = {st: int(n) for st, n in (await db.execute(select(EmbeddingQueue.status, func.count()).group_by(EmbeddingQueue.status))).all()}
    embedded = int((await db.execute(select(func.count()).select_from(Judgment).where(Judgment.embedding.isnot(None)))).scalar() or 0)
    required = list(settings.subscribed_reporters)
    transition = pakistancode_transition(
        journal_rows,
        required=required,
        threshold=float(wsettings.PLS_AUTO_UNPAUSE_COVERAGE or 0.95),
    )
    transition["enabled"] = bool(wsettings.PLS_AUTO_UNPAUSE_PAKISTANCODE)
    transition["done"] = cfg.get("pakistancode_auto_unpaused_at")
    return {
        "generated_at": now.isoformat(),
        "source_state": source.state if source else None,
        "totals": {
            "judgments_in_corpus": total_judgments,
            "citations_discovered": discovered_total,
            "citations_collected": collected_total,
            "coverage_pct": round(100.0 * collected_total / discovered_total, 1) if discovered_total else None,
            "remaining": remaining,
            "default_grid_rows": default_grid_rows or None,
        },
        "rate": {
            "judgments_1h": hour,
            "judgments_24h": day,
            "judgments_7d": week,
            "judgments_per_day": round(per_day, 1),
            "pls_pages_1h": pages_1h,
            "pls_pages_24h": pages_24h,
            "eta_days": eta_days(remaining, per_day),
        },
        "journals": journal_rows,
        "grid": sorted(cells, key=lambda c: (norm_reporter(c["reporter"]), -c["year"])),
        "search_queue": queue,
        "search_last_query_at": last_query_at.isoformat() if last_query_at else None,
        "drive": drive,
        "embeddings": {"embedded": embedded, "queue": emb, "pending": emb.get("pending", 0)},
        "watchdog": cfg.get(WATCHDOG_KEY) or {},
        "watchdog_host": read_host_state(),
        "pakistancode_transition": transition,
    }
