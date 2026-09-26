import pytest

from scraper.models import CrawlFrontier
from scraper.tasks.pls_self_healing import reset_retired_pls_search_map_frontier_db


@pytest.mark.asyncio
async def test_reset_retired_frontier_idempotent(db, login_source):
    db.add(
        CrawlFrontier(
            source_name="PakistanLawSite",
            tier=1,
            query_key="t1:PLD:2020",
            query_json={"reporter": "PLD", "year": 2020},
            cursor_json={"page_no": 1},
            status="retired",
            last_error="search map cannot express reporter citation query; missing usable roles: reporter",
        )
    )
    await db.flush()
    first = await reset_retired_pls_search_map_frontier_db(db)
    assert first == 1
    second = await reset_retired_pls_search_map_frontier_db(db)
    assert second == 0
