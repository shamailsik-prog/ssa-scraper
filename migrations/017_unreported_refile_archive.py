"""
017 — Archive copies of unreported judgments re-filed by 015 are written again under the corrected court.

015 corrected the court of unreported judgments in the database, but their archive copies (Drive, local, export)
stay complete under the old court's folder, and the mirror skips a judgment whose copies are complete. Every
archive record of an unreported judgment whose key is not under the judgment's current folder is marked
`superseded`, so the mirror writes a fresh set under `Unreported/<court>/...`. The old files are left where they
are (nothing in an archive is deleted); the superseded records name them.
"""

from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import text

from scraper.storage.archive import judgment_prefix


async def upgrade(conn) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT j.id, j.reporter, j.year, j.court_name, j.unreported, j.proceeding_type, j.canonical_citation, "
                "o.id AS object_id, o.object_key FROM judgment j JOIN archive_objects o ON o.judgment_id = j.id "
                "WHERE j.unreported AND o.status IN ('written', 'mismatch', 'missing')"
            )
        )
    ).mappings().all()
    stale = []
    for r in rows:
        prefix = judgment_prefix(SimpleNamespace(**{k: r[k] for k in ("reporter", "year", "court_name", "unreported", "proceeding_type", "canonical_citation")}))
        if not r["object_key"].startswith(prefix + "/"):
            stale.append(r["object_id"])
    for i in range(0, len(stale), 1000):
        await conn.execute(
            text("UPDATE archive_objects SET status = 'superseded', error = 'judgment re-filed under its court (migration 017)' WHERE id = ANY(:ids)"),
            {"ids": stale[i : i + 1000]},
        )
