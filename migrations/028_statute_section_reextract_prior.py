"""028 — Keep section text when rows are deleted, for the re-extract gate.

A script that deletes statute_section (or the current version) before promote
would otherwise show an empty stored body, and gate_section_reextract would
treat that as a first ingest. These triggers capture the pre-delete text.
Promotion reads it. Downgrade drops the capture only; statute rows stay.
"""

from __future__ import annotations

from sqlalchemy import text


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS statute_section_reextract_prior (
                section_id UUID PRIMARY KEY,
                statute_id UUID NOT NULL,
                section_number VARCHAR(100) NOT NULL,
                section_text TEXT NOT NULL,
                sort_key INTEGER,
                captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE INDEX IF NOT EXISTS ix_statute_section_reextract_prior_statute_id
                ON statute_section_reextract_prior (statute_id)
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION capture_statute_section_reextract_prior()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                INSERT INTO statute_section_reextract_prior (
                    section_id, statute_id, section_number, section_text, sort_key
                )
                SELECT OLD.id,
                       OLD.statute_id,
                       OLD.section_number,
                       COALESCE(
                           (
                               SELECT section_text
                               FROM statute_section_version
                               WHERE id = OLD.current_version_id
                           ),
                           ''
                       ),
                       OLD.sort_key
                ON CONFLICT (section_id) DO NOTHING;
                RETURN OLD;
            END;
            $$
            """
        )
    )
    await conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION capture_statute_section_version_reextract_prior()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE
                sid uuid;
                snum varchar(100);
                stid uuid;
                skey integer;
            BEGIN
                SELECT s.id, s.section_number, s.statute_id, s.sort_key
                  INTO sid, snum, stid, skey
                FROM statute_section s
                WHERE s.id = OLD.section_id
                  AND s.current_version_id = OLD.id;
                IF sid IS NULL THEN
                    RETURN OLD;
                END IF;
                INSERT INTO statute_section_reextract_prior (
                    section_id, statute_id, section_number, section_text, sort_key
                )
                VALUES (sid, stid, snum, OLD.section_text, skey)
                ON CONFLICT (section_id) DO NOTHING;
                RETURN OLD;
            END;
            $$
            """
        )
    )
    await conn.execute(text("DROP TRIGGER IF EXISTS trg_statute_section_reextract_prior ON statute_section"))
    await conn.execute(
        text(
            """
            CREATE TRIGGER trg_statute_section_reextract_prior
            BEFORE DELETE ON statute_section
            FOR EACH ROW
            EXECUTE FUNCTION capture_statute_section_reextract_prior()
            """
        )
    )
    await conn.execute(
        text("DROP TRIGGER IF EXISTS trg_statute_section_version_reextract_prior ON statute_section_version")
    )
    await conn.execute(
        text(
            """
            CREATE TRIGGER trg_statute_section_version_reextract_prior
            BEFORE DELETE ON statute_section_version
            FOR EACH ROW
            EXECUTE FUNCTION capture_statute_section_version_reextract_prior()
            """
        )
    )


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(text("DROP TRIGGER IF EXISTS trg_statute_section_reextract_prior ON statute_section"))
    await conn.execute(
        text("DROP TRIGGER IF EXISTS trg_statute_section_version_reextract_prior ON statute_section_version")
    )
    await conn.execute(text("DROP FUNCTION IF EXISTS capture_statute_section_reextract_prior()"))
    await conn.execute(text("DROP FUNCTION IF EXISTS capture_statute_section_version_reextract_prior()"))
    await conn.execute(text("DROP TABLE IF EXISTS statute_section_reextract_prior"))
