"""
/export — CSV / JSONL export of the contract tables.

Guard (Cursor command §5): full_text of login_session rows is excluded unless an admin
confirms with both the admin key and the header X-Admin-Confirm: export-login-session-full-text
(and the deployment flag EXPORT_LOGIN_SESSION_FULL_TEXT permits it).
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, Iterator, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scraper.config import settings
from scraper.database import get_db
from scraper.models import Judgment, Statute, StatuteSection, StatuteSectionVersion
from scraper.routers.auth import _ok
from scraper.security import is_login_session

router = APIRouter(prefix="/export", tags=["export"])
CONFIRM_PHRASE = "export-login-session-full-text"


def _login_full_text_allowed(x_api_key: Optional[str], x_admin_confirm: Optional[str]) -> bool:
    return bool(settings.EXPORT_LOGIN_SESSION_FULL_TEXT and _ok(x_api_key) and x_admin_confirm == CONFIRM_PHRASE)


def _judgment_row(j: Judgment, include_full_text: bool, login_ok: bool) -> Dict[str, Any]:
    ft: Optional[str]
    if not include_full_text:
        ft = None
    elif is_login_session(j.access_method) and not login_ok:
        ft = "[EXCLUDED: login_session full_text requires admin confirmation]"
    else:
        ft = j.full_text
    return {
        "canonical_citation": j.canonical_citation,
        "case_title": j.case_title,
        "court": j.court_name,
        "year": j.year,
        "decision_date": j.decision_date.isoformat() if j.decision_date else None,
        "bench_size": j.bench_size,
        "bench_type": j.bench_type,
        "judge_names": j.judge_names,
        "access_method": j.access_method,
        "source_name": j.source_name,
        "has_original_pdf": j.has_original_pdf,
        "full_text_hash": j.full_text_hash,
        "full_text": ft,
    }


@router.get("/judgments")
async def export_judgments(
    format: str = Query(default="csv", pattern="^(csv|jsonl)$"),
    include_full_text: bool = False,
    limit: int = Query(default=10000, le=100000),
    x_api_key: Optional[str] = Header(default=None),
    x_admin_confirm: Optional[str] = Header(default=None),
    db: AsyncSession = Depends(get_db),
):
    login_ok = _login_full_text_allowed(x_api_key, x_admin_confirm)
    rows = (await db.execute(select(Judgment).order_by(Judgment.year.desc(), Judgment.canonical_citation).limit(limit))).scalars().all()
    records = [_judgment_row(j, include_full_text, login_ok) for j in rows]
    if format == "jsonl":
        def gen() -> Iterator[str]:
            for r in records:
                yield json.dumps(r, ensure_ascii=False, default=str) + "\n"

        return StreamingResponse(gen(), media_type="application/x-ndjson")
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(records[0].keys()) if records else ["canonical_citation"])
    w.writeheader()
    for r in records:
        r = dict(r)
        r["judge_names"] = "; ".join(r.get("judge_names") or [])
        w.writerow(r)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv")


@router.get("/statutes")
async def export_statutes(format: str = Query(default="jsonl", pattern="^(csv|jsonl)$"), limit: int = Query(default=50000, le=500000), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(StatuteSection, Statute, StatuteSectionVersion).join(Statute, Statute.id == StatuteSection.statute_id).outerjoin(StatuteSectionVersion, StatuteSectionVersion.id == StatuteSection.current_version_id).limit(limit))).all()
    records = [
        {"statute": st.name, "short_name": st.short_name, "jurisdiction": st.jurisdiction, "section_number": sec.section_number, "section_title": sec.section_title, "version_no": ver.version_no if ver else None, "effective_from": ver.effective_from.isoformat() if ver and ver.effective_from else None, "effective_to": ver.effective_to.isoformat() if ver and ver.effective_to else None, "version_confidence": ver.version_confidence if ver else None, "section_text": ver.section_text if ver else None}
        for sec, st, ver in rows
    ]
    if format == "jsonl":
        return StreamingResponse((json.dumps(r, ensure_ascii=False) + "\n" for r in records), media_type="application/x-ndjson")
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=list(records[0].keys()) if records else ["statute"])
    w.writeheader()
    w.writerows(records)
    out.seek(0)
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv")
