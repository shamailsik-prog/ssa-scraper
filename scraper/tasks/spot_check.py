"""
Spot checks (operator request, 24 September 2026): every 30 minutes a few promoted records are
fetched again from their source and compared with what the corpus holds, so the operator can see
on /status that what was stored is what the site shows.

* Judgments (PakistanLawSite): three promoted judgments chosen at random, re-fetched through the
  login session under the ordinary pacing and lock, the page text produced exactly as the
  connector produced it (Case Description modal first, else the cleaned page), and compared with
  the stored full text by the same whitespace-normalised hash promotion uses. The stored citation
  must also appear on the page. A login or verification page ends the run without a verdict on
  the records (the slot is handled as during a harvest); a block halts the source as always.
* Statute sections (public sources, PakistanCode): two current section versions chosen at random,
  their source page re-fetched with the public fetcher (allow-list, robots, retries), and the
  section text looked for in the page text.

Every check is one row in spot_check: kind, what was checked, the result (match, differs,
unreachable, login_required, skipped) and a short detail. Nothing is changed in the corpus.
"""

from __future__ import annotations

import difflib
import logging
import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from celery import shared_task
from sqlalchemy import func, select

from scraper.auth.session_manager import (
    BrowserDisconnected,
    LoginRequired,
    NoActiveSlot,
    SessionLockHeld,
    playwright_browser_factory,
)
from scraper.config import settings
from scraper.database import SessionLocal, run_async
from scraper.fetchers import HttpFetcher, canonical_text_hash, pdf_text_with_ocr
from scraper.models import Judgment, ScraperSource, SourceProvenance, SpotCheck, Statute, StatuteSection, StatuteSectionVersion
from scraper.notify import notify
from scraper.parsers.text_cleaner import clean_html
from scraper.security import ExplicitBlock, VerificationRequired

logger = logging.getLogger(__name__)


def _norm(text: Optional[str]) -> str:
    return " ".join((text or "").split())


def _similarity(a: str, b: str, limit: int = 4000) -> float:
    return round(difflib.SequenceMatcher(None, a[:limit], b[:limit]).ratio(), 3)


def _record(db, *, kind: str, label: str, url: Optional[str], result: str, detail: str, similarity: Optional[float] = None, record_id=None) -> SpotCheck:
    row = SpotCheck(kind=kind, label=label[:300], source_url=(url or "")[:1000] or None, result=result, detail=detail[:1000], similarity=similarity, record_id=record_id)
    db.add(row)
    return row


# --------------------------------------------------------------------------- judgments (login session)
async def check_judgments(
    db,
    *,
    sample: Optional[int] = None,
    browser_factory: Callable = playwright_browser_factory,
    redis_client=None,
    sleep=None,
) -> List[Dict[str, Any]]:
    from scraper.auth.session_manager import raise_for_verdict
    from scraper.extractors.judgment_guards import strip_leading_judgment_chrome
    from scraper.tasks.pakistanlawsite import PacingBudgetExceeded, PakistanLawSitePipeline, SOURCE_NAME

    n = int(sample if sample is not None else settings.SPOT_CHECK_JUDGMENTS)
    if n <= 0 or not settings.login_scraping_effective:
        return [{"skipped": "login scraping not permitted here" if n > 0 else "SPOT_CHECK_JUDGMENTS=0"}]
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == SOURCE_NAME))).scalars().first()
    if source is None or source.state != "ACTIVE":
        return [{"skipped": f"{SOURCE_NAME} is {source.state if source else 'missing'}"}]
    rows = (
        await db.execute(
            select(Judgment)
            .where(Judgment.source_name == SOURCE_NAME, Judgment.source_url.isnot(None), Judgment.full_text.isnot(None))
            .order_by(func.random())
            .limit(n)
        )
    ).scalars().all()
    if not rows:
        return [{"skipped": "no promoted PakistanLawSite judgment with a source URL"}]
    kwargs = {"browser_factory": browser_factory, "redis_client": redis_client}
    if sleep is not None:
        kwargs["sleep"] = sleep
    pipeline = PakistanLawSitePipeline(db, source, **kwargs)
    pipeline._assert_permitted()
    from scraper.auth.session_manager import SessionLock

    lock = SessionLock(SOURCE_NAME, redis_client)
    try:
        await lock.acquire()
    except SessionLockHeld:
        await lock.release()  # closes the client the lock opened for itself
        return [{"skipped": "a login-session job holds the lock; checked on the next run"}]
    results: List[Dict[str, Any]] = []
    try:
        pipeline._session_lock = lock
        slot = await pipeline.manager.current_slot()
        if slot is None:
            return [{"skipped": "no ACTIVE slot"}]
        # The page is fetched on the current slot only: a spot check never walks the continuity
        # ladder (no reconnect on the alternate slot, no switch of the current slot), so a bounce
        # here costs one slot its state exactly as a harvest would, and nothing more.
        browser = await pipeline.runner.open(slot)
        for j in rows:
            label = j.canonical_citation
            try:
                await pipeline._charge_page()
                page = await browser.goto(j.source_url, capture_case_description_modal=bool(re.search(r"ReferenceCaseLawSearch", j.source_url or "", flags=re.IGNORECASE)))
                await pipeline.manager.touch(slot.slot_number)
                raise_for_verdict(page)
            except PacingBudgetExceeded as exc:
                results.append({"label": label, "result": "skipped", "detail": f"pacing: {exc}"})
                _record(db, kind="judgment", label=label, url=j.source_url, result="skipped", detail=f"pacing budget spent: {exc}", record_id=j.id)
                break
            except (LoginRequired, VerificationRequired) as exc:
                await pipeline.manager.mark_needs_human_login(slot.slot_number, f"{type(exc).__name__}: {exc}")
                results.append({"label": label, "result": "login_required", "detail": str(exc)[:300]})
                _record(db, kind="judgment", label=label, url=j.source_url, result="login_required", detail=f"{type(exc).__name__}: {exc}", record_id=j.id)
                break
            except (NoActiveSlot, BrowserDisconnected) as exc:
                results.append({"label": label, "result": "unreachable", "detail": str(exc)[:300]})
                _record(db, kind="judgment", label=label, url=j.source_url, result="unreachable", detail=f"{type(exc).__name__}: {str(exc)[:300]}", record_id=j.id)
                break
            except ExplicitBlock as exc:
                await pipeline.manager.halt_source(f"explicit block during a spot check: {exc}", slot_number=slot.slot_number)
                results.append({"label": label, "result": "unreachable", "detail": f"block: {exc}"})
                _record(db, kind="judgment", label=label, url=j.source_url, result="unreachable", detail=f"block, source halted: {exc}", record_id=j.id)
                break
            text = clean_html(page.html)
            modal_text = strip_leading_judgment_chrome((page.metadata or {}).get("case_description_modal_text"))
            if modal_text:
                text = modal_text
            text = strip_leading_judgment_chrome(text)
            live_hash = canonical_text_hash(text)
            stored_hash = j.full_text_hash or canonical_text_hash(j.full_text or "")
            citation_seen = _norm(j.canonical_citation).lower() in _norm(page.html).lower() or _norm(j.canonical_citation).lower() in _norm(text).lower()
            if live_hash == stored_hash and citation_seen:
                result, detail, sim = "match", "stored full text is exactly what the site shows and the citation is on the page", 1.0
            elif live_hash == stored_hash:
                result, detail, sim = "differs", "the text matches but the stored citation is not on the page (the URL may now serve another document)", 1.0
            else:
                sim = _similarity(_norm(text), _norm(j.full_text or ""))
                result = "differs"
                detail = f"live text differs from the stored text (similarity {sim:.3f}; live {len(_norm(text))} chars, stored {len(_norm(j.full_text or ''))} chars)" + ("" if citation_seen else "; citation string not seen on the page")
            _record(db, kind="judgment", label=label, url=j.source_url, result=result, detail=detail, similarity=sim, record_id=j.id)
            results.append({"label": label, "result": result, "similarity": sim})
        await db.flush()
        return results
    finally:
        pipeline._session_lock = None
        try:
            await pipeline._persist_live_session()
        except Exception as exc:
            logger.warning("spot check: could not persist the live session: %s", exc)
        await pipeline.runner.close()
        await lock.release()


# --------------------------------------------------------------------------- statute sections (public)
async def check_statute_sections(db, *, sample: Optional[int] = None, allow_private_for_tests: bool = False) -> List[Dict[str, Any]]:
    n = int(sample if sample is not None else settings.SPOT_CHECK_STATUTES)
    if n <= 0:
        return [{"skipped": "SPOT_CHECK_STATUTES=0"}]
    rows = (
        await db.execute(
            select(StatuteSectionVersion, StatuteSection, Statute, SourceProvenance)
            .join(StatuteSection, StatuteSection.id == StatuteSectionVersion.section_id)
            .join(Statute, Statute.id == StatuteSection.statute_id)
            .join(SourceProvenance, SourceProvenance.id == StatuteSectionVersion.source_provenance_id)
            .where(StatuteSection.current_version_id == StatuteSectionVersion.id, SourceProvenance.source_url.isnot(None))
            .order_by(func.random())
            .limit(n)
        )
    ).all()
    if not rows:
        return [{"skipped": "no current statute section with a source URL"}]
    results: List[Dict[str, Any]] = []
    sources: Dict[str, ScraperSource] = {}
    for version, section, statute, prov in rows:
        label = f"{statute.short_name or statute.name} s. {section.section_number}"
        source = sources.get(prov.source_name)
        if source is None:
            source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == prov.source_name))).scalars().first()
            if source is None:
                _record(db, kind="statute_section", label=label, url=prov.source_url, result="skipped", detail=f"source {prov.source_name} not found", record_id=section.id)
                results.append({"label": label, "result": "skipped"})
                continue
            sources[prov.source_name] = source
        try:
            async with HttpFetcher(source, allow_private_for_tests=allow_private_for_tests) as fetcher:
                res = await fetcher.get(prov.source_url)
        except ExplicitBlock as exc:
            if exc.kind == "robots_disallow":
                _record(db, kind="statute_section", label=label, url=prov.source_url, result="unreachable", detail=f"robots.txt disallows the page now: {str(exc)[:200]}", record_id=section.id)
                results.append({"label": label, "result": "unreachable", "detail": "robots disallow"})
                continue
            # A block halts the source exactly as the public pipeline does (specification 3.7 / 4.5).
            now = datetime.now(timezone.utc)
            source.state = "HALTED"
            source.state_reason = f"explicit block during a spot check: {str(exc)[:900]}"
            source.state_changed_at = now
            source.requires_admin_review = True
            await notify(db, level="critical", code="SOURCE_HALTED", message=f"explicit block — {str(exc)[:500]}; no evasion attempted; admin review required (spot check)", source_name=source.source_name)
            _record(db, kind="statute_section", label=label, url=prov.source_url, result="unreachable", detail=f"block, source halted: {str(exc)[:300]}", record_id=section.id)
            results.append({"label": label, "result": "unreachable", "detail": f"block: {str(exc)[:200]}"})
            break
        except Exception as exc:
            _record(db, kind="statute_section", label=label, url=prov.source_url, result="unreachable", detail=f"{type(exc).__name__}: {str(exc)[:300]}", record_id=section.id)
            results.append({"label": label, "result": "unreachable", "detail": str(exc)[:200]})
            continue
        if res.status_code >= 400:
            _record(db, kind="statute_section", label=label, url=prov.source_url, result="unreachable", detail=f"HTTP {res.status_code}", record_id=section.id)
            results.append({"label": label, "result": "unreachable", "detail": f"HTTP {res.status_code}"})
            continue
        if res.is_pdf:
            page_text, _ocr = pdf_text_with_ocr(res.content)
            page_text = _norm(page_text)
        elif "html" in (res.content_type or "").lower() or res.text.lstrip().startswith("<"):
            page_text = _norm(clean_html(res.text))
        else:
            page_text = _norm(res.text)
        stored = _norm(version.section_text)
        if stored and stored.lower() in page_text.lower():
            result, detail, sim = "match", "the whole stored section text is on the source page", 1.0
        else:
            sim = _similarity(page_text, stored)
            # the section number with a piece of its heading is a weaker sign the section is still there
            heading = _norm(f"{section.section_number} {section.section_title or ''}")[:80].lower()
            present = bool(heading.strip()) and heading in page_text.lower()
            result = "differs"
            detail = ("section heading still on the page but the stored text was not found" if present else "neither the stored text nor the section heading was found on the page") + f" (similarity {sim:.3f})"
        _record(db, kind="statute_section", label=label, url=prov.source_url, result=result, detail=detail, similarity=sim, record_id=section.id)
        results.append({"label": label, "result": result, "similarity": sim})
    await db.flush()
    return results


async def spot_check_judgments_async(**kwargs) -> Dict[str, Any]:
    async with SessionLocal() as db:
        try:
            results = await check_judgments(db, **kwargs)
        except Exception as exc:
            logger.exception("spot check of judgments failed")
            _record(db, kind="judgment", label="run", url=None, result="unreachable", detail=f"run failed: {type(exc).__name__}: {str(exc)[:300]}")
            results = [{"error": str(exc)[:300]}]
        await db.commit()
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "results": results}


async def spot_check_statutes_async(**kwargs) -> Dict[str, Any]:
    async with SessionLocal() as db:
        try:
            results = await check_statute_sections(db, **kwargs)
        except Exception as exc:
            logger.exception("spot check of statute sections failed")
            _record(db, kind="statute_section", label="run", url=None, result="unreachable", detail=f"run failed: {type(exc).__name__}: {str(exc)[:300]}")
            results = [{"error": str(exc)[:300]}]
        await db.commit()
    return {"checked_at": datetime.now(timezone.utc).isoformat(), "results": results}


@shared_task(name="scraper.tasks.spot_check.spot_check_judgments")
def spot_check_judgments() -> Dict[str, Any]:
    return run_async(spot_check_judgments_async())


@shared_task(name="scraper.tasks.spot_check.spot_check_statutes")
def spot_check_statutes() -> Dict[str, Any]:
    return run_async(spot_check_statutes_async())
