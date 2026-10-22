"""Guard: --dist loadgroup schedule ids must use per-file xdist_group markers."""

from __future__ import annotations

import pathlib


def _loadgroup_schedule_id(item) -> str:
    marker = item.get_closest_marker("xdist_group")
    assert marker is not None, item.nodeid
    return f"{item.nodeid}@{marker.args[0]}"


def test_every_test_loadgroup_id_ends_with_file_stem(request):
    for item in request.session.items:
        stem = pathlib.Path(item.path).stem
        marker = item.get_closest_marker("xdist_group")
        assert marker is not None, item.nodeid
        assert marker.args[0] == stem, item.nodeid
        schedule_id = _loadgroup_schedule_id(item)
        assert schedule_id.endswith(f"@{stem}"), schedule_id
