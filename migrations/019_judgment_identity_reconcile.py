"""
019 — Non-destructive reported/unreported identity links and review flags (B2).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS judgment_identity_link (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                unreported_judgment_id UUID NOT NULL REFERENCES judgment(id) ON DELETE CASCADE,
                reported_judgment_id UUID NOT NULL REFERENCES judgment(id) ON DELETE CASCADE,
                source VARCHAR(40) NOT NULL DEFAULT 'reconcile',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                CONSTRAINT uq_judgment_identity_link_pair UNIQUE (unreported_judgment_id, reported_judgment_id)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_identity_link_unreported ON judgment_identity_link (unreported_judgment_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_identity_link_reported ON judgment_identity_link (reported_judgment_id)"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS judgment_identity_review (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                unreported_judgment_id UUID NOT NULL REFERENCES judgment(id) ON DELETE CASCADE,
                reported_judgment_id UUID REFERENCES judgment(id) ON DELETE SET NULL,
                reason VARCHAR(200) NOT NULL,
                details JSONB,
                reviewed BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_identity_review_unreported ON judgment_identity_review (unreported_judgment_id)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_identity_review_reviewed ON judgment_identity_review (reviewed)"))


async def downgrade(conn) -> None:
    await conn.execute(text("DROP TABLE IF EXISTS judgment_identity_review"))
    await conn.execute(text("DROP TABLE IF EXISTS judgment_identity_link"))
