"""
013 — clear the PakistanCode statutes and read every law again against its table of contents
(operator instruction, 7 October 2026: "all the statutes ... may be deleted and again scraped after due
verification").

The old reader named statutes after a sentence of their text ("This Act may be called ...", or even
another Act's name), took the CONTENTS headings for section bodies, and turned years into section
numbers. From this release a PakistanCode statute is read by scraper/parsers/statute_contents.py
and enters the corpus only when its listing title matches the document and every section in its
contents list was found with real text (promotion gate `pakistancode_contents_check`).

This migration, once:
1. copies the PakistanCode statutes, their sections and section versions into
   backup_20261007_statute / _statute_section / _statute_section_version (nothing is lost);
2. copies the links from instruments to those statutes (instrument_relation and
   instrument_section_relation rows) into backup_20261007_instrument_relation / _instrument_section_relation;
3. deletes the instrument links whose only target is one of those statutes (a link must point somewhere,
   ck_instrument_relation_target_present), then the statutes themselves (sections, versions and
   section-level links cascade; other references are set to NULL by their foreign keys); the relation
   reconcile task links instruments again as the re-read statutes arrive;
4. deletes PakistanCode's statute staging rows (their review-queue rows cascade), which would otherwise
   make the re-fetched documents look like duplicates;
5. sets every PakistanCode frontier row back to pending, so the next PakistanCode run fetches each law
   page and PDF again through the new reader.

Statutes from other sources (National Assembly, Senate, Gazette, provincial assemblies) are not touched
here: their documents have other layouts, and they are cleared the same way once a reader with the same
verification exists for them.
"""

from __future__ import annotations

from sqlalchemy import text

STEPS = (
    "CREATE TABLE IF NOT EXISTS backup_20261007_statute AS SELECT * FROM statute WHERE source_name = 'PakistanCode'",
    "CREATE TABLE IF NOT EXISTS backup_20261007_statute_section AS SELECT ss.* FROM statute_section ss"
    " JOIN statute s ON s.id = ss.statute_id WHERE s.source_name = 'PakistanCode'",
    "CREATE TABLE IF NOT EXISTS backup_20261007_statute_section_version AS SELECT v.* FROM statute_section_version v"
    " JOIN statute_section ss ON ss.id = v.section_id JOIN statute s ON s.id = ss.statute_id WHERE s.source_name = 'PakistanCode'",
    "CREATE TABLE IF NOT EXISTS backup_20261007_instrument_relation AS SELECT r.* FROM instrument_relation r"
    " JOIN statute s ON s.id = r.target_statute_id WHERE s.source_name = 'PakistanCode'",
    "CREATE TABLE IF NOT EXISTS backup_20261007_instrument_section_relation AS SELECT r.* FROM instrument_section_relation r"
    " JOIN statute s ON s.id = r.target_statute_id WHERE s.source_name = 'PakistanCode'",
    "DELETE FROM instrument_relation r USING statute s WHERE s.id = r.target_statute_id"
    " AND s.source_name = 'PakistanCode' AND r.target_instrument_id IS NULL",
    "DELETE FROM statute WHERE source_name = 'PakistanCode'",
    "DELETE FROM statutes_staging WHERE source_name = 'PakistanCode' AND kind = 'statute'",
    "UPDATE crawl_frontier SET status = 'pending', attempts = 0, last_error = NULL WHERE source_name = 'PakistanCode'",
)


async def upgrade(conn) -> None:
    for sql in STEPS:
        await conn.execute(text(sql))
