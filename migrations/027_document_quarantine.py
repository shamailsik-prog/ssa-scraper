"""027 — document_quarantine side table (B7). Does not alter statute or judgment."""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS document_quarantine (
                doc_type VARCHAR(20) NOT NULL,
                doc_id UUID NOT NULL,
                reason VARCHAR(500) NOT NULL,
                prior_reason VARCHAR(500),
                run_id VARCHAR(64) NOT NULL,
                quarantined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (doc_type, doc_id)
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_document_quarantine_run_id
                ON document_quarantine (run_id)
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(text("DROP TABLE IF EXISTS document_quarantine"))
