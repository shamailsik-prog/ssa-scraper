"""PakistanLawSite judgments held for a reason a later extractor fix removes ("court unknown": 962 on 11 Oct 2026,
fixed by reading the bracketed court) stayed held until someone ran `pls_admin requalify-quarantine` by hand on the
server. The re-check now runs on its own, hourly, on the maintenance queue. It reads only stored text (no request
to PakistanLawSite) and leaves alone judgments held on the merits: a citation conflict or a human decision."""

from __future__ import annotations

from sqlalchemy import select

from scraper.fetchers import record_provenance
from scraper.models import QuarantineQueue, ScraperSource, ScraperStaging
from scraper.tasks.celery_app import app
from scraper.tasks.pls_backfill import requalify_quarantined

_TEXT = (
    "1983 P Cr. L J 2056   [Karachi]   Before Z. C. Valiani, J   ABDUL RAZZAK-Appellant   Versus   THE STATE-Respondent\n"
    "Criminal Appeal No. 23 of 1977, decided on 17th April, 1983.\n"
    + "The appellant was convicted under section 302 of the Pakistan Penal Code and the appeal is heard.\n" * 8
    + "The appeal is dismissed."
)


async def _held(db, source, *, reason, n):
    url = f"https://www.pakistanlawsite.com/Login/Check?caseName=1983P{n}"
    prov = await record_provenance(db, source=source, url=url, content=(_TEXT + str(n)).encode(), content_kind="html", route={})
    st = ScraperStaging(
        provenance_id=prov.id,
        source_id=source.id,
        source_name=source.source_name,
        access_method=source.access_method,
        source_url=url,
        content_hash=prov.content_hash,
        raw_text=_TEXT,
        extracted_citation=f"1983 PCrLJ {2056 + n}" if n else "1983 PCrLJ 2056",
        route_json={},
        status="quarantined",
        quarantine_reason=reason,
    )
    db.add(st)
    await db.flush()
    return st


async def test_scheduled_requalify_releases_court_unknown_but_keeps_merits_holds(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    court_unknown = await _held(db, source, reason="court unknown", n=0)
    conflict = await _held(db, source, reason="citation conflict between deterministic parser and AI", n=1)
    db.add(QuarantineQueue(staging_id=court_unknown.id, reason="court unknown", source_name="PakistanLawSite"))
    await db.commit()

    counts = await requalify_quarantined(db, dry_run=False, skip_final=True)
    await db.commit()

    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == court_unknown.id))).scalars().one()
    assert st.status in ("promoted", "duplicate"), (st.status, st.quarantine_reason)
    kept = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == conflict.id))).scalars().one()
    assert kept.status == "quarantined"
    assert kept.quarantine_reason == "citation conflict between deterministic parser and AI"
    assert counts["skipped_final"] == 1
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == court_unknown.id))).scalars().one()
    assert item.reviewed is True and item.resolution == "requalified"


async def test_merits_holds_do_not_fill_the_batch_ahead_of_releasable_rows(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    for n in range(1, 4):  # older merits holds, more of them than the batch
        await _held(db, source, reason="citation conflict between deterministic parser and AI", n=n)
    await db.commit()
    releasable = await _held(db, source, reason="court unknown", n=0)
    await db.commit()

    await requalify_quarantined(db, dry_run=False, limit=2, skip_final=True)
    await db.commit()
    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == releasable.id))).scalars().one()
    assert st.status in ("promoted", "duplicate")


def test_requalify_runs_hourly_on_the_maintenance_queue():
    entry = app.conf.beat_schedule["pls-requalify-quarantined"]
    assert entry["task"] == "scraper.tasks.pls_backfill.requalify_quarantined"
    assert entry["kwargs"]["skip_final"] is True
    assert app.conf.task_routes["scraper.tasks.pls_backfill.requalify_quarantined"] == {"queue": "maintenance"}


async def test_still_held_row_shows_its_current_reason_in_the_review_queue(db):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanLawSite"))).scalars().first()
    st = await _held(db, source, reason="court unknown", n=7)
    st.raw_text = "1983 PCrLJ 2063 short page"  # still fails, for another reason now
    db.add(QuarantineQueue(staging_id=st.id, reason="court unknown", source_name="PakistanLawSite"))
    await db.commit()
    await requalify_quarantined(db, dry_run=False, skip_final=True)
    await db.commit()
    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == st.id))).scalars().one()
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert st.status == "quarantined"
    assert item.reason == st.quarantine_reason


async def test_overlapping_requalify_runs_do_not_both_run():
    from sqlalchemy import text as sql_text

    from scraper.database import engine
    from scraper.tasks.pls_backfill import REQUALIFY_LOCK_KEY, run_requalify

    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        assert (await conn.execute(sql_text("SELECT pg_try_advisory_lock(:k)"), {"k": REQUALIFY_LOCK_KEY})).scalar()
        try:
            assert await run_requalify(skip_final=True) == {"skipped": "requalify_run_in_progress"}
        finally:
            await conn.execute(sql_text("SELECT pg_advisory_unlock(:k)"), {"k": REQUALIFY_LOCK_KEY})
