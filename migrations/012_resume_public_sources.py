"""
012 — resume the public sources alongside PakistanLawSite (operator instruction, 7 October 2026).

On 21 and 24 September the operator paused every public source to focus on PakistanLawSite
("dual-track only ...", "Shamail PLS-only focus ..."). On 7 October the operator asked for
PakistanCode, and then the other public sites, to run side by side with the PakistanLawSite
scraper. Public sources run on worker-public (queue `scraper`, two at a time), never on the
PakistanLawSite login worker, so they cannot slow it.

Only sources paused for those two reasons are resumed. A source HALTED by an explicit block
(BalochistanHighCourt, NasirLawSite) or paused for any other reason is left alone. PakistanCode
is due at once; the others are spread 30 minutes apart so the server is not loaded all at once.
"""

from __future__ import annotations

from sqlalchemy import text

FOCUS_PAUSES = ("dual-track only%", "Shamail PLS-only focus%")
REASON = "resumed 7 Oct 2026 on operator instruction: public sources run alongside PakistanLawSite"


async def upgrade(conn) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT source_name FROM scraper_sources WHERE source_name <> 'PakistanLawSite' AND state = 'PAUSED'"
                " AND (state_reason LIKE :a OR state_reason LIKE :b) ORDER BY (source_name = 'PakistanCode') DESC, source_name"
            ),
            {"a": FOCUS_PAUSES[0], "b": FOCUS_PAUSES[1]},
        )
    ).scalars().all()
    for i, name in enumerate(rows):
        await conn.execute(
            text(
                "UPDATE scraper_sources SET state = 'ACTIVE', is_active = true, state_reason = :reason,"
                " state_changed_at = now(), next_scrape_at = now() + make_interval(mins => :delay)"
                " WHERE source_name = :name"
            ),
            {"reason": REASON, "delay": 30 * i, "name": name},
        )
