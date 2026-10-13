"""
016 — The reading agent's readings of unreported judgments (scraper/tasks/unreported_reader.py).
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS judgment_reading (
                judgment_id UUID PRIMARY KEY REFERENCES judgment(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL,
                model VARCHAR(100),
                prompt_version VARCHAR(40),
                laws JSONB,
                points JSONB,
                interpretation TEXT,
                turns_on TEXT,
                subjects TEXT[],
                outcome VARCHAR(40),
                dropped JSONB,
                error TEXT,
                read_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_reading_status ON judgment_reading (status)"))
