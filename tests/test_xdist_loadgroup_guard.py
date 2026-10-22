"""Guard: under xdist --dist loadgroup, nodeids must carry real @<file stem> suffixes."""

from __future__ import annotations

import os
import pathlib

import pytest


def test_every_test_nodeid_ends_with_file_stem_under_loadgroup(request):
    """On xdist workers, xdist rewrites item.nodeid with @xdist_group (see xdist.remote)."""
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        pytest.skip("requires PYTEST_XDIST_WORKER (run under pytest-xdist)")
    if not request.config.getvalue("loadgroup"):
        pytest.skip("requires --dist loadgroup")

    for item in request.session.items:
        stem = pathlib.Path(item.path).stem
        marker = item.get_closest_marker("xdist_group")
        assert marker is not None, item.nodeid
        assert marker.args[0] == stem, item.nodeid
        assert item.nodeid.endswith(f"@{stem}"), item.nodeid
