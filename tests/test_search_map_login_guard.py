from __future__ import annotations

import pytest

from scraper.auth.session_manager import LoginRequired
from scraper.tasks.search_map import active_map, map_search_form


MAINPAGE_HTML = """<html><body><form id="mainLoginForm">
<input name="Login.UserName"><input type="password" name="Login.Password">
</form></body></html>"""


@pytest.mark.asyncio
async def test_login_page_does_not_overwrite_active_search_map(db, login_source):
    first = await map_search_form(
        db,
        login_source,
        '<html><body><form><input name="reporter"><input name="year"></form></body></html>',
        page_url="https://www.pakistanlawsite.com/Login/CitationSearch",
    )
    version = first.map_version
    bounced = await map_search_form(
        db,
        login_source,
        MAINPAGE_HTML,
        page_url="https://www.pakistanlawsite.com/Login/MainPage",
    )
    assert bounced.map_version == version
    active = await active_map(db, login_source.source_name)
    assert active is not None and active.map_version == version


@pytest.mark.asyncio
async def test_login_page_without_prior_map_raises(db, login_source):
    with pytest.raises(LoginRequired):
        await map_search_form(
            db,
            login_source,
            MAINPAGE_HTML,
            page_url="https://www.pakistanlawsite.com/Login/MainPage",
        )
