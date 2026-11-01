"""029 — Store sort_key on the re-extract capture.

028 already applied on databases that ran the first version of that migration
has no sort_key column, and its trigger functions do not write one. Fresh
installs create the column in 028; this migration adds it if it is missing
and replaces the trigger functions either way.
"""

from __future__ import annotations

from sqlalchemy import text


_SECTION_FN = """
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

_VERSION_FN = """
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


async def upgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            ALTER TABLE statute_section_reextract_prior
                ADD COLUMN IF NOT EXISTS sort_key INTEGER
            """
        )
    )
    await conn.execute(text(_SECTION_FN))
    await conn.execute(text(_VERSION_FN))


async def downgrade(conn) -> None:
    await conn.execute(text("SET LOCAL lock_timeout = '2s'"))
    await conn.execute(
        text(
            """
            CREATE OR REPLACE FUNCTION capture_statute_section_reextract_prior()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                INSERT INTO statute_section_reextract_prior (
                    section_id, statute_id, section_number, section_text
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
                       )
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
            BEGIN
                SELECT s.id, s.section_number, s.statute_id
                  INTO sid, snum, stid
                FROM statute_section s
                WHERE s.id = OLD.section_id
                  AND s.current_version_id = OLD.id;
                IF sid IS NULL THEN
                    RETURN OLD;
                END IF;
                INSERT INTO statute_section_reextract_prior (
                    section_id, statute_id, section_number, section_text
                )
                VALUES (sid, stid, snum, OLD.section_text)
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
            ALTER TABLE statute_section_reextract_prior
                DROP COLUMN IF EXISTS sort_key
            """
        )
    )
