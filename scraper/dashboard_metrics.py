"""The /status dashboard's live sections: is every moving part working (health), how fast the corpus is
growing right now (speed) and how far each walk has come (progress). Key-free like the rest of /status:
counts, times and states only, never a record's text, a URL or a configuration value."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.models import ArchiveObject, ArchiveTarget, CrawlFrontier, Instrument, Judgment, ScraperSource, SourceProvenance, Statute, StatuteSection

# How long a heartbeat may be old before its light turns red (the job's own cadence plus slack).
HEARTBEAT_LIMITS_SECONDS = {
    "dispatch": 5 * 60,  # Beat sends it every minute
    "promotion": 20 * 60,
    "archive_mirror": 70 * 60,
    "embeddings": 20 * 60,
    "login_session": 3 * 3600,
    "public": 12 * 3600,
    "caseid_walk": 30 * 60,
}
HEARTBEAT_LABELS = {
    "dispatch": "Scheduler (Celery Beat) and dispatch",
    "promotion": "Promotion of staged records",
    "archive_mirror": "Archive / Google Drive copy",
    "embeddings": "Embeddings",
    "login_session": "PakistanLawSite scraper jobs",
    "public": "Public-source scraper jobs",
    "caseid_walk": "PakistanLawSite case-number walk",
}
EXPECTED_WORKERS = {
    "scraper-login": "PakistanLawSite worker",
    "scraper-public": "Public sources worker",
    "maintenance": "Housekeeping worker",
    "embed": "Embeddings worker",
}
_PING_CACHE: Dict[str, Any] = {"at": 0.0, "workers": None}
_PING_TTL_SECONDS = 30.0


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, datetime) else value


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def _ping_workers() -> Optional[Dict[str, bool]]:
    """{worker base name: alive} from a Celery broadcast ping, cached for 30 s. None when the broker is unreachable."""
    cached = _PING_CACHE["workers"]
    if cached and time.monotonic() - _PING_CACHE["at"] < _PING_TTL_SECONDS:
        return cached

    def _ping() -> Optional[List[Dict[str, Any]]]:
        from scraper.tasks.celery_app import app

        try:
            return app.control.ping(timeout=1.5)
        except Exception:
            return None

    replies = await asyncio.to_thread(_ping)
    if replies is None:
        return None
    alive: Dict[str, bool] = {}
    for reply in replies:
        for node in reply:
            alive[str(node).split("@", 1)[0]] = True
    if not alive:
        return None
    _PING_CACHE.update({"at": time.monotonic(), "workers": alive})
    return alive


async def _redis_ok() -> bool:
    try:
        import redis.asyncio as aioredis

        client = aioredis.from_url(settings.REDIS_URL, socket_timeout=2, socket_connect_timeout=2)
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()
    except Exception:
        return False


async def health_section(*, sources: List[Dict[str, Any]], targets: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """One light per moving part: ok / warn / down, with the time it last did its work."""
    from scraper.heartbeat import read_all

    checks: List[Dict[str, Any]] = [{"name": "Database", "state": "ok", "detail": "answering"}]
    redis_ok = await _redis_ok()
    checks.append({"name": "Redis (task queue)", "state": "ok" if redis_ok else "down", "detail": "answering" if redis_ok else "not reachable"})
    workers = await _ping_workers() if redis_ok else None
    for base, label in EXPECTED_WORKERS.items():
        if workers is None:
            checks.append({"name": label, "state": "warn", "detail": "could not ask the workers"})
        elif workers.get(base):
            checks.append({"name": label, "state": "ok", "detail": "running"})
        elif base == "maintenance" and workers.get("scraper-public"):
            checks.append({"name": label, "state": "warn", "detail": "not started; the public worker is covering housekeeping"})
        else:
            checks.append({"name": label, "state": "down", "detail": "not running"})
    beats = await read_all() if redis_ok else {}
    active = {s["source"] for s in sources if s.get("state") == "ACTIVE"}
    for name, label in HEARTBEAT_LABELS.items():
        at = _parse(beats.get(name))
        limit = HEARTBEAT_LIMITS_SECONDS[name]
        idle_ok = (name == "public" and not (active - {"PakistanLawSite"})) or (name in ("login_session", "caseid_walk") and "PakistanLawSite" not in active)
        if at is None:
            state, detail = ("ok" if idle_ok else "warn"), ("idle (no active source)" if idle_ok else "no run recorded yet")
        else:
            age = (now - at).total_seconds()
            state = "ok" if age <= limit or idle_ok else ("warn" if age <= 3 * limit else "down")
            detail = f"last ran {int(age // 60)} min ago"
        checks.append({"name": label, "state": state, "detail": detail, "last_at": _iso(at)})
    for s in sources:
        for slot in s.get("slots") or []:
            st = slot.get("state")
            checks.append(
                {
                    "name": f"{s['source']} login slot {slot.get('slot')}",
                    "state": "ok" if st == "ACTIVE" else ("warn" if st in ("RECOVERING", "IDLE", "NEEDS_HUMAN_LOGIN") else "down"),
                    "detail": (st or "unknown") + (f": {slot['reason'][:90]}" if slot.get("reason") and st != "ACTIVE" else ""),
                }
            )
    for t in targets:
        if not t.get("enabled"):
            continue
        bad = int(t.get("consecutive_failures") or 0)
        checks.append(
            {
                "name": f"Archive target {t['name']} ({t['type']})",
                "state": ("ok" if t.get("last_ok_at") else "warn") if bad == 0 else ("warn" if bad < 3 else "down"),
                "detail": f"last good write {t.get('last_ok_at') or 'never'}" + (f"; {bad} failed runs in a row" if bad else ""),
            }
        )
    worst = "down" if any(c["state"] == "down" for c in checks) else ("warn" if any(c["state"] == "warn" for c in checks) else "ok")
    return {"overall": worst, "checks": checks}


async def _count_since(db: AsyncSession, column, since: datetime, *where) -> int:
    stmt = select(func.count()).select_from(column.class_).where(column >= since, *where)
    return int((await db.execute(stmt)).scalar() or 0)


async def speed_section(db: AsyncSession, *, now: datetime) -> Dict[str, Any]:
    """Pages fetched and records added over the last 15 minutes, hour and day, with hourly rates."""
    windows = {"15m": timedelta(minutes=15), "1h": timedelta(hours=1), "24h": timedelta(hours=24)}
    out: Dict[str, Any] = {}
    for key, span in windows.items():
        since = now - span
        hours = span.total_seconds() / 3600
        pages = int((await db.execute(select(func.count()).select_from(SourceProvenance).where(SourceProvenance.fetched_at >= since))).scalar() or 0)
        judgments = await _count_since(db, Judgment.promoted_at, since)
        statutes = await _count_since(db, Statute.created_at, since)
        sections = await _count_since(db, StatuteSection.created_at, since)
        instruments = await _count_since(db, Instrument.created_at, since)
        out[key] = {
            "pages": pages,
            "judgments": judgments,
            "statutes": statutes,
            "statute_sections": sections,
            "instruments": instruments,
            "pages_per_hour": round(pages / hours, 1),
            "judgments_per_hour": round(judgments / hours, 1),
        }
    by_source = [
        {"source": name, "pages_last_hour": int(n)}
        for name, n in (
            await db.execute(
                select(SourceProvenance.source_name, func.count())
                .where(SourceProvenance.fetched_at >= now - timedelta(hours=1))
                .group_by(SourceProvenance.source_name)
                .order_by(func.count().desc())
            )
        ).all()
    ]
    out["by_source_last_hour"] = by_source
    return out


async def progress_section(db: AsyncSession, *, pls_cfg: Dict[str, Any], by_reporter_year: List[Dict[str, Any]], now: datetime) -> Dict[str, Any]:
    """How far each walk has come: PakistanLawSite journal grids, public-source queues, the archive copy."""
    from scraper.pls_grid_health import JOURNAL_CURSOR_PREFIX, cursor_saturated, journal_cursor_key, journal_unsupported
    from scraper.storage.archive import ArchiveMirror

    per_reporter: Dict[str, int] = {}
    for row in by_reporter_year:
        per_reporter[str(row["reporter"])] = per_reporter.get(str(row["reporter"]), 0) + int(row["judgments"])
    journals = []
    names = list(dict.fromkeys(list(settings.grid_journals) + [k[len(JOURNAL_CURSOR_PREFIX):] for k in pls_cfg if isinstance(k, str) and k.startswith(JOURNAL_CURSOR_PREFIX)]))
    for journal in names:
        cur = pls_cfg.get(journal_cursor_key(journal)) or {}
        total = int(cur.get("last_total_rows") or 0)
        offset = int(cur.get("row_offset") or 0)
        lap = cur.get("lap") or {}
        if journal_unsupported(cur, now=now):
            state = "set aside"
        elif cursor_saturated(cur, now=now):
            state = "complete (rechecked later)"
        elif not cur:
            state = "not started"
        else:
            state = "walking"
        journals.append(
            {
                "journal": journal,
                "state": state,
                "grid_rows": total or None,
                "position": offset if total else None,
                "lap_rows_read": int(lap.get("rows") or 0),
                "lap_pct": round(100.0 * min(int(lap.get("rows") or 0), total) / total, 1) if total else None,
                "judgments_in_corpus": per_reporter.get(journal, 0),
                "note": cur.get("unsupported_reason") or cur.get("saturated_reason"),
            }
        )
    frontier = {}
    for name, status, n in (await db.execute(select(CrawlFrontier.source_name, CrawlFrontier.status, func.count()).group_by(CrawlFrontier.source_name, CrawlFrontier.status))).all():
        frontier.setdefault(name, {})[status] = int(n)
    states = {s.source_name: (s.state, s.is_active) for s in (await db.execute(select(ScraperSource))).scalars().all()}
    public = []
    for name, counts in sorted(frontier.items()):
        if name == "PakistanLawSite":
            continue
        done = counts.get("done", 0) + counts.get("retired", 0)
        total = sum(counts.values())
        public.append({"source": name, "state": (states.get(name) or ("?", False))[0], "done": done, "pending": counts.get("pending", 0) + counts.get("in_progress", 0), "pct": round(100.0 * done / total, 1) if total else None})
    mirror = ArchiveMirror(db)
    archive = []
    for t in (await db.execute(select(ArchiveTarget).where(ArchiveTarget.enabled.is_(True)).order_by(ArchiveTarget.name))).scalars().all():
        lag = await mirror.lag(t)
        lag.pop("_last_written", None)
        written_hour = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(ArchiveObject)
                    .where(
                        ArchiveObject.target_id == t.id,
                        ArchiveObject.status == "written",
                        ArchiveObject.judgment_id.isnot(None),
                        ArchiveObject.written_at >= now - timedelta(hours=1),
                    )
                )
            ).scalar()
            or 0
        )
        per_hour = written_hour / 3.0  # three objects per judgment
        remaining = int(lag["judgments_unmirrored"])
        archive.append(
            {
                "target": t.name,
                "type": t.target_type,
                "judgments_total": int(lag["judgments_total"]),
                "judgments_missing": remaining,
                "pct": round(100.0 * (lag["judgments_total"] - remaining) / lag["judgments_total"], 1) if lag["judgments_total"] else None,
                "judgments_per_hour": round(per_hour, 1),
                "eta_hours": round(remaining / per_hour, 1) if per_hour > 0 and remaining else (0 if not remaining else None),
            }
        )
    walk = pls_cfg.get("caseid_walk") or {}
    walk_groups = walk.get("groups") or {}
    caseid_walk = {
        "calibrated": bool(walk.get("calibrated_at")),
        "calibration": walk.get("calibration"),
        "current_group": walk.get("current"),
        "groups_started": len(walk_groups),
        "groups_done": sum(1 for g in walk_groups.values() if g.get("done")),
        "found": sum(int(g.get("hits") or 0) for g in walk_groups.values()),
        "misses": sum(int(g.get("misses") or 0) for g in walk_groups.values()),
        "last_probe_at": walk.get("last_probe_at"),
    }
    return {"pakistanlawsite_journals": journals, "public_sources": public, "archive": archive, "caseid_walk": caseid_walk}
