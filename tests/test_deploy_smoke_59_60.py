from __future__ import annotations

from pathlib import Path


REQUIRED_ENV_KEYS = {
    "ENCRYPTION_KEY",
    "HARVEST_MODE",
    "HARVEST_AUTO_SWITCH",
    "UPDATE_CADENCE_HOURS",
    "BACKFILL_SOURCE_FREQUENCY_MINUTES",
    "BACKFILL_LOGIN_DELAY_MIN",
    "BACKFILL_LOGIN_DELAY_MAX",
    "BACKFILL_PAGES_PER_HOUR",
    "BACKFILL_PAGES_PER_DAY",
    "BACKFILL_LOGIN_SESSION_CONCURRENCY",
}


def _env_keys_from_example() -> set[str]:
    keys: set[str] = set()
    for raw in Path(".env.example").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        if key:
            keys.add(key)
    return keys


def test_required_env_keys_exist_in_env_example():
    keys = _env_keys_from_example()
    missing = sorted(REQUIRED_ENV_KEYS - keys)
    assert not missing, f"missing env keys in .env.example: {missing}"


def test_smoke_checklist_documents_controls():
    text = Path("docs/DEPLOY_SMOKE_59_60.md").read_text(encoding="utf-8")
    for token in (
        "HARVEST_MODE",
        "allow_list",
        "document_cdn_hosts",
        "ENCRYPTION_KEY",
        "slot `1` + slot `2`",
        "saved_credentials_used",
    ):
        assert token in text
