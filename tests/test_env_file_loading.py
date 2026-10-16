"""The deploy scripts never source or export docker compose's .env (16 October 2026).

auto_deploy.sh and install.sh --deploy-only did `set -a; source .env` under `set -euo pipefail`. Docker compose's .env is
KEY=VALUE text with compose's own rules: a value with a space, a quote, a `$` or a bracket is fine for compose but makes
bash run a command or expand a variable, and the script exits. On the free-sites server every auto-deploy run since at
least 12 October stopped right there, before it fetched.

Exporting the values instead (the first fix) was wrong too (Codex review, #225): exported variables override .env for
`docker compose`, and compose strips inline comments and interpolates `${VAR}`, so an exported literal COMPOSE_FILE with
a comment would break every deployment. Compose keeps reading its own .env; the scripts read only the two keys they use.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "env_file.sh"

COMPOSE_ENV = (
    "# comment\n"
    "\n"
    "POSTGRES_USER=legal1\n"
    "PROJECT_NAME=SIKANDER AI corpus\n"
    'ARCHIVE_JSON={"folder": "Corpus (public)"}\n'
    "PASSWORD=ab$cd(ef)&gh\n"
    "COMPOSE_FILE=docker-compose.yml:docker-compose.cloud.yml # cloud\n"
    'QUOTED="x y" # note\n'
    "SINGLE='p q'\n"
    "EMPTY=\n"
    "CRLF_VALUE=r\r\n"
    "export EXPORTED=e1\n"
    "SPACED = s1\n"
    "not a key line\n"
    "POSTGRES_USER=legal2\n"  # the last assignment wins, as in compose
    "POSTGRES_DB=legal_scraper_x"  # last line without a newline
)


def _shell(script: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", "-c", script], cwd=cwd, capture_output=True, text=True)


def _value(tmp_path: Path, key: str) -> str:
    res = _shell(f'set -euo pipefail; source "{HELPER}"; printf "[%s]" "$(env_file_value .env {key})"', tmp_path)
    assert res.returncode == 0, res.stderr
    return res.stdout


def test_sourcing_a_compose_env_as_shell_kills_a_strict_script(tmp_path):
    """The failure the scripts had: this is why every run stopped before the fetch."""
    (tmp_path / ".env").write_text(COMPOSE_ENV)
    res = _shell('set -euo pipefail; set -a; source .env; set +a; echo REACHED', tmp_path)
    assert "REACHED" not in res.stdout


def test_env_file_value_reads_one_value_as_data_under_strict_mode(tmp_path):
    (tmp_path / ".env").write_text(COMPOSE_ENV)
    assert _value(tmp_path, "POSTGRES_USER") == "[legal2]"
    assert _value(tmp_path, "POSTGRES_DB") == "[legal_scraper_x]"
    assert _value(tmp_path, "PROJECT_NAME") == "[SIKANDER AI corpus]"
    assert _value(tmp_path, "PASSWORD") == "[ab$cd(ef)&gh]"
    assert _value(tmp_path, "COMPOSE_FILE") == "[docker-compose.yml:docker-compose.cloud.yml]"
    assert _value(tmp_path, "QUOTED") == "[x y]"
    assert _value(tmp_path, "SINGLE") == "[p q]"
    assert _value(tmp_path, "EMPTY") == "[]"
    assert _value(tmp_path, "CRLF_VALUE") == "[r]"
    assert _value(tmp_path, "EXPORTED") == "[e1]"
    assert _value(tmp_path, "SPACED") == "[s1]"
    assert _value(tmp_path, "MISSING") == "[]"


def test_env_file_value_without_a_file_is_empty(tmp_path):
    assert _value(tmp_path, "POSTGRES_USER") == "[]"


def test_reading_env_exports_nothing(tmp_path):
    """Compose must keep reading its own .env: nothing may reach the environment of `docker compose`."""
    (tmp_path / ".env").write_text(COMPOSE_ENV)
    res = _shell(
        f'set -euo pipefail; source "{HELPER}"; u="$(env_file_value .env POSTGRES_USER)"; '
        'env | grep -E "^(COMPOSE_FILE|POSTGRES_USER|PROJECT_NAME)=" || echo NONE',
        tmp_path,
    )
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == "NONE"


def test_deploy_scripts_never_source_or_export_env():
    auto = (ROOT / "scripts" / "auto_deploy.sh").read_text()
    install = (ROOT / "cloud" / "install.sh").read_text()
    for name, text in (("auto_deploy.sh", auto), ("install.sh", install)):
        assert "source .env" not in text, name
        assert "load_env_file" not in text, name
    assert 'env_file_value .env POSTGRES_USER' in auto and 'env_file_value .env POSTGRES_DB' in auto
    deploy_only = install[install.index('if [ "$DEPLOY_ONLY" = 1 ]; then'):]
    deploy_only = deploy_only[: deploy_only.index("\nfi\n")]
    code = [ln for ln in deploy_only.splitlines() if not ln.lstrip().startswith("#")]
    assert not [ln for ln in code if ".env" in ln]  # compose reads .env itself; the installer needs nothing from it here
    for rel in ("scripts/auto_deploy.sh", "cloud/install.sh", "scripts/env_file.sh"):
        subprocess.run(["bash", "-n", str(ROOT / rel)], check=True)
