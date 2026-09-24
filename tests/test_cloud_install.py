from __future__ import annotations

import re
import shlex
import subprocess
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "cloud" / "install.sh"


def _run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        cmd,
        cwd=str(cwd),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed


def _extract_shell_function(name: str) -> str:
    source = INSTALL_SH.read_text(encoding="utf-8")
    match = re.search(rf"(?ms)^{name}\(\) \{{\n.*?^}}\n", source)
    assert match, f"could not find shell function {name}"
    return match.group(0)


def test_install_stashes_local_server_edits_before_checkout():
    helper = _extract_shell_function("stash_local_server_edits")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        remote = tmp / "remote.git"
        seed = tmp / "seed"
        deploy = tmp / "deploy"

        _run(["git", "init", "--bare", str(remote)], cwd=tmp)
        _run(["git", "clone", str(remote), str(seed)], cwd=tmp)
        _run(["git", "config", "user.name", "Test User"], cwd=seed)
        _run(["git", "config", "user.email", "test@example.com"], cwd=seed)

        tracked = seed / "tracked.txt"
        tracked.write_text("base\n", encoding="utf-8")
        _run(["git", "add", "tracked.txt"], cwd=seed)
        _run(["git", "commit", "-m", "base"], cwd=seed)
        _run(["git", "branch", "-M", "main"], cwd=seed)
        _run(["git", "push", "-u", "origin", "main"], cwd=seed)

        _run(["git", "clone", "-b", "main", str(remote), str(deploy)], cwd=tmp)
        _run(["git", "config", "user.name", "Test User"], cwd=deploy)
        _run(["git", "config", "user.email", "test@example.com"], cwd=deploy)

        tracked.write_text("remote update\n", encoding="utf-8")
        _run(["git", "commit", "-am", "remote update"], cwd=seed)
        _run(["git", "push", "origin", "main"], cwd=seed)
        _run(["git", "fetch", "origin", "main"], cwd=deploy)

        (deploy / "tracked.txt").write_text("local edit\n", encoding="utf-8")
        blocked = subprocess.run(
            ["git", "checkout", "-q", "-B", "main", "FETCH_HEAD"],
            cwd=str(deploy),
            text=True,
            capture_output=True,
            check=False,
        )
        assert blocked.returncode != 0
        assert "would be overwritten by checkout" in (blocked.stdout + blocked.stderr)

        script = "\n".join(
            [
                "set -euo pipefail",
                "log() { :; }",
                f"DIR={shlex.quote(str(deploy))}",
                "BRANCH=main",
                helper.rstrip(),
                "stash_local_server_edits",
                'git -C "$DIR" checkout -q -B "$BRANCH" FETCH_HEAD',
                'git -C "$DIR" for-each-ref --format=\'%(refname)\' refs/server-edits',
            ]
        )
        completed = _run(["bash", "-lc", script], cwd=tmp)

        refs = completed.stdout.splitlines()
        assert any(ref.startswith("refs/server-edits/") for ref in refs)
        assert (deploy / "tracked.txt").read_text(encoding="utf-8") == "remote update\n"
        assert _run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=deploy).stdout == ""
        stash_show = _run(["git", "stash", "show", "--name-only", "refs/stash"], cwd=deploy)
        assert "tracked.txt" in stash_show.stdout
