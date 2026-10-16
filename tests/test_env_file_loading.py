"""The deploy scripts read docker compose's .env as data, never as shell code (16 October 2026).

auto_deploy.sh and install.sh --deploy-only did `set -a; source .env` under `set -euo pipefail`. Docker compose's .env
is literal KEY=VALUE text: a value with a space, a quote, a `$` or a bracket is fine for compose but makes bash run a
command or expand a variable, and the script exits. On the free-sites server every auto-deploy run since at least 12
October stopped right there, before it fetched, and wrote nothing but its own .env pins to the log.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "env_file.sh"

COMPOSE_ENV = (
    "# comment\n"
    "\n"
    "POSTGRES_USER=legal2\n"
    "PROJECT_NAME=SIKANDER AI corpus\n"
    'ARCHIVE_JSON={"folder": "Corpus (public)"}\n'
    "PASSWORD=ab$cd(ef)&gh\n"
    'QUOTED="x y"\n'
    "SINGLE='p q'\n"
    "EMPTY=\n"
    "CRLF_VALUE=r\r\n"
    "not a key line\n"
    "POSTGRES_DB=legal_scraper_x"  # last line without a newline
)


def _shell(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


def test_sourcing_a_compose_env_as_shell_kills_a_strict_script(tmp_path):
    """The failure the scripts had: this is why every run stopped before the fetch."""
    (tmp_path / ".env").write_text(COMPOSE_ENV)
    res = _shell('set -euo pipefail; set -a; source .env; set +a; echo REACHED', tmp_path)
    assert "REACHED" not in res.stdout


def test_load_env_file_reads_values_literally_under_strict_mode(tmp_path):
    (tmp_path / ".env").write_text(COMPOSE_ENV)
    res = _shell(
        f'set -euo pipefail; source "{HELPER}"; load_env_file .env; '
        'printf "%s\\n" "$POSTGRES_USER" "$PROJECT_NAME" "$ARCHIVE_JSON" "$PASSWORD" "$QUOTED" "$SINGLE" "[$EMPTY]" "[$CRLF_VALUE]" "$POSTGRES_DB"; '
        'bash -c "printf %s \\"\\$POSTGRES_DB\\""',  # exported to child processes, as set -a did
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    lines = res.stdout.splitlines()
    assert lines[:9] == [
        "legal2",
        "SIKANDER AI corpus",
        '{"folder": "Corpus (public)"}',
        "ab$cd(ef)&gh",
        "x y",
        "p q",
        "[]",
        "[r]",
        "legal_scraper_x",
    ]
    assert lines[9] == "legal_scraper_x"


def test_load_env_file_without_a_file_is_a_no_op(tmp_path):
    res = _shell(f'set -euo pipefail; source "{HELPER}"; load_env_file .env; echo OK', tmp_path)
    assert res.returncode == 0 and res.stdout.strip() == "OK"


def test_deploy_scripts_load_env_with_the_helper_and_never_source_it():
    for rel in ("scripts/auto_deploy.sh", "cloud/install.sh"):
        text = (ROOT / rel).read_text()
        assert "source .env" not in text, rel
        assert "load_env_file .env" in text, rel
        assert "env_file.sh" in text, rel
        subprocess.run(["bash", "-n", str(ROOT / rel)], check=True)


def test_a_readonly_shell_variable_in_env_is_skipped_not_fatal(tmp_path):
    (tmp_path / ".env").write_text("UID=1000\nEUID=1000\nPOSTGRES_USER=legal3\n")
    res = _shell(f'set -euo pipefail; source "{HELPER}"; load_env_file .env; echo "$POSTGRES_USER"', tmp_path)
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "legal3"
