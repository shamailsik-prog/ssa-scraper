"""Unit tests for PakistanLawSite #archivedpatientGrid DataTables absolute seek."""

from __future__ import annotations

import subprocess
from pathlib import Path

from scraper.auth.session_manager import ARCHIVED_GRID_SEEK_JS, PlaywrightBrowser


REPO_ROOT = Path(__file__).resolve().parents[1]
SEEK_JS_PATH = REPO_ROOT / "scraper" / "auth" / "archived_grid_seek.js"
SEEK_NODE_TEST = REPO_ROOT / "tests" / "js" / "test_archived_grid_seek.js"


def test_archived_grid_seek_js_uses_datatable_page_and_ajax_start():
    source = SEEK_JS_PATH.read_text(encoding="utf-8")
    assert source == ARCHIVED_GRID_SEEK_JS
    assert "api().page(Math.floor(start/pageLength)).draw(false)" in source
    assert "api.page(Math.floor(boundedStart / pageLength)).draw(false)" in source
    assert "oAjaxData.start = start" in source


def test_archived_grid_seek_js_nonzero_start_row_and_seek_mode():
    completed = subprocess.run(
        ["node", str(SEEK_NODE_TEST)],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "all assertions passed" in completed.stdout


def test_playwright_snapshot_script_embeds_datatable_seek_helper():
    source = PlaywrightBrowser.__dict__["_capture_archived_grid_snapshot"].__code__.co_consts
    joined = "\n".join(part for part in source if isinstance(part, str))
    assert "seekArchivedGridAbsolute" in joined
    assert "seek_mode" in joined
    assert "requested_start_row" in joined
