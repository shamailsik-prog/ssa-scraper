"""
Archive mirror and reconcile_storage (Section 9 / 9A; Amendment §12, §19).

* The archive is a MIRROR. PostgreSQL stays the working database; the application never reads
  the archive.
* Folder tree (created by the service):
      Judgments/<court_code>/<year>/<citation_slug>/original.pdf        (genuine source PDF)
      Judgments/<court_code>/<year>/<citation_slug>/rendered_copy.pdf   (HTML-only judgments, labelled)
      Judgments/<court_code>/<year>/<citation_slug>/judgment.txt
      Judgments/<court_code>/<year>/<citation_slug>/metadata.json     (corpus record, scraper/corpus_record.py)
      Statutes/<statute_slug>/<section>/v<n>.txt
      _index/judgments-YYYYMMDD-HHMMSS.csv, _index/statutes-YYYYMMDD-HHMMSS.csv
* Write-once: an object key is written at most once per target; a different hash at the same
  key is a MISMATCH, never an overwrite.
* Per-target failure isolation: one target failing never stops the others.
* Login-session rows are mirrored only when both the deployment flag and the target flag allow.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Literal, Optional, Tuple

_DriveEnsureTreeResult = Literal["ok", "failed", "timed_out"]
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.corpus_record import judgment_record
from scraper.fetchers import read_raw
from scraper.models import ArchiveObject, ArchiveTarget, Judgment, SourceProvenance, Statute, StatuteSection, StatuteSectionVersion
from scraper.notify import notify
from scraper.parsers.pdf_writer import render_judgment_pdf_bytes
from scraper.security import is_login_session
from scraper.storage.adapters import ArchiveAdapter, ArchiveError, ObjectExists, build_adapter

logger = logging.getLogger(__name__)


def _drive_folder_path_for_key(key: str) -> str:
    return key.rsplit("/", 1)[0]


def _drive_folder_cached(drive_cfg: Dict[str, Any], folder: str) -> bool:
    cache = drive_cfg.get("_drive_folder_cache")
    if not isinstance(cache, dict):
        return False
    return folder.strip("/") in cache


def _distinct_folder_keys(folder_keys: List[str]) -> List[str]:
    """One object key per distinct Drive folder path, preserving first-seen order."""
    seen: set[str] = set()
    out: List[str] = []
    for key in folder_keys:
        folder = _drive_folder_path_for_key(key)
        if folder in seen:
            continue
        seen.add(folder)
        out.append(key)
    return out


def _aware(d: Optional[datetime]) -> datetime:
    if d is None:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def slug(s: Optional[str], limit: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")
    return (s or "unknown")[:limit]


def judgment_prefix(j: Judgment) -> str:
    """Citations/<reporter>/<year>/<citation> for reported judgments; Unreported/<court>/<proceeding>/<year>/<identity> for
    judgments marked unreported; Unreported/<court>/<year>/<citation> for the court-website judgments promoted before that."""
    if j.reporter and j.year:
        return f"Citations/{slug(j.reporter, 20)}/{j.year}/{slug(j.canonical_citation)}"
    court = slug(j.court_name or "Unknown_Court", 40)
    if getattr(j, "unreported", False):
        # the unreported section, sorted by the kind of proceeding (bail, constitutional petition, suit ...)
        from scraper.unreported import PROCEEDING_LABELS

        kind = slug(PROCEEDING_LABELS.get(j.proceeding_type or "other", "Other"), 40)
        return f"Unreported/{court}/{kind}/{j.year or 'unknown'}/{slug(j.canonical_citation, 200)}"  # whole identity: a same-day order differs only in its tail
    return f"Unreported/{court}/{j.year or 'unknown'}/{slug(j.canonical_citation)}"


def target_config(t: ArchiveTarget) -> Dict[str, Any]:
    cfg: Dict[str, Any] = {}
    if t.config_encrypted:
        cfg = json.loads(settings.decrypt_value(t.config_encrypted))
    # environment fallbacks per type (firm values; blank stays blank)
    env = {
        "local_path": {"path": settings.ARCHIVE_LOCAL_PATH},
        "s3_compatible": {"endpoint": settings.ARCHIVE_S3_ENDPOINT, "bucket": settings.ARCHIVE_S3_BUCKET, "access_key": settings.ARCHIVE_S3_ACCESS_KEY, "secret_key": settings.ARCHIVE_S3_SECRET_KEY.get_secret_value(), "region": settings.ARCHIVE_S3_REGION},
        "sftp": {"host": settings.ARCHIVE_SFTP_HOST, "port": settings.ARCHIVE_SFTP_PORT, "user": settings.ARCHIVE_SFTP_USER, "password": settings.ARCHIVE_SFTP_PASSWORD.get_secret_value(), "root": settings.ARCHIVE_SFTP_ROOT},
        "smb": {"server": settings.ARCHIVE_SMB_SERVER, "share": settings.ARCHIVE_SMB_SHARE, "user": settings.ARCHIVE_SMB_USER, "password": settings.ARCHIVE_SMB_PASSWORD.get_secret_value(), "root": settings.ARCHIVE_SMB_ROOT},
        "dropbox": {"token": settings.ARCHIVE_DROPBOX_TOKEN.get_secret_value(), "root": settings.ARCHIVE_DROPBOX_ROOT},
        "onedrive": {"token": settings.ARCHIVE_ONEDRIVE_TOKEN.get_secret_value(), "root": settings.ARCHIVE_ONEDRIVE_ROOT},
        "google_drive": {"service_account_json": settings.GOOGLE_APPLICATION_CREDENTIALS_JSON.get_secret_value() if settings.GOOGLE_APPLICATION_CREDENTIALS_JSON else "", "folder_id": settings.DRIVE_FOLDER_ROOT},
    }.get(t.target_type, {})
    for k, v in env.items():
        if v and not cfg.get(k):
            cfg[k] = v
    if t.root_path and not cfg.get("root") and t.target_type != "local_path":
        cfg["root"] = t.root_path
    if t.target_type == "local_path" and t.root_path and not cfg.get("path"):
        cfg["path"] = t.root_path
    return cfg


RETRY_DELAYS = (1.0, 3.0)
JUDGMENT_OBJECT_COUNT = 3  # document (original or rendered copy) + judgment.txt + metadata.json
OBJECTS_CACHE_MAX = 600  # judgments whose built objects one run keeps for the next target (a slice is 500)


def _http_status_from_exc(exc: BaseException) -> Optional[int]:
    resp = getattr(exc, "resp", None)
    status = getattr(resp, "status", None)
    if status is not None:
        return int(status)
    code = getattr(exc, "status_code", None)
    if code is not None:
        return int(code)
    return None


def is_drive_rate_limited(exc: BaseException) -> bool:
    """True for HTTP 429 or Drive 403 with rateLimitExceeded (not arbitrary '429' in text)."""
    status = _http_status_from_exc(exc)
    if status == 429:
        return True
    reason = str(getattr(exc, "reason", "") or "").lower().replace(" ", "").replace("_", "")
    if reason == "ratelimitexceeded":
        return True
    msg_norm = str(exc).lower().replace(" ", "").replace("_", "")
    if status == 403 and "ratelimitexceeded" in msg_norm:
        return True
    return False


async def drive_rate_limit_backoff() -> None:
    await asyncio.sleep(random.uniform(0.5, 2.5))


class _AdaptiveConcurrency:
    """Caps concurrent Drive uploads; halving reduces the cap for the rest of the run."""

    def __init__(self, limit: int) -> None:
        self.limit = max(1, int(limit))
        self._in_flight = 0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                if self._in_flight < self.limit:
                    self._in_flight += 1
                    return
            await asyncio.sleep(0.01)

    async def release(self) -> None:
        async with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def halve(self) -> int:
        self.limit = max(1, self.limit // 2)
        return self.limit


@dataclass(frozen=True)
class _JudgmentUploadWork:
    target_id: UUID
    judgment_id: UUID
    prov_id: Optional[UUID]
    access_method: str
    obj: Dict[str, Any]


@dataclass
class _AdapterStoreOutcome:
    status: str
    content_hash: str
    byte_size: int
    stored_size: Optional[int] = None


async def _retry(fn, *args, rate_limit_sensitive: bool = False):
    """Run an adapter call, retrying transient failures ('The read operation timed out' from Drive)."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return await fn(*args)
        except ObjectExists:
            raise
        except Exception as exc:
            if rate_limit_sensitive and is_drive_rate_limited(exc):
                raise
            if delay is None:
                raise
            await asyncio.sleep(delay)


class ArchiveMirror:
    def __init__(self, db: AsyncSession, *, adapter_factory=build_adapter, deadline: Optional[float] = None, commit_progress: bool = False):
        self.db = db
        self.adapter_factory = adapter_factory
        self.summary: Dict[str, Dict[str, int]] = {}
        self._gate: Dict[Any, bool] = {}  # target id -> whether this run writes to it (batch transfer)
        # A run with a deadline (time.monotonic()) stops starting new objects once it passes, and one
        # with commit_progress commits its ledger as it goes: after the statute re-scrape a single
        # all-or-nothing pass over thousands of objects ran for hours, and a killed worker lost every
        # ledger row it had written (8 October 2026). The next run carries on where this one stopped.
        self.deadline = deadline
        self.commit_progress = commit_progress
        self.out_of_time = False
        # Set when a target took a full backlog slice: more is owed, so the caller queues the next run at once
        # instead of waiting for Beat (13 October 2026: 11,000 released judgments, one 500-row slice per 30 minutes).
        self.more_pending = False
        # Each judgment's objects (the rendered PDF above all) are built once per run and reused for every target.
        self._objects_cache: Dict[Any, List[Dict[str, Any]]] = {}

    def _time_up(self) -> bool:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.out_of_time = True
        return self.out_of_time

    async def _checkpoint(self) -> None:
        if self.commit_progress:
            await self.db.commit()

    async def targets(self, *, for_reconcile: bool = False) -> List[ArchiveTarget]:
        # Least recently completed first: a run walks the targets against one time budget, and in a fixed order the
        # last target starved whenever there was a backlog ("local" wrote nothing for hours on 9 October 2026). A
        # target the budget never reached keeps its old last_ok_at and so leads the next run. Reconcile uses
        # last_reconciled_at for the same fairness: mirror order would leave Drive first every hour.
        order_col = ArchiveTarget.last_reconciled_at if for_reconcile else ArchiveTarget.last_ok_at
        q = (
            select(ArchiveTarget)
            .where(ArchiveTarget.enabled.is_(True))
            .order_by(order_col.asc().nulls_first(), ArchiveTarget.name)
        )
        rows = list((await self.db.execute(q)).scalars().all())
        if for_reconcile:
            return rows
        # Local disks first: they take a full slice in a minute or two. Google Drive uploads took the whole
        # 15-minute budget when it led (13 October 2026: 424 Drive objects, 0 local, in one run), so the local
        # copies wrote nothing on every other run. The sort is stable, so least recently completed still leads
        # within each group.
        return sorted(rows, key=lambda t: t.target_type != "local_path")

    # ------------------------------------------------------------------ objects for a judgment
    async def judgment_objects(self, j: Judgment) -> List[Dict[str, Any]]:
        cached = self._objects_cache.get(j.id)
        if cached is not None:
            return cached
        objs = await self._build_judgment_objects(j)
        if len(self._objects_cache) >= OBJECTS_CACHE_MAX:
            self._objects_cache.pop(next(iter(self._objects_cache)))
        self._objects_cache[j.id] = objs
        return objs

    async def _build_judgment_objects(self, j: Judgment) -> List[Dict[str, Any]]:
        prefix = judgment_prefix(j)
        objs: List[Dict[str, Any]] = []
        original: Optional[bytes] = None
        if j.has_original_pdf and j.original_document_id:
            prov = (await self.db.execute(select(SourceProvenance).where(SourceProvenance.id == j.original_document_id))).scalars().first()
            if prov is not None and prov.raw_ref:
                try:
                    original = read_raw(prov.raw_ref)
                except FileNotFoundError:
                    original = None
        if original:
            objs.append({"key": f"{prefix}/original.pdf", "data": original, "kind": "original_pdf", "content_type": "application/pdf"})
        else:
            meta = {"court": j.court_name, "title": j.case_title, "citation": j.canonical_citation, "source_name": j.source_name, "source_url": j.source_url, "content_hash": j.full_text_hash, "rendered_at": (j.promoted_at or j.created_at or datetime(1970, 1, 1, tzinfo=timezone.utc)).isoformat(), "access_method": j.access_method}
            objs.append({"key": f"{prefix}/rendered_copy.pdf", "data": render_judgment_pdf_bytes(meta, j.full_text or ""), "kind": "rendered_copy", "content_type": "application/pdf"})
        objs.append({"key": f"{prefix}/judgment.txt", "data": (j.full_text or "").encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"})
        metadata = judgment_record(j, has_original_pdf=bool(original))
        objs.append({"key": f"{prefix}/metadata.json", "data": json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"), "kind": "text", "content_type": "application/json"})
        return objs

    # ------------------------------------------------------------------ write-once put
    async def _ledger_precheck(self, db: AsyncSession, target_id: UUID, obj: Dict[str, Any]) -> Tuple[Optional[str], str]:
        """If the ledger already records this key, return (status, content_hash) without touching the adapter."""
        key = obj["key"]
        data: bytes = obj["data"]
        h = hashlib.sha256(data).hexdigest()
        ledger = (await db.execute(select(ArchiveObject).where(ArchiveObject.target_id == target_id, ArchiveObject.object_key == key))).scalars().first()
        if ledger is not None and ledger.status == "written":
            if obj["kind"] == "rendered_copy":
                return "exists", h
            if ledger.content_hash != h:
                ledger.status = "mismatch"
                ledger.error = "new content differs from the write-once object"
                await db.flush()
                return "mismatch", h
            return "exists", h
        return None, h

    async def _adapter_store(self, adapter: ArchiveAdapter, obj: Dict[str, Any], *, rate_limiter: Optional[_AdaptiveConcurrency] = None) -> _AdapterStoreOutcome:
        key = obj["key"]
        data: bytes = obj["data"]
        h = hashlib.sha256(data).hexdigest()
        stored_size: Optional[int] = None
        attempts = 0
        rate_sensitive = rate_limiter is not None
        while True:
            try:
                exists = await _retry(asyncio.to_thread, adapter.exists, key, rate_limit_sensitive=rate_sensitive)
                if exists:
                    size = await _retry(asyncio.to_thread, adapter.size, key, rate_limit_sensitive=rate_sensitive)
                    if obj["kind"] == "rendered_copy" and size:
                        stored_size = size
                        status = "written"
                    else:
                        status = "written" if size == len(data) else "mismatch"
                else:
                    await _retry(
                        asyncio.to_thread,
                        adapter.put,
                        key,
                        data,
                        obj.get("content_type", "application/octet-stream"),
                        rate_limit_sensitive=rate_sensitive,
                    )
                    status = "written"
                return _AdapterStoreOutcome(status=status, content_hash=h, byte_size=len(data), stored_size=stored_size)
            except ObjectExists:
                return _AdapterStoreOutcome(status="written", content_hash=h, byte_size=len(data), stored_size=stored_size)
            except Exception as exc:
                if rate_limiter is not None and is_drive_rate_limited(exc) and attempts < 5:
                    attempts += 1
                    rate_limiter.halve()
                    await drive_rate_limit_backoff()
                    continue
                raise

    async def _persist_write(
        self,
        db: AsyncSession,
        target: ArchiveTarget,
        obj: Dict[str, Any],
        outcome: _AdapterStoreOutcome,
        *,
        judgment_id=None,
        prov_id=None,
        access_method: str = "public",
    ) -> str:
        key = obj["key"]
        data: bytes = obj["data"]
        h = outcome.content_hash
        ledger = (await db.execute(select(ArchiveObject).where(ArchiveObject.target_id == target.id, ArchiveObject.object_key == key))).scalars().first()
        status = outcome.status
        stored_size = outcome.stored_size
        if ledger is None:
            ledger = ArchiveObject(
                target_id=target.id,
                object_key=key,
                content_hash=h,
                byte_size=stored_size or len(data),
                document_kind=obj["kind"],
                judgment_id=judgment_id,
                source_provenance_id=prov_id,
                access_method=access_method,
                status=status,
                written_at=datetime.now(timezone.utc),
            )
            db.add(ledger)
        else:
            ledger.status = status
            ledger.error = None
            ledger.written_at = datetime.now(timezone.utc)
            ledger.content_hash = h
            ledger.byte_size = stored_size or len(data)
        if status == "written" and stored_size is None:
            await db.execute(
                update(ArchiveTarget)
                .where(ArchiveTarget.id == target.id)
                .values(
                    objects_written=ArchiveTarget.objects_written + 1,
                    bytes_written=ArchiveTarget.bytes_written + len(data),
                )
            )
        await db.flush()
        return status

    async def _write(self, target: ArchiveTarget, adapter: ArchiveAdapter, obj: Dict[str, Any], *, judgment_id=None, prov_id=None, access_method: str = "public") -> str:
        pre, h = await self._ledger_precheck(self.db, target.id, obj)
        if pre is not None:
            return pre
        try:
            outcome = await self._adapter_store(adapter, obj)
        except Exception as exc:
            err = str(exc)[:2000]
            key = obj["key"]
            data: bytes = obj["data"]
            ledger = (await self.db.execute(select(ArchiveObject).where(ArchiveObject.target_id == target.id, ArchiveObject.object_key == key))).scalars().first()
            if ledger is None:
                ledger = ArchiveObject(
                    target_id=target.id,
                    object_key=key,
                    content_hash=h,
                    byte_size=len(data),
                    document_kind=obj["kind"],
                    judgment_id=judgment_id,
                    source_provenance_id=prov_id,
                    access_method=access_method,
                    status="failed",
                    error=err,
                )
                self.db.add(ledger)
            else:
                ledger.status, ledger.error = "failed", err
            await self.db.flush()
            raise
        return await self._persist_write(self.db, target, obj, outcome, judgment_id=judgment_id, prov_id=prov_id, access_method=access_method)

    async def _write_with_session(
        self,
        session: AsyncSession,
        target_id: UUID,
        adapter: ArchiveAdapter,
        work: _JudgmentUploadWork,
        *,
        rate_limiter: Optional[_AdaptiveConcurrency] = None,
    ) -> str:
        target = (await session.execute(select(ArchiveTarget).where(ArchiveTarget.id == target_id))).scalars().first()
        if target is None:
            raise ArchiveError(f"archive target {target_id} missing")
        pre, _ = await self._ledger_precheck(session, target_id, work.obj)
        if pre is not None:
            return pre
        try:
            outcome = await self._adapter_store(adapter, work.obj, rate_limiter=rate_limiter)
        except Exception as exc:
            err = str(exc)[:2000]
            key = work.obj["key"]
            data: bytes = work.obj["data"]
            h = hashlib.sha256(data).hexdigest()
            ledger = (await session.execute(select(ArchiveObject).where(ArchiveObject.target_id == target_id, ArchiveObject.object_key == key))).scalars().first()
            if ledger is None:
                session.add(
                    ArchiveObject(
                        target_id=target_id,
                        object_key=key,
                        content_hash=h,
                        byte_size=len(data),
                        document_kind=work.obj["kind"],
                        judgment_id=work.judgment_id,
                        source_provenance_id=work.prov_id,
                        access_method=work.access_method,
                        status="failed",
                        error=err,
                    )
                )
            else:
                ledger.status, ledger.error = "failed", err
            await session.flush()
            raise
        return await self._persist_write(
            session,
            target,
            work.obj,
            outcome,
            judgment_id=work.judgment_id,
            prov_id=work.prov_id,
            access_method=work.access_method,
        )

    async def _mirror_drive_uploads(
        self,
        t: ArchiveTarget,
        target_type: str,
        target_cfg: Dict[str, Any],
        judgments: List[Judgment],
        summary: Dict[str, int],
        index_rows: List[List[str]],
        *,
        concurrency: int,
    ) -> int:
        """Parallel Drive uploads with a fixed pool of adapters (httplib2 is not thread-safe)."""
        from scraper.database import SessionLocal

        pool_size = max(1, int(concurrency))
        drive_cfg = dict(target_cfg)
        drive_cfg["_drive_folder_cache"] = drive_cfg.get("_drive_folder_cache") or {"": drive_cfg.get("folder_id") or drive_cfg.get("root_folder_id") or ""}

        pending_work: List[_JudgmentUploadWork] = []
        folder_keys: List[str] = []
        for j in judgments:
            if self._time_up():
                break
            if not self._policy_allows(t, j.access_method):
                summary["skipped_policy"] += 1
                continue
            already = (await self.db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.judgment_id == j.id, ArchiveObject.status == "written"))).scalar() or 0
            if already >= JUDGMENT_OBJECT_COUNT:
                continue
            objs = await self.judgment_objects(j)
            for obj in objs:
                pending_work.append(_JudgmentUploadWork(t.id, j.id, j.source_provenance_id, j.access_method, obj))
                folder_keys.append(obj["key"])
            index_rows.append(
                [
                    j.canonical_citation,
                    j.case_title or "",
                    j.court_name or "",
                    str(j.year or ""),
                    j.decision_date.isoformat() if j.decision_date else "",
                    judgment_prefix(j),
                    "original_pdf" if j.has_original_pdf else "rendered_copy",
                    j.full_text_hash or "",
                ]
            )
            await self._checkpoint()

        failures = 0
        limiter = _AdaptiveConcurrency(pool_size)
        adapter_pool: asyncio.Queue = asyncio.Queue()
        for _ in range(pool_size):
            try:
                adapter_pool.put_nowait(await asyncio.to_thread(self.adapter_factory, target_type, drive_cfg))
            except Exception as exc:
                failures += 1
                summary["failed"] += 1
                t.last_error = f"adapter init: {exc}"[:2000]
                logger.warning("archive %s: drive adapter pool init failed: %s", t.name, exc)
        if adapter_pool.empty():
            return failures

        failed_folders: set[str] = set()
        folder_ensure_lock = asyncio.Lock()
        preflight_cap = int(settings.MIRROR_DRIVE_SERIAL_PREFLIGHT_MAX_FOLDERS)
        if preflight_cap > 0 and folder_keys:
            distinct_keys = _distinct_folder_keys(folder_keys)
            preflight_deadline: Optional[float] = None
            if self.deadline is not None:
                remaining = max(0.0, self.deadline - time.monotonic())
                frac = float(settings.MIRROR_DRIVE_PREFLIGHT_BUDGET_FRACTION)
                if frac > 0 and remaining > 0:
                    preflight_deadline = time.monotonic() + remaining * frac
            setup_adapter = await adapter_pool.get()
            try:
                for key in distinct_keys[:preflight_cap]:
                    if self._time_up():
                        break
                    if preflight_deadline is not None and time.monotonic() >= preflight_deadline:
                        break
                    folder = _drive_folder_path_for_key(key)
                    if folder in failed_folders or _drive_folder_cached(drive_cfg, folder):
                        continue
                    ensure_status = await self._drive_ensure_tree(setup_adapter, key, limiter=limiter)
                    if ensure_status == "timed_out":
                        break
                    if ensure_status == "failed":
                        failed_folders.add(folder)
                        failures += 1
                        summary["failed"] += 1
                        logger.warning("archive %s: ensure_tree failed for %s", t.name, folder)
            finally:
                await adapter_pool.put(setup_adapter)

        failure_lock = asyncio.Lock()
        summary_lock = asyncio.Lock()
        stop = asyncio.Event()

        async def _ensure_folder_for_upload(adapter: ArchiveAdapter, work: _JudgmentUploadWork) -> bool:
            nonlocal failures
            folder = _drive_folder_path_for_key(work.obj["key"])
            if folder in failed_folders:
                return False
            if _drive_folder_cached(drive_cfg, folder):
                return True
            async with folder_ensure_lock:
                if folder in failed_folders:
                    return False
                if _drive_folder_cached(drive_cfg, folder):
                    return True
                ensure_status = await self._drive_ensure_tree(adapter, work.obj["key"], limiter=limiter)
                if ensure_status == "timed_out":
                    return False
                if ensure_status == "failed":
                    failed_folders.add(folder)
                    async with failure_lock:
                        failures += 1
                        summary["failed"] += 1
                    logger.warning("archive %s: ensure_tree failed for %s", t.name, folder)
                    return False
            return True

        async def run_one(work: _JudgmentUploadWork) -> None:
            nonlocal failures
            if stop.is_set() or self._time_up():
                return
            await limiter.acquire()
            adapter = await adapter_pool.get()
            try:
                if self._time_up():
                    stop.set()
                    return
                if not await _ensure_folder_for_upload(adapter, work):
                    return
                async with SessionLocal() as session:
                    try:
                        status = await self._write_with_session(session, t.id, adapter, work, rate_limiter=limiter)
                    except Exception:
                        await session.commit()
                        raise
                    await session.commit()
                async with summary_lock:
                    summary[status if status in summary else "written"] += 1
            except Exception as exc:
                async with failure_lock:
                    failures += 1
                    summary["failed"] += 1
                    if failures >= 5:
                        stop.set()
                logger.warning("archive %s: %s failed: %s", t.name, work.obj["key"], exc)
            finally:
                await adapter_pool.put(adapter)
                await limiter.release()

        tasks = [asyncio.create_task(run_one(work)) for work in pending_work]
        if tasks:
            await asyncio.gather(*tasks)
        return failures

    async def _drive_ensure_tree(
        self,
        adapter: ArchiveAdapter,
        key: str,
        *,
        limiter: _AdaptiveConcurrency,
    ) -> _DriveEnsureTreeResult:
        """Create Drive folders with the same retry/backoff policy as uploads."""
        attempts = 0
        while True:
            if self._time_up():
                return "timed_out"
            try:
                await _retry(asyncio.to_thread, adapter.ensure_tree, key, rate_limit_sensitive=True)
                return "ok"
            except Exception as exc:
                if is_drive_rate_limited(exc) and attempts < 5:
                    attempts += 1
                    limiter.halve()
                    await drive_rate_limit_backoff()
                    continue
                return "failed"

    def _policy_allows(self, target: ArchiveTarget, access_method: str) -> bool:
        if not is_login_session(access_method):
            return True
        return bool(settings.MIRROR_LOGIN_SESSION_ROWS and target.mirror_login_session_rows)

    def _have_subquery(self, t: ArchiveTarget):
        """Per judgment, how many of its objects target `t` holds. A 'mismatch' object counts as present (it
        needs a human or the reconcile, not a retry); 'failed' and 'missing' are retried."""
        return (
            select(ArchiveObject.judgment_id.label("jid"), func.count().label("c"))
            .where(ArchiveObject.target_id == t.id, ArchiveObject.status.in_(["written", "mismatch"]), ArchiveObject.judgment_id.isnot(None))
            .group_by(ArchiveObject.judgment_id)
            .subquery()
        )

    def _policy_clause(self, t: ArchiveTarget):
        """SQL form of _policy_allows: a target that may not hold login-session rows never counts them as
        owed. Filtering in Python after the LIMIT let those rows fill the oldest-first window for good."""
        if self._policy_allows(t, "login_session"):
            return None
        return func.lower(func.coalesce(Judgment.access_method, "")) != "login_session"

    async def backlog_judgments(self, t: ArchiveTarget, limit: int, *, exclude=frozenset(), before: Optional[datetime] = None) -> List[Judgment]:
        """Oldest judgments that lack a full set of objects on target `t` and that the target may hold."""
        if limit <= 0:
            return []
        have = self._have_subquery(t)
        q = select(Judgment).outerjoin(have, have.c.jid == Judgment.id).where(func.coalesce(have.c.c, 0) < JUDGMENT_OBJECT_COUNT)
        policy = self._policy_clause(t)
        if policy is not None:
            q = q.where(policy)
        if exclude:
            q = q.where(Judgment.id.notin_(list(exclude)))
        if before is not None:
            q = q.where(Judgment.promoted_at <= before)
        return list((await self.db.execute(q.order_by(Judgment.promoted_at.asc()).limit(limit))).scalars().all())

    async def lag(self, t: ArchiveTarget) -> Dict[str, Any]:
        """How far behind the corpus this target is (judgments it may hold)."""
        policy = self._policy_clause(t)
        total_q = select(func.count()).select_from(Judgment)
        have = self._have_subquery(t)
        unmirrored_q = select(func.count()).select_from(Judgment).outerjoin(have, have.c.jid == Judgment.id).where(func.coalesce(have.c.c, 0) < JUDGMENT_OBJECT_COUNT)
        if policy is not None:
            total_q, unmirrored_q = total_q.where(policy), unmirrored_q.where(policy)
        total = int((await self.db.execute(total_q)).scalar() or 0)
        unmirrored = int((await self.db.execute(unmirrored_q)).scalar() or 0)
        # progress means judgments reaching the target: index, statute and instrument writes do not count
        last_written = (
            await self.db.execute(
                select(func.max(ArchiveObject.written_at)).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written", ArchiveObject.judgment_id.is_not(None))
            )
        ).scalar()
        return {"judgments_total": total, "judgments_unmirrored": unmirrored, "last_written_at": last_written.isoformat() if last_written else None, "_last_written": last_written}

    async def alert_on_lag(self) -> List[str]:
        """ARCHIVE_MIRROR_LAG once per target while it has unmirrored judgments and has written nothing for
        MIRROR_LAG_ALERT_HOURS; acknowledged again when writes resume or the target catches up."""
        from datetime import timedelta

        from scraper.models import Notification

        raised: List[str] = []
        now = datetime.now(timezone.utc)
        for t in await self.targets():
            lag = await self.lag(t)
            last = lag.pop("_last_written")
            allowed = float(settings.MIRROR_LAG_ALERT_HOURS) + (float(settings.ARCHIVE_BATCH_MAX_WAIT_HOURS) if self._batched(t) else 0.0)
            if self._batched(t):
                waiting = await self.batch_waiting(t)
                oldest = waiting["oldest"]
                if oldest is not None and oldest.tzinfo is None:
                    oldest = oldest.replace(tzinfo=timezone.utc)
                backlog_stale = oldest is not None and (now - oldest) > timedelta(hours=allowed)
                if last is not None and last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                write_stale = last is None or (now - last) > timedelta(hours=float(settings.MIRROR_LAG_ALERT_HOURS))
                stale = backlog_stale and write_stale
            else:
                stale = last is None or (now - last) > timedelta(hours=allowed)
            problem = lag["judgments_unmirrored"] > 0 and stale
            code_msg = f"ARCHIVE_MIRROR_LAG:{t.name}"
            open_alert = (
                await self.db.execute(select(Notification).where(Notification.code == "ARCHIVE_MIRROR_LAG", Notification.source_name == t.name, Notification.acknowledged.is_(False)).limit(1))
            ).scalars().first()
            if problem and open_alert is None:
                await notify(self.db, level="warning", code="ARCHIVE_MIRROR_LAG", source_name=t.name, message=f"{t.name}: {lag['judgments_unmirrored']} of {lag['judgments_total']} judgments not mirrored and nothing written since {lag['last_written_at']}", details=lag)
                raised.append(code_msg)
            elif not problem and open_alert is not None:
                open_alert.acknowledged = True
        await self.db.flush()
        return raised

    # ------------------------------------------------------------------ batch transfer
    # Google Drive receives judgments in batches of about ARCHIVE_BATCH_BYTES instead of file by file. A batch
    # is a snapshot: it opens with a cut-off time, takes only what was waiting at that moment (newer records
    # wait for the next batch, or the stream would never end) and closes once the snapshot is on the target.
    # The cut-off lives in Redis; if it is lost, the target simply waits for the next threshold.
    def _batched(self, t: ArchiveTarget) -> bool:
        types = {x.strip() for x in (settings.ARCHIVE_BATCH_TARGET_TYPES or "").split(",") if x.strip()}
        return int(settings.ARCHIVE_BATCH_BYTES or 0) > 0 and t.target_type in types

    @staticmethod
    def _cutoff_key(t: ArchiveTarget) -> str:
        return f"corpus:archive_batch_cutoff:{t.id}"

    def _get_cutoff(self, t: ArchiveTarget) -> Optional[datetime]:
        try:
            from scraper.tasks.chain import _redis

            raw = _redis().get(self._cutoff_key(t))
            return datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else raw) if raw else None
        except Exception as exc:
            logger.warning("archive batch: cut-off for %s unreadable: %s", t.name, exc)
            return None

    def _set_cutoff(self, t: ArchiveTarget, cutoff: Optional[datetime]) -> None:
        try:
            from scraper.tasks.chain import _redis

            if cutoff is None:
                _redis().delete(self._cutoff_key(t))
            else:
                _redis().set(self._cutoff_key(t), cutoff.isoformat(), ex=7 * 86400)
        except Exception as exc:
            logger.warning("archive batch: cut-off for %s not stored: %s", t.name, exc)

    async def batch_waiting(self, t: ArchiveTarget, *, before: Optional[datetime] = None) -> Dict[str, Any]:
        """Judgments the target still lacks: how many, roughly how many bytes, and how long the oldest has waited."""
        from scraper.models import SourceProvenance as Prov

        have = self._have_subquery(t)
        size = func.coalesce(Prov.byte_size, 0) + 2 * func.coalesce(func.octet_length(Judgment.full_text), 0)  # PDF (or rendered copy) + text
        q = (
            select(func.count(), func.coalesce(func.sum(size), 0), func.min(Judgment.promoted_at))
            .select_from(Judgment)
            .outerjoin(have, have.c.jid == Judgment.id)
            .outerjoin(Prov, Prov.id == Judgment.original_document_id)
            .where(func.coalesce(have.c.c, 0) < JUDGMENT_OBJECT_COUNT)
        )
        policy = self._policy_clause(t)
        if policy is not None:
            q = q.where(policy)
        if before is not None:
            q = q.where(Judgment.promoted_at <= before)
        count, nbytes, oldest = (await self.db.execute(q)).one()
        return {"judgments": int(count or 0), "bytes": int(nbytes or 0), "oldest": oldest}

    async def batch_gate(self, t: ArchiveTarget, *, now: Optional[datetime] = None) -> tuple:
        """(go, cutoff). go=False: the target waits for its next batch. cutoff: only judgments promoted
        up to it belong to the open batch (None for a target that is not batched)."""
        if not self._batched(t):
            return True, None
        now = now or datetime.now(timezone.utc)
        cutoff = self._get_cutoff(t)
        if cutoff is not None:
            if (await self.batch_waiting(t, before=cutoff))["judgments"] > 0:
                return True, cutoff
            self._set_cutoff(t, None)  # the snapshot is on the target: the batch is closed
            logger.info("archive batch for %s complete", t.name)
        waiting = await self.batch_waiting(t)
        oldest = waiting["oldest"]
        if oldest is not None and oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=timezone.utc)
        overdue = oldest is not None and now - oldest >= timedelta(hours=float(settings.ARCHIVE_BATCH_MAX_WAIT_HOURS))
        info = {"judgments": waiting["judgments"], "mb": round(waiting["bytes"] / 1024**2, 1), "threshold_mb": round(int(settings.ARCHIVE_BATCH_BYTES) / 1024**2, 1)}
        if waiting["judgments"] and (waiting["bytes"] >= int(settings.ARCHIVE_BATCH_BYTES) or overdue):
            self._set_cutoff(t, now)
            logger.info("archive batch for %s opened: %s judgments, %s MB%s", t.name, info["judgments"], info["mb"], " (waited the maximum)" if overdue else "")
            return True, now
        self.summary.setdefault(t.name, {})["batch_waiting"] = info
        return False, None

    # ------------------------------------------------------------------ mirror run
    async def mirror_pending(self, limit: int = 200, *, backlog_limit: Optional[int] = None) -> Dict[str, Any]:
        targets = await self.targets()
        if not targets:
            return {"targets": 0, "note": "no archive targets configured"}
        newest = (await self.db.execute(select(Judgment).order_by(Judgment.promoted_at.desc()).limit(limit))).scalars().all()
        index_rows: List[List[str]] = []
        for t in targets:
            summary = self.summary.setdefault(t.name, {"written": 0, "exists": 0, "failed": 0, "skipped_policy": 0, "mismatch": 0, "backlog": 0})
            go, cutoff = await self.batch_gate(t)
            self._gate[t.id] = go
            if not go:
                continue  # this target waits for its next batch
            # The newest `limit` judgments were the only ones ever considered, so once promotion moved on the
            # older ones were never mirrored (13.8k of 19.7k on Drive). Every run now also takes the OLDEST
            # judgments this target still lacks, so the backlog always shrinks.
            # One row past the slice says whether more is owed after this run, without a second count query.
            backlog_quota = int(settings.MIRROR_BACKLOG_PER_RUN if backlog_limit is None else backlog_limit)
            backlog = await self.backlog_judgments(t, backlog_quota + 1 if backlog_quota > 0 else 0, exclude={j.id for j in newest}, before=cutoff)
            if len(backlog) > backlog_quota:
                self.more_pending = True
                backlog = backlog[:backlog_quota]
            judgments = [j for j in newest if cutoff is None or _aware(j.promoted_at) <= cutoff] + backlog
            summary["backlog"] += len(backlog)
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                t.consecutive_failures += 1
                t.last_error = f"adapter init: {exc}"[:2000]
                summary["failed"] += 1
                await notify(self.db, level="error", code="ARCHIVE_TARGET_FAILED", message=f"{t.name} ({t.target_type}): {exc}", details={"target": t.name})
                await self.db.flush()
                continue
            failures = 0
            target_timed_out = False
            touched = False
            drive_concurrency = min(16, max(1, int(settings.MIRROR_DRIVE_CONCURRENCY))) if t.target_type == "google_drive" else 1
            if t.target_type == "google_drive" and drive_concurrency > 1:
                touched = bool(judgments)
                failures = await self._mirror_drive_uploads(
                    t,
                    t.target_type,
                    target_config(t),
                    judgments,
                    summary,
                    index_rows,
                    concurrency=drive_concurrency,
                )
                await self.db.refresh(t)
                target_timed_out = self.out_of_time
            else:
                for j in judgments:
                    if self._time_up():
                        target_timed_out = True
                        break
                    touched = True
                    if not self._policy_allows(t, j.access_method):
                        summary["skipped_policy"] += 1
                        continue
                    already = (await self.db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.judgment_id == j.id, ArchiveObject.status == "written"))).scalar() or 0
                    if already >= JUDGMENT_OBJECT_COUNT:
                        continue  # complete on this target: skip the PDF render entirely
                    objs = await self.judgment_objects(j)
                    for obj in objs:
                        try:
                            status = await self._write(t, adapter, obj, judgment_id=j.id, prov_id=j.source_provenance_id, access_method=j.access_method)
                            summary[status if status in summary else "written"] += 1
                        except Exception as exc:
                            failures += 1
                            summary["failed"] += 1
                            logger.warning("archive %s: %s failed: %s", t.name, obj["key"], exc)
                            if failures >= 5:
                                break
                    if failures >= 5:
                        break
                    index_rows.append([j.canonical_citation, j.case_title or "", j.court_name or "", str(j.year or ""), j.decision_date.isoformat() if j.decision_date else "", judgment_prefix(j), "original_pdf" if j.has_original_pdf else "rendered_copy", j.full_text_hash or ""])
                    await self._checkpoint()
            # _index CSV snapshot (write-once: timestamped file name)
            if index_rows:
                buf = io.StringIO()
                w = csv.writer(buf)
                w.writerow(["canonical_citation", "case_title", "court", "year", "decision_date", "folder", "document_kind", "full_text_sha256_canonical"])
                w.writerows(index_rows)
                stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
                try:
                    st = await self._write(t, adapter, {"key": f"_index/judgments-{stamp}.csv", "data": buf.getvalue().encode("utf-8"), "kind": "index_csv", "content_type": "text/csv"})
                    summary[st if st in summary else "written"] += 1
                except Exception as exc:
                    failures += 1
                    logger.warning("archive %s: index write failed: %s", t.name, exc)
            if failures:
                t.consecutive_failures += 1
                t.last_error = f"{failures} object write(s) failed"
                await notify(self.db, level="error", code="ARCHIVE_TARGET_FAILED", message=f"{t.name}: {failures} object write(s) failed", details={"target": t.name})
            elif target_timed_out and not touched:
                pass
            else:
                t.consecutive_failures = 0
                t.last_error = None
                t.last_ok_at = datetime.now(timezone.utc)
            await self.db.flush()
            await self._checkpoint()
            index_rows = []
        return {"targets": len(targets), "summary": self.summary}

    @staticmethod
    def _statute_key(jurisdiction: Optional[str], name: Optional[str], section_number: Optional[str], version_no: Any) -> str:
        return f"Statutes/{slug(jurisdiction or 'Federal', 30)}/{slug(name)}/{slug(section_number, 40)}/v{version_no}.txt"

    @staticmethod
    def _instrument_key(inst_id: Any, date_: Any, title: Optional[str], number: Optional[str], full_text_hash: Optional[str]) -> str:
        year = date_.year if date_ else "undated"
        return f"Instruments/{year}/{slug(title or number or str(inst_id), 100)}_{full_text_hash[:10] if full_text_hash else str(inst_id)[:8]}.txt"

    async def mirror_statutes(self, limit: int = 500) -> Dict[str, Any]:
        targets = await self.targets()
        # Key columns only: the section text is fetched for the (at most `limit`) versions a target lacks.
        key_rows = (
            await self.db.execute(
                select(StatuteSectionVersion.id, Statute.jurisdiction, Statute.name, StatuteSection.section_number, StatuteSectionVersion.version_no)
                .join(StatuteSection, StatuteSection.id == StatuteSectionVersion.section_id)
                .join(Statute, Statute.id == StatuteSection.statute_id)
                .order_by(StatuteSectionVersion.created_at.desc())
            )
        ).all()
        out: Dict[str, int] = {}
        for t in targets:
            if self._time_up():
                break
            if self._gate.get(t.id) is False:
                continue  # batched target between batches
            done = await self._written_keys(t, "Statutes/")
            pending = [(r[0], key) for r in key_rows if (key := self._statute_key(r[1], r[2], r[3], r[4])) not in done][:limit]
            if not pending:
                out[t.name] = 0
                continue
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                t.last_error = f"adapter init: {exc}"[:2000]
                continue
            bodies = {
                vid: (text_, prov)
                for vid, text_, prov in (
                    await self.db.execute(
                        select(StatuteSectionVersion.id, StatuteSectionVersion.section_text, StatuteSectionVersion.source_provenance_id).where(StatuteSectionVersion.id.in_([vid for vid, _ in pending]))
                    )
                ).all()
            }
            n = 0
            for i, (vid, key) in enumerate(pending, 1):
                if self._time_up():
                    break
                text_, prov = bodies.get(vid, ("", None))
                try:
                    status = await self._write(t, adapter, {"key": key, "data": (text_ or "").encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"}, prov_id=prov)
                    n += status == "written"
                except Exception as exc:
                    logger.warning("archive %s: %s failed: %s", t.name, key, exc)
                if i % 50 == 0:
                    await self._checkpoint()
            await self._checkpoint()
            out[t.name] = n
        return out

    async def _written_keys(self, t: ArchiveTarget, prefix: str) -> set:
        rows = (await self.db.execute(select(ArchiveObject.object_key).where(ArchiveObject.target_id == t.id, ArchiveObject.status.in_(["written", "mismatch"]), ArchiveObject.object_key.like(prefix + "%")))).scalars().all()
        return set(rows)

    async def mirror_instruments(self, limit: int = 500) -> Dict[str, Any]:
        from scraper.models import Instrument

        targets = await self.targets()
        key_rows = (
            await self.db.execute(select(Instrument.id, Instrument.date, Instrument.title, Instrument.number, Instrument.full_text_hash).order_by(Instrument.created_at.desc()))
        ).all()
        out: Dict[str, int] = {}
        for t in targets:
            if self._time_up():
                break
            if self._gate.get(t.id) is False:
                continue  # batched target between batches
            done = await self._written_keys(t, "Instruments/")
            pending = [(r[0], key) for r in key_rows if (key := self._instrument_key(*r)) not in done][:limit]
            if not pending:
                out[t.name] = 0
                continue
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                t.last_error = f"adapter init: {exc}"[:2000]
                continue
            bodies = {
                iid: (text_, prov)
                for iid, text_, prov in (
                    await self.db.execute(select(Instrument.id, Instrument.full_text, Instrument.source_provenance_id).where(Instrument.id.in_([iid for iid, _ in pending])))
                ).all()
            }
            n = 0
            for i, (iid, key) in enumerate(pending, 1):
                if self._time_up():
                    break
                text_, prov = bodies.get(iid, ("", None))
                try:
                    status = await self._write(t, adapter, {"key": key, "data": (text_ or "").encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"}, prov_id=prov)
                    n += status == "written"
                except Exception as exc:
                    logger.warning("archive %s: %s failed: %s", t.name, key, exc)
                if i % 50 == 0:
                    await self._checkpoint()
            await self._checkpoint()
            out[t.name] = n
        return out

    async def test_target(self, t: ArchiveTarget) -> Dict[str, Any]:
        """Operator 'test' control: instantiate the adapter and list the index folder; nothing is written."""
        try:
            adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            return await asyncio.to_thread(adapter.check)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    # ------------------------------------------------------------------ reconcile
    async def reconcile(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """Check stored objects against each target, least recently verified first.

        One pass over every object asked Drive about ~150,000 files inside a single transaction: Postgres ended
        it at the idle-in-transaction limit before it finished, and while it ran it held the target row the
        mirror updates, so Drive copies stopped (11 October 2026). With `limit`, a deadline and commit_progress
        a run checks a bounded slice and commits as it goes; the next run carries on from the oldest checks."""
        report: Dict[str, Any] = {}
        for t in await self.targets(for_reconcile=True):
            rep = {"checked": 0, "ok": 0, "missing": 0, "mismatch": 0, "error": None}
            if self._time_up():
                report[t.name] = rep
                continue
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                rep["error"] = str(exc)[:500]
                report[t.name] = rep
                continue
            q = (
                select(ArchiveObject)
                .where(ArchiveObject.target_id == t.id, ArchiveObject.status.in_(["written", "missing", "mismatch"]))
                .order_by(ArchiveObject.verified_at.asc().nulls_first(), ArchiveObject.id)
            )
            if limit is not None:
                q = q.limit(limit)
            objs = (await self.db.execute(q)).scalars().all()
            for i, o in enumerate(objs, 1):
                if self._time_up():
                    break
                if i % 50 == 0:
                    await self._checkpoint()
                rep["checked"] += 1
                try:
                    size = await asyncio.to_thread(adapter.size, o.object_key)
                except Exception as exc:
                    rep["error"] = str(exc)[:500]
                    break
                # verified_at is when the object was last checked, whatever the answer: a missing object keeps its
                # status but moves to the back of the queue instead of leading every slice.
                o.verified_at = datetime.now(timezone.utc)
                if size is None:
                    o.status = "missing"
                    rep["missing"] += 1
                elif size != o.byte_size:
                    o.status = "mismatch"
                    rep["mismatch"] += 1
                else:
                    o.status = "written"
                    rep["ok"] += 1
            t.last_reconciled_at = datetime.now(timezone.utc)
            if rep["missing"] or rep["mismatch"]:
                await notify(self.db, level="warning", code="ARCHIVE_RECONCILE", message=f"{t.name}: {rep['missing']} missing, {rep['mismatch']} mismatched of {rep['checked']}", details=rep)
            report[t.name] = rep
            await self.db.flush()
            await self._checkpoint()
        return report


async def archive_status(db: AsyncSession) -> List[Dict[str, Any]]:
    out = []
    for t in (await db.execute(select(ArchiveTarget))).scalars().all():
        written = (await db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written"))).scalar() or 0
        failed = (await db.execute(select(func.count()).select_from(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "failed"))).scalar() or 0
        out.append(
            {
                "name": t.name,
                "type": t.target_type,
                "enabled": t.enabled,
                "root": t.root_path or ("CONFIGURED" if t.config_encrypted else "NOT CONFIGURED"),
                "mirror_login_session_rows": t.mirror_login_session_rows and settings.MIRROR_LOGIN_SESSION_ROWS,
                "objects_written": written,
                "objects_failed": failed,
                "bytes_written": t.bytes_written,
                "last_ok_at": t.last_ok_at.isoformat() if t.last_ok_at else None,
                "last_error": t.last_error,
                "consecutive_failures": t.consecutive_failures,
                "last_reconciled_at": t.last_reconciled_at.isoformat() if t.last_reconciled_at else None,
            }
        )
    return out
