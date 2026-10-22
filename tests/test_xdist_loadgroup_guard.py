"""Guard: under xdist --dist loadgroup, nodeids must carry real @<file stem> suffixes."""

from __future__ import annotations

import os
import pathlib

import pytest

pytest_plugins = ("pytester",)


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


def test_loadgroup_keeps_each_file_on_one_worker(pytester: pytest.Pytester) -> None:
    """Inner -n2 --dist loadgroup: all tests in a file must share one worker."""
    pytester.makeconftest(
        """
import pathlib
import pytest

@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    for item in items:
        if item.get_closest_marker("xdist_group") is not None:
            continue
        item.add_marker(pytest.mark.xdist_group(pathlib.Path(item.path).stem))
"""
    )
    pytester.makepyfile(
        test_alpha="""
import os
from pathlib import Path

def _record(tag: str) -> None:
    root = Path(os.environ["LOADGROUP_GUARD_TMP"])
    path = root / f"{tag}_workers.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(os.environ.get("PYTEST_XDIST_WORKER", "master") + "\\n")

def test_alpha_one():
    _record("alpha")

def test_alpha_two():
    _record("alpha")
""",
        test_beta="""
import os
from pathlib import Path

def _record(tag: str) -> None:
    root = Path(os.environ["LOADGROUP_GUARD_TMP"])
    path = root / f"{tag}_workers.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(os.environ.get("PYTEST_XDIST_WORKER", "master") + "\\n")

def test_beta_one():
    _record("beta")

def test_beta_two():
    _record("beta")
""",
    )
    tmp = pytester.path / "worker_logs"
    tmp.mkdir()
    os.environ["LOADGROUP_GUARD_TMP"] = str(tmp)
    result = pytester.runpytest_subprocess("-n", "2", "--dist", "loadgroup")
    result.assert_outcomes(passed=4)

    alpha_workers = (tmp / "alpha_workers.txt").read_text(encoding="utf-8").strip().split()
    beta_workers = (tmp / "beta_workers.txt").read_text(encoding="utf-8").strip().split()
    assert len(alpha_workers) == 2
    assert len(beta_workers) == 2
    assert len(set(alpha_workers)) == 1, alpha_workers
    assert len(set(beta_workers)) == 1, beta_workers
