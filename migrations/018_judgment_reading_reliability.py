"""
018 — Harden unreported-judgment machine reading:
* retain the current validated reading as a serving snapshot,
* add immutable attempt/version history,
* add evidence and taxonomy provenance,
* backfill existing rows into the history table.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    for sql in (
        "ALTER TABLE judgment_reading ADD COLUMN IF NOT EXISTS taxonomy_version VARCHAR(40)",
        "ALTER TABLE judgment_reading ADD COLUMN IF NOT EXISTS judgment_text_hash VARCHAR(64)",
        "ALTER TABLE judgment_reading ADD COLUMN IF NOT EXISTS interpretation_evidence JSONB",
        "ALTER TABLE judgment_reading ADD COLUMN IF NOT EXISTS turns_on_evidence JSONB",
        "ALTER TABLE judgment_reading ADD COLUMN IF NOT EXISTS outcome_evidence JSONB",
    ):
        await conn.execute(text(sql))

    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS judgment_reading_version (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                judgment_id UUID NOT NULL REFERENCES judgment(id) ON DELETE CASCADE,
                status VARCHAR(20) NOT NULL,
                model VARCHAR(100),
                provider VARCHAR(255),
                prompt_version VARCHAR(40),
                taxonomy_version VARCHAR(40),
                judgment_text_hash VARCHAR(64),
                laws JSONB,
                points JSONB,
                interpretation TEXT,
                interpretation_evidence JSONB,
                turns_on TEXT,
                turns_on_evidence JSONB,
                subjects TEXT[],
                outcome VARCHAR(40),
                outcome_evidence JSONB,
                dropped JSONB,
                error TEXT,
                error_kind VARCHAR(40),
                attempt_no INTEGER NOT NULL,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                latency_ms INTEGER NOT NULL DEFAULT 0,
                chunk_count INTEGER NOT NULL DEFAULT 1,
                parse_repair_used BOOLEAN NOT NULL DEFAULT false,
                is_current BOOLEAN NOT NULL DEFAULT false,
                started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                retry_after_at TIMESTAMPTZ,
                CONSTRAINT uq_judgment_reading_version_attempt UNIQUE (judgment_id, attempt_no)
            )
            """
        )
    )
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_reading_version_judgment ON judgment_reading_version (judgment_id, attempt_no)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_reading_version_status ON judgment_reading_version (status, completed_at)"))
    await conn.execute(text("CREATE INDEX IF NOT EXISTS ix_judgment_reading_version_current ON judgment_reading_version (judgment_id, is_current)"))
    await conn.execute(
        text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_judgment_reading_version_current "
            "ON judgment_reading_version (judgment_id) WHERE is_current"
        )
    )

    # Preserve every pre-migration row as attempt 1. Only a successful legacy reading is current.
    await conn.execute(
        text(
            """
            INSERT INTO judgment_reading_version (
                judgment_id, status, model, prompt_version, judgment_text_hash,
                laws, points, interpretation, turns_on, subjects, outcome, dropped, error,
                attempt_no, is_current, started_at, completed_at
            )
            SELECT
                r.judgment_id, r.status, r.model, r.prompt_version, j.full_text_hash,
                r.laws, r.points, r.interpretation, r.turns_on, r.subjects, r.outcome, r.dropped, r.error,
                1, (r.status = 'read'), r.read_at, r.read_at
            FROM judgment_reading r
            JOIN judgment j ON j.id = r.judgment_id
            WHERE NOT EXISTS (
                SELECT 1 FROM judgment_reading_version v WHERE v.judgment_id = r.judgment_id
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            UPDATE judgment_reading
            SET judgment_text_hash = j.full_text_hash
            FROM judgment j
            WHERE judgment_reading.judgment_id = j.id
              AND judgment_reading.judgment_text_hash IS NULL
            """
        )
    )
