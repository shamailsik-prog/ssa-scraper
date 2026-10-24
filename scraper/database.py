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
import os
import pkgutil
from datetime import datetime, timezone
from typing import AsyncIterator, List

from sqlalchemy import String, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

from scraper.config import settings
from scraper.keys import fit_key

logger = logging.getLogger(__name__)

# Postgres advisory locks for idempotent seed / role setup / test DB reset (finding #238).
PG_ADVISORY_COURT_SEED_KEY1 = 0x5353
PG_ADVISORY_COURT_SEED_KEY2 = 0x436F
PG_ADVISORY_ROLE_SETUP_KEY1 = 0x5353
PG_ADVISORY_ROLE_SETUP_KEY2 = 0x524F
PG_ADVISORY_INIT_DB_KEY1 = 0x5353
PG_ADVISORY_INIT_DB_KEY2 = 0x494E
TEST_DB_ADVISORY_LOCK_ID = 738_238_001
PG_ADVISORY_ROLE_SETUP_CLUSTER_LOCK = 738_238_002


class RoleAttributeChangeRequiresSuperuserError(RuntimeError):
    """ensure_roles must demote SUPERUSER or CREATEROLE but the connected role is not a superuser."""


async def _pg_advisory_xact_lock(conn, key1: int, key2: int) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k1, :k2)"), {"k1": key1, "k2": key2})


class Base(DeclarativeBase):
    pass


def connect_args() -> dict:
    """Server settings for every app connection: a session idle inside a transaction for
    DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS is ended by Postgres, releasing its locks (pool_pre_ping replaces it)."""
    timeout = int(settings.DB_IDLE_IN_TRANSACTION_TIMEOUT_SECONDS or 0)
    if timeout <= 0:
        return {}
    return {"server_settings": {"idle_in_transaction_session_timeout": str(timeout * 1000)}}


def _postgres_catalog_database_url() -> str:
    """``postgres`` catalog URL (tests/xdist only when ``DB_LOCK_ON_CATALOG=1``)."""
    from urllib.parse import quote, urlparse, urlunparse

    parsed = urlparse(str(settings.DATABASE_URL))
    return urlunparse(parsed._replace(path="/" + quote("postgres", safe="")))


def _use_catalog_for_advisory_locks() -> bool:
    return os.environ.get("DB_LOCK_ON_CATALOG", "").lower() in ("1", "true", "yes")


def _advisory_lock_database_url() -> str:
    if _use_catalog_for_advisory_locks():
        return _postgres_catalog_database_url()
    return str(settings.DATABASE_URL)


def _engine_kwargs() -> dict:
    kwargs = {
        "connect_args": connect_args(),
        "echo": settings.DATABASE_ECHO,
        "pool_pre_ping": True,
    }
    if os.environ.get("DATABASE_NULL_POOL", "").lower() in ("1", "true", "yes"):
        from sqlalchemy.pool import NullPool

        kwargs["poolclass"] = NullPool
    else:
        kwargs["pool_size"] = settings.DATABASE_POOL_SIZE
        kwargs["max_overflow"] = settings.DATABASE_MAX_OVERFLOW
        kwargs["pool_timeout"] = settings.DATABASE_POOL_TIMEOUT
    return kwargs


engine = create_async_engine(settings.DATABASE_URL, **_engine_kwargs())
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class PgSafeString(TypeDecorator):
    """Strip NUL on bind so unique-key lookups match rows written after flush-time cleanup.

    A value longer than the column (a listing key that embeds a whole judgment URL) is shortened the same way on
    every bind: its head plus a SHA-256 of the full value. Inserts no longer fail, equality lookups with the raw
    value still find the row, and keys that share a long prefix stay distinct."""

    impl = String
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if not isinstance(value, str):
            return value
        if "\x00" in value:
            value = value.replace("\x00", "")
        return fit_key(value, self.impl.length)


def _strip_nul(value):
    """PostgreSQL text and JSONB cannot hold NUL (\x00 / \u0000)."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {_strip_nul(k): _strip_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strip_nul(v) for v in value]
    return value


def _has_nul(value) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(k) or _has_nul(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(v) for v in value)
    return False


def _strip_nul_before_flush(session, flush_context, instances) -> None:
    """Every string and JSON value written by any session loses its NUL characters.

    On 8 October 2026 binary files read as text (a .docx on an assembly site) carried NUL into
    statutes_staging, then into crawl_frontier.query_json through links read from the same file; each
    INSERT failed and killed the whole job. Fixing call sites one by one kept missing the next one."""
    from sqlalchemy import inspect as sa_inspect

    new = set(session.new)
    for obj in list(new) + list(session.dirty):
        try:
            state = sa_inspect(obj)
            keys = {attr.key for attr in state.mapper.column_attrs}
        except Exception:
            continue
        # only values already loaded (reading an expired attribute here would query the database), and for
        # rows already stored only the attributes changed now: a stored value cannot hold NUL, and promotion
        # changes only status fields on rows that carry whole judgments.
        for key, value in list(state.dict.items()):
            if key not in keys:
                continue
            if obj not in new and not state.attrs[key].history.added:
                continue
            if _has_nul(value):
                setattr(obj, key, _strip_nul(value))


from sqlalchemy import event as _sa_event  # noqa: E402
from sqlalchemy.orm import Session as _SyncSession  # noqa: E402

_sa_event.listen(_SyncSession, "before_flush", _strip_nul_before_flush)


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


async def ensure_statute_canonical_name_key_index_valid() -> None:
    """Rebuild ix_statute_canonical_name_key when missing or INVALID; no-op if column absent."""
    async with engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        has_col = (
            await conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name = 'statute'
                      AND column_name = 'canonical_name_key'
                    LIMIT 1
                    """
                )
            )
        ).first()
        if not has_col:
            return
        row = (
            await conn.execute(
                text(
                    """
                    SELECT i.indisvalid
                    FROM pg_class c
                    JOIN pg_index i ON i.indexrelid = c.oid
                    JOIN pg_class t ON t.oid = i.indrelid
                    WHERE c.relname = 'ix_statute_canonical_name_key'
                      AND t.relname = 'statute'
                    LIMIT 1
                    """
                )
            )
        ).first()
        if row is not None and not row.indisvalid:
            await conn.execute(text("DROP INDEX CONCURRENTLY IF EXISTS ix_statute_canonical_name_key"))
            await conn.execute(
                text(
                    "CREATE INDEX CONCURRENTLY ix_statute_canonical_name_key ON statute (canonical_name_key)"
                )
            )
            return
        if row is None:
            await conn.execute(
                text(
                    """
                    CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_statute_canonical_name_key
                        ON statute (canonical_name_key)
                    """
                )
            )


async def run_migrations() -> List[str]:
    """Apply every migrations/NNN_*.py whose version is not yet recorded. Returns the list applied."""
    await ensure_statute_canonical_name_key_index_valid()
    applied_now: List[str] = []
    async with engine.begin() as conn:
        done = await _applied_versions(conn)
    for name in discover_migrations():
        if name in done:
            continue
        module = importlib.import_module(f"migrations.{name}")
        post_commit = getattr(module, "upgrade_post_commit", None)
        if post_commit is not None:
            async with engine.begin() as conn:
                await module.upgrade(conn)
            await post_commit()
            async with engine.begin() as conn:
                await conn.execute(
                    text("INSERT INTO schema_migrations(version) VALUES (:v) ON CONFLICT DO NOTHING"),
                    {"v": name},
                )
        else:
            async with engine.begin() as conn:
                await module.upgrade(conn)
                await conn.execute(
                    text("INSERT INTO schema_migrations(version) VALUES (:v) ON CONFLICT DO NOTHING"),
                    {"v": name},
                )
        applied_now.append(name)
        logger.info("migration applied: %s", name)
    return applied_now


# --------------------------------------------------------------------------- roles
def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"unsafe identifier {name!r}")
    return '"' + name + '"'


_READER_ROLE_BOOL_COLS = (
    "rolsuper",
    "rolcreatedb",
    "rolcreaterole",
    "rolinherit",
    "rolreplication",
    "rolbypassrls",
)
_READER_ROLE_DESIRED = {
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolinherit": False,
    "rolreplication": False,
    "rolbypassrls": False,
}
_SUPERUSER_ONLY_READER_DEMOTE_COLS = frozenset(
    {"rolsuper", "rolcreaterole", "rolreplication", "rolbypassrls"}
)


async def _connection_is_superuser(conn) -> bool:
    row = await conn.execute(text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user"))
    return bool(row.scalar())


async def _alter_reader_role_login(conn, role_ident: str, reader_pw: str) -> None:
    """Rotate password and align reader safety attributes; idempotent ALTER fragments only."""
    row = (
        await conn.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls "
                "FROM pg_roles WHERE rolname = :r"
            ),
            {"r": settings.SIKANDER_READER_ROLE},
        )
    ).one()
    current = {col: bool(row[i]) for i, col in enumerate(_READER_ROLE_BOOL_COLS)}
    caller_super = await _connection_is_superuser(conn)
    clauses = ["LOGIN", f"PASSWORD {_pg_literal(reader_pw)}"]
    attr_tokens = {
        "rolsuper": ("SUPERUSER", "NOSUPERUSER"),
        "rolcreatedb": ("CREATEDB", "NOCREATEDB"),
        "rolcreaterole": ("CREATEROLE", "NOCREATEROLE"),
        "rolinherit": ("INHERIT", "NOINHERIT"),
        "rolreplication": ("REPLICATION", "NOREPLICATION"),
        "rolbypassrls": ("BYPASSRLS", "NOBYPASSRLS"),
    }
    for col in _READER_ROLE_BOOL_COLS:
        want = _READER_ROLE_DESIRED[col]
        if current[col] == want:
            continue
        if not want and col in _SUPERUSER_ONLY_READER_DEMOTE_COLS and not caller_super:
            raise RoleAttributeChangeRequiresSuperuserError(
                f"cannot set {attr_tokens[col][1]} on {settings.SIKANDER_READER_ROLE!r}: "
                "connected role is not a superuser"
            )
        clauses.append(attr_tokens[col][0] if want else attr_tokens[col][1])
    await conn.execute(text(f"ALTER ROLE {role_ident} WITH {' '.join(clauses)}"))


async def ensure_roles() -> dict:
    """Create/refresh corpus_writer and sikander_reader. Idempotent; safe to call on every start."""
    return await _ensure_roles_locked()


_run_ensure_roles = ensure_roles


async def _verification_schema_exists(conn) -> bool:
    return bool(
        (
            await conn.execute(
                text("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'verification'")
            )
        ).scalar()
    )


async def _apply_role_grants(lock_conn, status: dict) -> None:
    from scraper.models import CONTRACT_TABLES, INTERNAL_TABLES

    reader_pw = settings.SIKANDER_READER_PASSWORD.get_secret_value()
    writer_pw = settings.CORPUS_WRITER_PASSWORD.get_secret_value()
    await _pg_advisory_xact_lock(lock_conn, PG_ADVISORY_ROLE_SETUP_KEY1, PG_ADVISORY_ROLE_SETUP_KEY2)
    dbname = (await lock_conn.execute(text("SELECT current_database()"))).scalar()
    if writer_pw:
        role = _quote_ident(settings.CORPUS_WRITER_ROLE)
        exists = (await lock_conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.CORPUS_WRITER_ROLE})).scalar()
        if not exists:
            await lock_conn.execute(text(f"CREATE ROLE {role} LOGIN PASSWORD :p".replace(":p", _pg_literal(writer_pw))))
        else:
            await lock_conn.execute(text(f"ALTER ROLE {role} WITH LOGIN PASSWORD {_pg_literal(writer_pw)}"))
        await lock_conn.execute(text(f"GRANT CONNECT ON DATABASE {_quote_ident(dbname)} TO {role}"))
        await lock_conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await lock_conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA public TO {role}"))
        await lock_conn.execute(text(f"GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO {role}"))
        if await _verification_schema_exists(lock_conn):
            await lock_conn.execute(text(f"GRANT USAGE ON SCHEMA verification TO {role}"))
            await lock_conn.execute(text(f"GRANT ALL ON ALL TABLES IN SCHEMA verification TO {role}"))
        status["writer"] = "CONFIGURED"
    if reader_pw:
        role = _quote_ident(settings.SIKANDER_READER_ROLE)
        exists = (await lock_conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.SIKANDER_READER_ROLE})).scalar()
        if not exists:
            await lock_conn.execute(
                text(
                    f"CREATE ROLE {role} LOGIN PASSWORD {_pg_literal(reader_pw)} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS"
                )
            )
        else:
            await _alter_reader_role_login(lock_conn, role, reader_pw)
        await lock_conn.execute(text(f"GRANT CONNECT ON DATABASE {_quote_ident(dbname)} TO {role}"))
        await lock_conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {role}"))
        await lock_conn.execute(text(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role}"))
        await lock_conn.execute(text(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role}"))
        await lock_conn.execute(text(f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role}"))
        await lock_conn.execute(text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role}"))
        for t in CONTRACT_TABLES:
            await lock_conn.execute(text(f"GRANT SELECT ON TABLE {_quote_ident(t)} TO {role}"))
        for t in INTERNAL_TABLES:
            await lock_conn.execute(text(f"REVOKE ALL ON TABLE {_quote_ident(t)} FROM {role}"))
        for t in INTERNAL_TABLES:
            await lock_conn.execute(text(f"REVOKE ALL ON TABLE {_quote_ident(t)} FROM PUBLIC"))
        status["reader"] = "CONFIGURED"
    verifier_pw = settings.CORPUS_VERIFIER_PASSWORD.get_secret_value()
    if verifier_pw and await _verification_schema_exists(lock_conn):
        vrole = _quote_ident(settings.CORPUS_VERIFIER_ROLE)
        exists = (
            await lock_conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname=:r"), {"r": settings.CORPUS_VERIFIER_ROLE})
        ).scalar()
        if not exists:
            await lock_conn.execute(
                text(
                    f"CREATE ROLE {vrole} LOGIN PASSWORD {_pg_literal(verifier_pw)} "
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT "
                    "NOREPLICATION NOBYPASSRLS"
                )
            )
        else:
            await lock_conn.execute(text(f"ALTER ROLE {vrole} WITH LOGIN PASSWORD {_pg_literal(verifier_pw)}"))
        await lock_conn.execute(text(f"GRANT CONNECT ON DATABASE {_quote_ident(dbname)} TO {vrole}"))
        await lock_conn.execute(text(f"REVOKE ALL ON SCHEMA public FROM {vrole}"))
        await lock_conn.execute(text(f"GRANT USAGE ON SCHEMA verification TO {vrole}"))
        await lock_conn.execute(text(f"GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA verification TO {vrole}"))
        await lock_conn.execute(
            text(f"ALTER DEFAULT PRIVILEGES IN SCHEMA verification GRANT SELECT, INSERT, UPDATE ON TABLES TO {vrole}")
        )
        for t in CONTRACT_TABLES:
            await lock_conn.execute(text(f"REVOKE ALL ON TABLE {_quote_ident(t)} FROM {vrole}"))
        status["verifier"] = "CONFIGURED"


async def _ensure_roles_locked() -> dict:
    status = {"reader": "NOT CONFIGURED", "writer": "NOT CONFIGURED"}
    lock_url = _advisory_lock_database_url()
    use_catalog = lock_url != str(settings.DATABASE_URL)
    lock_engine = create_async_engine(lock_url, **_engine_kwargs()) if use_catalog else engine
    dispose_lock_engine = use_catalog
    try:
        async with lock_engine.connect() as cluster_lock_conn:
            await cluster_lock_conn.execute(
                text("SELECT pg_advisory_lock(:k)"),
                {"k": PG_ADVISORY_ROLE_SETUP_CLUSTER_LOCK},
            )
            await cluster_lock_conn.commit()
            try:
                if use_catalog:
                    async with engine.connect() as lock_conn:
                        await _apply_role_grants(lock_conn, status)
                        await lock_conn.commit()
                else:
                    await _apply_role_grants(cluster_lock_conn, status)
                    await cluster_lock_conn.commit()
            except Exception:
                await cluster_lock_conn.rollback()
                raise
            finally:
                await cluster_lock_conn.rollback()
                await cluster_lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:k)"),
                    {"k": PG_ADVISORY_ROLE_SETUP_CLUSTER_LOCK},
                )
    finally:
        if dispose_lock_engine:
            await lock_engine.dispose()
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


async def _seed_courts(db_engine=None) -> None:
    """Insert court seed rows under a transaction advisory lock; conflicts on any unique key are ignored."""
    from sqlalchemy.dialects.postgresql import insert

    from scraper.models import Court

    if not COURT_SEED:
        return
    eng = db_engine if db_engine is not None else engine
    async with eng.begin() as conn:
        await _pg_advisory_xact_lock(conn, PG_ADVISORY_COURT_SEED_KEY1, PG_ADVISORY_COURT_SEED_KEY2)
        court_rows = (
            await conn.execute(text("SELECT short_code, name, court_level, city FROM court"))
        ).mappings().all()
        existing = {row["short_code"]: row for row in court_rows}
        for name, code, level, prov, city, aliases in COURT_SEED:
            row = existing.get(code)
            if row is None:
                continue
            if (row["name"], row["court_level"], row["city"]) != (name, level, city):
                logger.warning(
                    "court seed short_code=%s already present as name=%r level=%r city=%r; "
                    "seed expects name=%r level=%r city=%r (insert skipped on conflict)",
                    code,
                    row["name"],
                    row["court_level"],
                    row["city"],
                    name,
                    level,
                    city,
                )
        await conn.execute(
            insert(Court)
            .values(
                [
                    {
                        "name": name,
                        "short_code": code,
                        "court_level": level,
                        "jurisdiction_province": prov,
                        "city": city,
                        "aliases": aliases,
                    }
                    for name, code, level, prov, city, aliases in COURT_SEED
                ]
            )
            .on_conflict_do_nothing()
        )


async def seed_data(db_engine=None) -> None:
    from sqlalchemy import select

    from scraper.models import ArchiveTarget, BrowserSessionSlot, Court, CorpusMetadata, ScraperSource

    eng = db_engine if db_engine is not None else engine
    await _seed_courts(eng)
    if db_engine is None:
        session_factory = SessionLocal
    else:
        session_factory = async_sessionmaker(eng, expire_on_commit=False, class_=AsyncSession)
    async with session_factory() as db:
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
                    crawl_max_pages=200 if name == "PakistanCode" else 50,
                    state="PAUSED" if method == "login_session" else "ACTIVE",
                    state_reason="Awaiting human login" if method == "login_session" else None,
                )
            )
        # Sources the owner retired stay as DISABLED rows (their records and history remain) and are
        # never dispatched; a new database seeds them the same way.
        from scraper.deploy_role import retired_sources

        retired = retired_sources()
        for row in list(existing_source_rows.values()) + [o for o in db.new if isinstance(o, ScraperSource)]:
            if row.source_name in retired and (row.state != "DISABLED" or row.is_active):
                row.state, row.is_active = "DISABLED", False
                row.state_reason = "retired on the owner's instruction (12 Oct 2026): the site blocks this service"
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
            if int(pakistan_code.crawl_max_pages or 0) < 200:
                pakistan_code.crawl_max_pages = 200
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
            "harvest_mode": settings.HARVEST_MODE,
            "harvest_mode_changed_at": datetime.now(timezone.utc).isoformat(),
            "harvest_mode_changed_by": "seed",
            "harvest_mode_reason": "initial seed",
            "service_started_at": datetime.now(timezone.utc).isoformat(),
        }
        existing_meta = {m.key: m for m in (await db.execute(select(CorpusMetadata))).scalars().all()}
        stored_mode = existing_meta.get("harvest_mode")
        stored_by = existing_meta.get("harvest_mode_changed_by")
        stored_reason = existing_meta.get("harvest_mode_reason")
        if (
            stored_mode is not None
            and stored_mode.value == "updates"
            and settings.HARVEST_MODE == "backfill"
            and stored_by is not None
            and stored_by.value == "system"
            and (stored_reason.value if stored_reason is not None else "").startswith("auto-switch")
            and not (settings.BACKFILL_TARGET_JUDGMENTS or settings.BACKFILL_TARGET_STATUTES)
        ):
            now_iso = datetime.now(timezone.utc).isoformat()
            stored_mode.value = "backfill"
            stored_by.value = "system"
            stored_reason.value = "repair: undid a targetless auto-switch to updates (see docs/AUDIT_2026-09-22.md F1)"
            changed_at = existing_meta.get("harvest_mode_changed_at")
            if changed_at is not None:
                changed_at.value = now_iso
            logger.warning("harvest_mode restored to backfill: the stored 'updates' came from a targetless auto-switch")
        for k, v in meta.items():
            if k in existing_meta:
                if k in ("embedding_model", "embedding_dim") and existing_meta[k].value != v:
                    # Do not silently change the recorded embedding identity; the embedding worker refuses to run.
                    logger.error("corpus_metadata %s=%s differs from configured %s; embedding worker will refuse", k, existing_meta[k].value, v)
                    continue
                if k in ("harvest_mode", "harvest_mode_changed_at", "harvest_mode_changed_by", "harvest_mode_reason"):
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


async def _init_db_unlock(conn) -> None:
    await conn.execute(
        text("SELECT pg_advisory_unlock(:k1, :k2)"),
        {"k1": PG_ADVISORY_INIT_DB_KEY1, "k2": PG_ADVISORY_INIT_DB_KEY2},
    )


async def _init_db_lock(conn) -> None:
    await conn.execute(
        text("SELECT pg_advisory_lock(:k1, :k2)"),
        {"k1": PG_ADVISORY_INIT_DB_KEY1, "k2": PG_ADVISORY_INIT_DB_KEY2},
    )
    await conn.commit()


async def _init_db_work(ensure_roles: bool) -> dict:
    async with engine.connect() as lock_conn:
        async with lock_conn.begin():
            await lock_conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await lock_conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            await lock_conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp"'))
    applied = await run_migrations()
    roles = (
        await _run_ensure_roles()
        if ensure_roles
        else {"reader": "NOT CONFIGURED", "writer": "NOT CONFIGURED"}
    )
    await seed_data()
    logger.info("database initialised; migrations applied now: %s; roles: %s", applied, roles)
    return {"migrations_applied": applied, "roles": roles}


async def init_db(ensure_roles: bool = True) -> dict:
    """Extensions → migrations → roles → seed. Returns a status dictionary for /health."""
    lock_url = _advisory_lock_database_url()
    use_catalog = lock_url != str(settings.DATABASE_URL)
    lock_engine = create_async_engine(lock_url, **_engine_kwargs()) if use_catalog else engine
    dispose_lock_engine = use_catalog
    try:
        async with lock_engine.connect() as init_lock_conn:
            await _init_db_lock(init_lock_conn)
            try:
                return await _init_db_work(ensure_roles)
            finally:
                await _init_db_unlock(init_lock_conn)
    finally:
        if dispose_lock_engine:
            await lock_engine.dispose()
