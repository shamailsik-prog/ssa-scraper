"""
Async engine, session factory, migration runner, database roles and seed data.

Roles (Amendment §0, Cursor command):
  * corpus_writer  — the service role (the only writer). Created when CORPUS_WRITER_PASSWORD is set.
  * sikander_reader — the read-only role handed to SIKANDER AI. It may SELECT the contract
    tables only. Every internal table (staging, session state, extraction audit, archive
    configuration, secrets) is explicitly REVOKEd. Created when SIKANDER_READER_PASSWORD is
    set; otherwise the dashboard shows NOT CONFIGURED.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import pkgutil
from datetime import datetime, timezone
from typing import AsyncIterator, List

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from scraper.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DATABASE_ECHO,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_timeout=settings.DATABASE_POOL_TIMEOUT,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


def run_async(coro):
    """Run a coroutine to completion in a fresh event loop (Celery / CLI entry points).

    asyncpg connections are bound to the loop that opened them, so the pool is disposed
    before and after every run.
    """

    async def _wrapped():
        await engine.dispose(close=False)
        try:
            return await coro
        finally:
            await engine.dispose()

    return asyncio.run(_wrapped())


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


# --------------------------------------------------------------------------- migrations
async def _applied_versions(conn) -> set:
    await conn.execute(
        text("CREATE TABLE IF NOT EXISTS schema_migrations (version VARCHAR(100) PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
    )
    rows = await conn.execute(text("SELECT version FROM schema_migrations"))
    return {r[0] for r in rows}


def discover_migrations() -> List[str]:
    import migrations as pkg

    names = sorted(m.name for m in pkgutil.iter_modules(pkg.__path__) if m.name[0].isdigit())
    return names


async def run_migrations() -> List[str]:
    """Apply every migrations/NNN_*.py whose version is not yet recorded. Returns the list applied."""
    applied_now: List[str] = []
    async with engine.begin() as conn:
        done = await _applied_versions(conn)
    for name in discover_migrations():
        if name in done:
            continue
        module = importlib.import_module(f"migrations.{name}")
        async with engine.begin() as conn:
            await module.upgrade(conn)
            await conn.execute(text("INSERT INTO schema_migrations(version) VALUES (:v) ON CONFLICT DO NOTHING"), {"v": name})
        applied_now.append(name)
        logger.info("migration applied: %s", name)
    return applied_now


# --------------------------------------------------------------------------- roles
def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return '"' + name + '"'


async def ensure_roles() -> dict:
    """Create/refresh corpus_writer and sikander_reader. Idempotent; safe to call on every start."""
    from scraper.models import CONTRACT_TABLES, INTERNAL_TABLES

    status = {"reader": "NOT CONFIGURED", "writer": "NOT CONFIGURED"}
    reader_pw = settings.SIKANDER_READER_PASSWORD.get_secret_value()
    writer_pw = settings.CORPUS_WRITER_PASSWORD.get_secret_value()
    async with engine.begin() as conn:
        dbname = (await conn.execute(text("SELECT current_database()"))).scalar()
        if writer_pw:
            role = _quote_ident(settings.CORPUS_WRITER_ROLE)
            exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.CORPUS_WRITER_ROLE})).scalar()
            if not exists:
                await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD :p".replace(":p", _pg_literal(writer_pw))))
            else:
                await conn.execute(text(f"ALTER ROLE {role} WITH LOGIN PASSWORD {_pg_literal(writer_pw)}"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {_quote_ident(dbname)} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
            await conn.execute(text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role}"))
            status["writer"] = "CONFIGURED"
        if reader_pw:
            role = _quote_ident(settings.SIKANDER_READER_ROLE)
            exists = (await conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.SIKANDER_READER_ROLE})).scalar()
            if not exists:
                await conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD {_pg_literal(reader_pw)} NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT"))
            else:
                await conn.execute(text(f"ALTER ROLE {role} WITH LOGIN PASSWORD {_pg_literal(reader_pw)} NOSUPERUSER NOCREATEDB NOCREATEROLE"))
            await conn.execute(text(f"GRANT CONNECT ON DATABASE {_quote_ident(dbname)} TO {role}"))
            await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
            # Start from nothing, then grant SELECT on the contract tables only.
            await conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"))
            await conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}"))
            await conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role}"))
            await conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}"))
            for t in CONTRACT_TABLES:
                await conn.execute(text(f"GRANT SELECT ON TABLE {_quote_ident(t)} TO {role}"))
            for t in INTERNAL_TABLES:
                await conn.execute(text(f"REVOKE ALL ON TABLE {_quote_ident(t)} FROM {role}"))
            # PUBLIC must not leak internal tables to the reader either.
            for t in INTERNAL_TABLES:
                await conn.execute(text(f"REVOKE ALL ON TABLE {_quote_ident(t)} FROM PUBLIC"))
            status["reader"] = "CONFIGURED"
    return status


def _pg_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --------------------------------------------------------------------------- seed
COURT_SEED = [
    ("Supreme Court of Pakistan", "SC", "supreme_court", "Federal", "Islamabad", ["Supreme Court", "S.C.", "SCP"]),
    ("Federal Shariat Court", "FSC", "federal_shariat_court", "Federal", "Islamabad", ["F.S.C.", "Shariat Court"]),
    ("Lahore High Court", "LHC", "high_court", "Punjab", "Lahore", ["Lah.", "Lahore"]),
    ("High Court of Sindh", "SHC", "high_court", "Sindh", "Karachi", ["Kar.", "Karachi", "Sindh High Court"]),
    ("Peshawar High Court", "PHC", "high_court", "KPK", "Peshawar", ["Pesh.", "Peshawar"]),
    ("High Court of Balochistan", "BHC", "high_court", "Balochistan", "Quetta", ["Quetta", "Balochistan High Court"]),
    ("Islamabad High Court", "IHC", "high_court", "Islamabad", "Islamabad", ["Isl.", "Islamabad"]),
    ("High Court of Azad Jammu and Kashmir", "AJK-HC", "high_court", "AJK", "Muzaffarabad", ["AJK High Court", "AJ&K High Court"]),
    ("Supreme Appellate Court Gilgit-Baltistan", "SAC-GB", "supreme_appellate_court", "GB", "Gilgit", ["Gilgit-Baltistan"]),
    ("Azad Jammu and Kashmir Supreme Court", "AJK-SC", "supreme_court", "AJK", "Muzaffarabad", ["AJ&K"]),
    ("Shariat Appellate Bench, Supreme Court", "SC-SAB", "shariat_appellate_bench", "Federal", "Islamabad", ["Shariat Appellate Bench"]),
]

SOURCE_SEED = [
    # name, display, url, access_method, allow_list, case_law, statutes, instruments, frequency_h, extraction_mode
    ("PakistanLawSite", "Pakistan Law Site (subscription)", "https://www.pakistanlawsite.com", "login_session", ["www.pakistanlawsite.com", "pakistanlawsite.com"], True, True, False, 24, "hybrid"),
    ("NasirLawSite", "Nasir Law Site", "https://www.nasirlawsite.com", "public", ["www.nasirlawsite.com", "nasirlawsite.com"], True, True, False, 24, "hybrid"),
    ("PakistanCode", "Pakistan Code (Ministry of Law)", "https://pakistancode.gov.pk/english/index.php", "public", ["pakistancode.gov.pk", "www.pakistancode.gov.pk"], False, True, True, 48, "hybrid"),
    ("SupremeCourt", "Supreme Court of Pakistan", "https://www.supremecourt.gov.pk/judgements/", "public", ["www.supremecourt.gov.pk", "supremecourt.gov.pk"], True, False, False, 12, "hybrid"),
    ("LahoreHighCourt", "Lahore High Court", "https://opc.lhc.gov.pk/Relevant_Laws.aspx", "public", ["opc.lhc.gov.pk", "sys.lhc.gov.pk", "lhc.gov.pk", "www.lhc.gov.pk"], True, False, False, 12, "hybrid"),
    ("SindhHighCourt", "High Court of Sindh", "https://www.shc.gov.pk", "public", ["www.shc.gov.pk", "shc.gov.pk", "caselaw.shc.gov.pk"], True, False, False, 12, "hybrid"),
    (
        "PeshawarHighCourt",
        "Peshawar High Court",
        "https://www.peshawarhighcourt.gov.pk/PHCCMS/reportedJudgments.php",
        "public",
        ["www.peshawarhighcourt.gov.pk", "peshawarhighcourt.gov.pk"],
        True,
        False,
        False,
        12,
        "hybrid",
    ),
    (
        "BalochistanHighCourt",
        "High Court of Balochistan",
        "https://bhc.gov.pk/judgments",
        "public",
        ["bhc.gov.pk", "www.bhc.gov.pk", "portal.bhc.gov.pk", "api.bhc.gov.pk"],
        True,
        False,
        False,
        24,
        "hybrid",
    ),
    ("IslamabadHighCourt", "Islamabad High Court", "https://mis.ihc.gov.pk/frmJgmnt.aspx?jgs=1", "public", ["mis.ihc.gov.pk", "ihc.gov.pk", "www.ihc.gov.pk"], True, False, False, 12, "hybrid"),
    ("AJKHighCourt", "Azad Jammu & Kashmir High Court", "https://ajkhighcourt.gok.pk/important-judgments", "public", ["ajkhighcourt.gok.pk", "www.ajkhighcourt.gok.pk"], True, False, False, 24, "hybrid"),
    (
        "AJKSupremeCourt",
        "Azad Jammu & Kashmir Supreme Court",
        "https://ajksupremecourt.gok.pk/judgements-orders/",
        "public",
        ["ajksupremecourt.gok.pk", "www.ajksupremecourt.gok.pk", "scapp.ajksupremecourt.gok.pk"],
        True,
        False,
        False,
        24,
        "hybrid",
    ),
    (
        "SupremeAppellateCourtGB",
        "Supreme Appellate Court Gilgit-Baltistan",
        "https://sacgb.gov.pk/Judgments.html",
        "public",
        ["sacgb.gov.pk", "www.sacgb.gov.pk"],
        True,
        False,
        False,
        24,
        "hybrid",
    ),
    ("FederalShariatCourt", "Federal Shariat Court", "https://www.federalshariatcourt.gov.pk/en/judgments/", "public", ["www.federalshariatcourt.gov.pk", "federalshariatcourt.gov.pk"], True, False, False, 24, "hybrid"),
    ("NationalAssembly", "National Assembly of Pakistan", "https://na.gov.pk/en/acts-tenure.php", "public", ["na.gov.pk", "www.na.gov.pk"], False, True, True, 168, "hybrid"),
    ("Senate", "Senate of Pakistan", "https://senate.gov.pk/en/acts.php?id=-1&catid=186&subcatid=285&cattitle=Acts", "public", ["senate.gov.pk", "www.senate.gov.pk"], False, True, True, 168, "hybrid"),
    ("PunjabAssembly", "Provincial Assembly of the Punjab", "https://www.pap.gov.pk/acts", "public", ["www.pap.gov.pk", "pap.gov.pk", "punjablaws.gov.pk", "www.punjablaws.gov.pk"], False, True, True, 168, "hybrid"),
    ("SindhAssembly", "Provincial Assembly of Sindh", "https://www.pas.gov.pk/index.php/acts", "public", ["www.pas.gov.pk", "pas.gov.pk", "sindhlaws.gov.pk", "www.sindhlaws.gov.pk"], False, True, True, 168, "hybrid"),
    ("KPAssembly", "Provincial Assembly of Khyber Pakhtunkhwa", "https://www.pakp.gov.pk/act/", "public", ["www.pakp.gov.pk", "pakp.gov.pk", "kpcode.kp.gov.pk"], False, True, True, 168, "hybrid"),
    (
        "BalochistanAssembly",
        "Provincial Assembly of Balochistan",
        "https://www.pabalochistan.gov.pk/acts",
        "public",
        ["www.pabalochistan.gov.pk", "pabalochistan.gov.pk", "balochistancode.gob.pk", "www.balochistancode.gob.pk"],
        False,
        True,
        True,
        168,
        "hybrid",
    ),
    (
        "AJKAssembly",
        "Azad Jammu and Kashmir Law Department",
        "https://law.gok.pk/revised-volume/",
        "public",
        ["law.gok.pk", "www.law.gok.pk"],
        False,
        True,
        True,
        168,
        "hybrid",
    ),
    (
        "GBAssembly",
        "Gilgit-Baltistan Law Department",
        "https://gilgitbaltistan.gov.pk/pages/acts",
        "public",
        ["gilgitbaltistan.gov.pk", "www.gilgitbaltistan.gov.pk"],
        False,
        True,
        False,
        168,
        "hybrid",
    ),
    ("GazetteOfPakistan", "Gazette of Pakistan (Printing Corporation)", "http://pcp.gov.pk/Download", "public", ["www.pcp.gov.pk", "pcp.gov.pk"], False, False, True, 48, "hybrid"),
]


async def seed_data() -> None:
    from sqlalchemy import select

    from scraper.models import ArchiveTarget, BrowserSessionSlot, Court, CorpusMetadata, ScraperSource

    async with SessionLocal() as db:
        existing_courts = {c.short_code for c in (await db.execute(select(Court))).scalars().all()}
        for name, code, level, prov, city, aliases in COURT_SEED:
            if code not in existing_courts:
                db.add(Court(name=name, short_code=code, court_level=level, jurisdiction_province=prov, city=city, aliases=aliases))
        existing_source_rows = {s.source_name: s for s in (await db.execute(select(ScraperSource))).scalars().all()}
        for name, display, url, method, allow, case_law, statutes, instruments, freq, mode in SOURCE_SEED:
            if name in existing_source_rows:
                continue
            db.add(
                ScraperSource(
                    source_name=name,
                    display_name=display,
                    source_url=url,
                    access_method=method,
                    allow_list=allow,
                    scrape_case_law=case_law,
                    scrape_statutes=statutes,
                    scrape_instruments=instruments,
                    requires_login=(method == "login_session"),
                    scrape_frequency_hours=freq,
                    extraction_mode=mode,
                    extraction_min_confidence=settings.SGAI_DEFAULT_MIN_CONFIDENCE,
                    scrapegraph_schema_version=settings.SGAI_SCHEMA_VERSION,
                    state="PAUSED" if method == "login_session" else "ACTIVE",
                    state_reason="Awaiting human login" if method == "login_session" else None,
                )
            )
        # Keep LHC source aligned with the connector defaults on existing databases.
        lhc = existing_source_rows.get("LahoreHighCourt")
        if lhc is not None:
            current_allow = {h.lower() for h in (lhc.allow_list or [])}
            merged_allow = list(dict.fromkeys((lhc.allow_list or []) + ["opc.lhc.gov.pk", "sys.lhc.gov.pk", "lhc.gov.pk", "www.lhc.gov.pk"]))
            if current_allow != {h.lower() for h in merged_allow}:
                lhc.allow_list = merged_allow
            if not (lhc.source_url or "").startswith("https://opc.lhc.gov.pk/"):
                lhc.source_url = "https://opc.lhc.gov.pk/Relevant_Laws.aspx"
        bhc = existing_source_rows.get("BalochistanHighCourt")
        if bhc is not None:
            current_allow = {h.lower() for h in (bhc.allow_list or [])}
            merged_allow = list(dict.fromkeys((bhc.allow_list or []) + ["bhc.gov.pk", "www.bhc.gov.pk", "portal.bhc.gov.pk", "api.bhc.gov.pk"]))
            if current_allow != {h.lower() for h in merged_allow}:
                bhc.allow_list = merged_allow
        na = existing_source_rows.get("NationalAssembly")
        if na is not None and (na.source_url or "").startswith("https://na.gov.pk/en/legis.php"):
            na.source_url = "https://na.gov.pk/en/acts-tenure.php"
        senate = existing_source_rows.get("Senate")
        if senate is not None and (senate.source_url or "").startswith("https://senate.gov.pk/en/legislation.php"):
            senate.source_url = "https://senate.gov.pk/en/acts.php?id=-1&catid=186&subcatid=285&cattitle=Acts"
        kp = existing_source_rows.get("KPAssembly")
        if kp is not None and (kp.source_url or "").startswith("https://www.pakp.gov.pk/acts"):
            kp.source_url = "https://www.pakp.gov.pk/act/"
        balochistan_assembly = existing_source_rows.get("BalochistanAssembly")
        if balochistan_assembly is not None:
            current_allow = {h.lower() for h in (balochistan_assembly.allow_list or [])}
            merged_allow = list(
                dict.fromkeys(
                    (balochistan_assembly.allow_list or [])
                    + ["balochistancode.gob.pk", "www.balochistancode.gob.pk"]
                )
            )
            if current_allow != {h.lower() for h in merged_allow}:
                balochistan_assembly.allow_list = merged_allow
        gazette = existing_source_rows.get("GazetteOfPakistan")
        if gazette is not None and ((gazette.source_url or "").startswith("https://www.pcp.gov.pk/gazette") or (gazette.source_url or "").startswith("http://www.pcp.gov.pk/gazette")):
            gazette.source_url = "http://pcp.gov.pk/Download"
        pakistan_code = existing_source_rows.get("PakistanCode")
        if pakistan_code is not None:
            if (pakistan_code.source_url or "").rstrip("/") == "https://pakistancode.gov.pk":
                pakistan_code.source_url = "https://pakistancode.gov.pk/english/index.php"
            old_listings = [
                "https://pakistancode.gov.pk/english/LGu3ZBxW1-apaUY2Fqa-apaUY2Fqa-sg-jjjjjjjjjjjjj",
                "https://pakistancode.gov.pk/federal",
            ]
            cfg = dict(pakistan_code.config_json or {})
            if cfg.get("statute_urls") == old_listings:
                cfg.pop("statute_urls", None)
                pakistan_code.config_json = cfg
        slots = {(s.source_name, s.slot_number) for s in (await db.execute(select(BrowserSessionSlot))).scalars().all()}
        for n in (1, 2):
            if ("PakistanLawSite", n) not in slots:
                db.add(BrowserSessionSlot(source_name="PakistanLawSite", slot_number=n, role="primary" if n == 1 else "alternate", state="EMPTY"))
        targets = {t.name for t in (await db.execute(select(ArchiveTarget))).scalars().all()}
        if settings.ARCHIVE_LOCAL_PATH and "local" not in targets:
            db.add(ArchiveTarget(name="local", target_type="local_path", root_path=settings.ARCHIVE_LOCAL_PATH, mirror_login_session_rows=settings.MIRROR_LOGIN_SESSION_ROWS))
        # corpus_metadata (Annex B-6 / B-9)
        meta = {
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_dim": str(settings.EMBEDDING_DIM),
            "deploy_region": settings.DEPLOY_REGION or "NOT CONFIGURED",
            "data_residency_note": settings.DATA_RESIDENCY_NOTE or "NOT CONFIGURED",
            "schema_contract": "annex-a-v1",
            "scrapegraph_schema_version": str(settings.SGAI_SCHEMA_VERSION),
            "service_started_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_meta = {m.key: m for m in (await db.execute(select(CorpusMetadata))).scalars().all()}
        for k, v in meta.items():
            if k in existing_meta:
                if k in ("embedding_model", "embedding_dim") and existing_meta[k].value != v:
                    # Do not silently change the recorded embedding identity; the embedding worker refuses to run.
                    logger.error("corpus_metadata %s=%s differs from configured %s; embedding worker will refuse", k, existing_meta[k].value, v)
                    continue
                existing_meta[k].value = v
            else:
                db.add(CorpusMetadata(key=k, value=v))
        await db.commit()


async def embedding_identity_matches() -> bool:
    from sqlalchemy import select

    from scraper.models import CorpusMetadata

    async with SessionLocal() as db:
        rows = {m.key: m.value for m in (await db.execute(select(CorpusMetadata).where(CorpusMetadata.key.in_(["embedding_model", "embedding_dim"])))).scalars().all()}
    return rows.get("embedding_model") == settings.EMBEDDING_MODEL and rows.get("embedding_dim") == str(settings.EMBEDDING_DIM)


async def init_db() -> dict:
    """Extensions → migrations → roles → seed. Returns a status dictionary for /health."""
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    applied = await run_migrations()
    roles = await ensure_roles()
    await seed_data()
    logger.info("database initialised; migrations applied now: %s; roles: %s", applied, roles)
    return {"migrations_applied": applied, "roles": roles}
