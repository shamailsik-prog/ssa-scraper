"""
Archive mirror and reconcile_storage (Section 9 / 9A; Amendment §12, §19).

* The archive is a MIRROR. PostgreSQL stays the working database; the application never reads
  the archive.
* Folder tree (created by the service):
      Judgments/<court_code>/<year>/<citation_slug>/original.pdf        (genuine source PDF)
      Judgments/<court_code>/<year>/<citation_slug>/rendered_copy.pdf   (HTML-only judgments, labelled)
      Judgments/<court_code>/<year>/<citation_slug>/judgment.txt
      Judgments/<court_code>/<year>/<citation_slug>/metadata.json
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
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.fetchers import read_raw
from scraper.models import ArchiveObject, ArchiveTarget, Judgment, SourceProvenance, Statute, StatuteSection, StatuteSectionVersion
from scraper.notify import notify
from scraper.parsers.pdf_writer import RENDERED_COPY_LABEL, render_judgment_pdf_bytes
from scraper.security import is_login_session
from scraper.storage.adapters import ArchiveAdapter, ArchiveError, ObjectExists, build_adapter

logger = logging.getLogger(__name__)


def slug(s: Optional[str], limit: int = 80) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", s or "").strip("_")
    return (s or "unknown")[:limit]


def judgment_prefix(j: Judgment) -> str:
    """Citations/<reporter>/<year>/<citation> for reported judgments; Unreported/<court>/<year>/<citation> otherwise."""
    if j.reporter and j.year:
        return f"Citations/{slug(j.reporter, 20)}/{j.year}/{slug(j.canonical_citation)}"
    court = slug(j.court_name or "Unknown_Court", 40)
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


async def _retry(fn, *args):
    """Run an adapter call, retrying transient failures ('The read operation timed out' from Drive)."""
    for delay in (*RETRY_DELAYS, None):
        try:
            return await fn(*args)
        except ObjectExists:
            raise
        except Exception:
            if delay is None:
                raise
            await asyncio.sleep(delay)


class ArchiveMirror:
    def __init__(self, db: AsyncSession, *, adapter_factory=build_adapter):
        self.db = db
        self.adapter_factory = adapter_factory
        self.summary: Dict[str, Dict[str, int]] = {}

    async def targets(self) -> List[ArchiveTarget]:
        return list((await self.db.execute(select(ArchiveTarget).where(ArchiveTarget.enabled.is_(True)))).scalars().all())

    # ------------------------------------------------------------------ objects for a judgment
    async def judgment_objects(self, j: Judgment) -> List[Dict[str, Any]]:
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
        metadata = {
            "canonical_citation": j.canonical_citation,
            "case_title": j.case_title,
            "court": j.court_name,
            "judge_names": j.judge_names,
            "bench_size": j.bench_size,
            "bench_type": j.bench_type,
            "decision_date": j.decision_date.isoformat() if j.decision_date else None,
            "year": j.year,
            "source_name": j.source_name,
            "source_url": j.source_url,
            "access_method": j.access_method,
            "full_text_sha256_canonical": j.full_text_hash,
            "has_original_pdf": bool(original),
            "document_label": "ORIGINAL SOURCE PDF" if original else RENDERED_COPY_LABEL,
            "judgment_id": str(j.id),
        }
        objs.append({"key": f"{prefix}/metadata.json", "data": json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"), "kind": "text", "content_type": "application/json"})
        return objs

    # ------------------------------------------------------------------ write-once put
    async def _write(self, target: ArchiveTarget, adapter: ArchiveAdapter, obj: Dict[str, Any], *, judgment_id=None, prov_id=None, access_method: str = "public") -> str:
        key = obj["key"]
        data: bytes = obj["data"]
        h = hashlib.sha256(data).hexdigest()
        stored_size: Optional[int] = None
        ledger = (await self.db.execute(select(ArchiveObject).where(ArchiveObject.target_id == target.id, ArchiveObject.object_key == key))).scalars().first()
        if ledger is not None and ledger.status == "written":
            if obj["kind"] == "rendered_copy":
                # A rendered copy is derived from the preserved text and, before the render was made
                # deterministic, embedded its render time: the same judgment hashed differently on every run
                # and 7,000 healthy objects were flagged 'mismatch'. The key is the identity; reconcile
                # verifies the stored size.
                return "exists"
            if ledger.content_hash != h:
                ledger.status = "mismatch"
                ledger.error = "new content differs from the write-once object"
                return "mismatch"
            return "exists"
        try:
            exists = await _retry(asyncio.to_thread, adapter.exists, key)
            if exists:
                size = await _retry(asyncio.to_thread, adapter.size, key)
                if obj["kind"] == "rendered_copy" and size:
                    # derived copy already stored: adopt the stored object, never compare it with a re-render
                    stored_size = size
                    status = "written"
                else:
                    status = "written" if size == len(data) else "mismatch"
            else:
                await _retry(asyncio.to_thread, adapter.put, key, data, obj.get("content_type", "application/octet-stream"))
                status = "written"
        except ObjectExists:
            status = "written"
        except Exception as exc:
            status = "failed"
            err = str(exc)[:2000]
            if ledger is None:
                ledger = ArchiveObject(target_id=target.id, object_key=key, content_hash=h, byte_size=len(data), document_kind=obj["kind"], judgment_id=judgment_id, source_provenance_id=prov_id, access_method=access_method, status="failed", error=err)
                self.db.add(ledger)
            else:
                ledger.status, ledger.error = "failed", err
            await self.db.flush()
            raise
        if ledger is None:
            ledger = ArchiveObject(target_id=target.id, object_key=key, content_hash=h, byte_size=stored_size or len(data), document_kind=obj["kind"], judgment_id=judgment_id, source_provenance_id=prov_id, access_method=access_method, status=status, written_at=datetime.now(timezone.utc))
            self.db.add(ledger)
        else:
            ledger.status = status
            ledger.error = None
            ledger.written_at = datetime.now(timezone.utc)
            ledger.content_hash = h
            ledger.byte_size = stored_size or len(data)
        if status == "written" and stored_size is None:
            target.objects_written += 1
            target.bytes_written += len(data)
        await self.db.flush()
        return status

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

    async def backlog_judgments(self, t: ArchiveTarget, limit: int, *, exclude=frozenset()) -> List[Judgment]:
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
        last_written = (await self.db.execute(select(func.max(ArchiveObject.written_at)).where(ArchiveObject.target_id == t.id, ArchiveObject.status == "written"))).scalar()
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
            stale = last is None or (now - last) > timedelta(hours=float(settings.MIRROR_LAG_ALERT_HOURS))
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

    # ------------------------------------------------------------------ mirror run
    async def mirror_pending(self, limit: int = 200, *, backlog_limit: Optional[int] = None) -> Dict[str, Any]:
        targets = await self.targets()
        if not targets:
            return {"targets": 0, "note": "no archive targets configured"}
        newest = (await self.db.execute(select(Judgment).order_by(Judgment.promoted_at.desc()).limit(limit))).scalars().all()
        index_rows: List[List[str]] = []
        for t in targets:
            summary = self.summary.setdefault(t.name, {"written": 0, "exists": 0, "failed": 0, "skipped_policy": 0, "mismatch": 0, "backlog": 0})
            # The newest `limit` judgments were the only ones ever considered, so once promotion moved on the
            # older ones were never mirrored (13.8k of 19.7k on Drive). Every run now also takes the OLDEST
            # judgments this target still lacks, so the backlog always shrinks.
            backlog = await self.backlog_judgments(t, settings.MIRROR_BACKLOG_PER_RUN if backlog_limit is None else backlog_limit, exclude={j.id for j in newest})
            judgments = list(newest) + backlog
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
            for j in judgments:
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
            else:
                t.consecutive_failures = 0
                t.last_error = None
                t.last_ok_at = datetime.now(timezone.utc)
            await self.db.flush()
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
            for vid, key in pending:
                text_, prov = bodies.get(vid, ("", None))
                try:
                    status = await self._write(t, adapter, {"key": key, "data": (text_ or "").encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"}, prov_id=prov)
                    n += status == "written"
                except Exception as exc:
                    logger.warning("archive %s: %s failed: %s", t.name, key, exc)
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
            for iid, key in pending:
                text_, prov = bodies.get(iid, ("", None))
                try:
                    status = await self._write(t, adapter, {"key": key, "data": (text_ or "").encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"}, prov_id=prov)
                    n += status == "written"
                except Exception as exc:
                    logger.warning("archive %s: %s failed: %s", t.name, key, exc)
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
    async def reconcile(self) -> Dict[str, Any]:
        report: Dict[str, Any] = {}
        for t in await self.targets():
            rep = {"checked": 0, "ok": 0, "missing": 0, "mismatch": 0, "error": None}
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                rep["error"] = str(exc)[:500]
                report[t.name] = rep
                continue
            objs = (await self.db.execute(select(ArchiveObject).where(ArchiveObject.target_id == t.id, ArchiveObject.status.in_(["written", "missing", "mismatch"])))).scalars().all()
            for o in objs:
                rep["checked"] += 1
                try:
                    size = await asyncio.to_thread(adapter.size, o.object_key)
                except Exception as exc:
                    rep["error"] = str(exc)[:500]
                    break
                if size is None:
                    o.status = "missing"
                    rep["missing"] += 1
                elif size != o.byte_size:
                    o.status = "mismatch"
                    rep["mismatch"] += 1
                else:
                    o.status = "written"
                    o.verified_at = datetime.now(timezone.utc)
                    rep["ok"] += 1
            t.last_reconciled_at = datetime.now(timezone.utc)
            if rep["missing"] or rep["mismatch"]:
                await notify(self.db, level="warning", code="ARCHIVE_RECONCILE", message=f"{t.name}: {rep['missing']} missing, {rep['mismatch']} mismatched of {rep['checked']}", details=rep)
            report[t.name] = rep
            await self.db.flush()
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
