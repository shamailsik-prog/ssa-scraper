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
    court = slug(j.court_name or "Unknown_Court", 40)
    return f"Judgments/{court}/{j.year or 'unknown'}/{slug(j.canonical_citation)}"


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
            meta = {"court": j.court_name, "title": j.case_title, "citation": j.canonical_citation, "source_name": j.source_name, "source_url": j.source_url, "content_hash": j.full_text_hash, "rendered_at": datetime.now(timezone.utc).isoformat(), "access_method": j.access_method}
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
        ledger = (await self.db.execute(select(ArchiveObject).where(ArchiveObject.target_id == target.id, ArchiveObject.object_key == key))).scalars().first()
        if ledger is not None and ledger.status == "written":
            if ledger.content_hash != h:
                ledger.status = "mismatch"
                ledger.error = "new content differs from the write-once object"
                return "mismatch"
            return "exists"
        try:
            exists = await asyncio.to_thread(adapter.exists, key)
            if exists:
                size = await asyncio.to_thread(adapter.size, key)
                status = "written" if size == len(data) else "mismatch"
            else:
                await asyncio.to_thread(adapter.put, key, data, obj.get("content_type", "application/octet-stream"))
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
            ledger = ArchiveObject(target_id=target.id, object_key=key, content_hash=h, byte_size=len(data), document_kind=obj["kind"], judgment_id=judgment_id, source_provenance_id=prov_id, access_method=access_method, status=status, written_at=datetime.now(timezone.utc))
            self.db.add(ledger)
        else:
            ledger.status = status
            ledger.error = None
            ledger.written_at = datetime.now(timezone.utc)
            ledger.content_hash = h
            ledger.byte_size = len(data)
        if status == "written":
            target.objects_written += 1
            target.bytes_written += len(data)
        await self.db.flush()
        return status

    def _policy_allows(self, target: ArchiveTarget, access_method: str) -> bool:
        if not is_login_session(access_method):
            return True
        return bool(settings.MIRROR_LOGIN_SESSION_ROWS and target.mirror_login_session_rows)

    # ------------------------------------------------------------------ mirror run
    async def mirror_pending(self, limit: int = 200) -> Dict[str, Any]:
        targets = await self.targets()
        if not targets:
            return {"targets": 0, "note": "no archive targets configured"}
        judgments = (await self.db.execute(select(Judgment).order_by(Judgment.promoted_at.desc()).limit(limit))).scalars().all()
        index_rows: List[List[str]] = []
        for t in targets:
            summary = self.summary.setdefault(t.name, {"written": 0, "exists": 0, "failed": 0, "skipped_policy": 0, "mismatch": 0})
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
                objs = await self.judgment_objects(j)
                if already >= len(objs):
                    continue
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

    async def mirror_statutes(self, limit: int = 500) -> Dict[str, Any]:
        targets = await self.targets()
        rows = (await self.db.execute(select(StatuteSectionVersion, StatuteSection, Statute).join(StatuteSection, StatuteSection.id == StatuteSectionVersion.section_id).join(Statute, Statute.id == StatuteSection.statute_id).order_by(StatuteSectionVersion.created_at.desc()).limit(limit))).all()
        out: Dict[str, int] = {}
        for t in targets:
            try:
                adapter = await asyncio.to_thread(self.adapter_factory, t.target_type, target_config(t))
            except Exception as exc:
                t.last_error = f"adapter init: {exc}"[:2000]
                continue
            n = 0
            for ver, sec, st in rows:
                key = f"Statutes/{slug(st.name)}/{slug(sec.section_number, 40)}/v{ver.version_no}.txt"
                try:
                    status = await self._write(t, adapter, {"key": key, "data": ver.section_text.encode("utf-8"), "kind": "text", "content_type": "text/plain; charset=utf-8"}, prov_id=ver.source_provenance_id)
                    n += status == "written"
                except Exception as exc:
                    logger.warning("archive %s: %s failed: %s", t.name, key, exc)
            out[t.name] = n
        return out

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
