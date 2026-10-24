"""028 — read-only corpus record verifier results (separate schema; no corpus writes)."""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(text("CREATE SCHEMA IF NOT EXISTS verification"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS verification.verification_results (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                record_kind VARCHAR(20) NOT NULL,
                record_id UUID NOT NULL,
                canonical_key VARCHAR(500),
                journal_year INTEGER,
                verdict VARCHAR(8) NOT NULL,
                reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
                rules_version VARCHAR(32) NOT NULL,
                checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                source_fingerprint_status VARCHAR(32),
                source_fingerprint_detail VARCHAR(1000),
                CONSTRAINT uq_verification_results_record_rules
                    UNIQUE (record_kind, record_id, rules_version)
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_verification_results_verdict_checked
                ON verification.verification_results (verdict, checked_at DESC)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_verification_results_journal_year
                ON verification.verification_results (journal_year, verdict)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS verification.verification_run_state (
                stream VARCHAR(64) PRIMARY KEY,
                last_record_id UUID,
                rules_version VARCHAR(32) NOT NULL,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(text("DROP TABLE IF EXISTS verification.verification_run_state"))
    await conn.execute(text("DROP TABLE IF EXISTS verification.verification_results"))
    await conn.execute(text("DROP SCHEMA IF EXISTS verification"))
