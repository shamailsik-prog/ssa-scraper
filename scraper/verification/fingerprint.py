"""Optional source re-fetch fingerprint (never writes corpus tables)."""

from __future__ import annotations

import random
from typing import Optional, Tuple

from scraper.auth.session_manager import SessionLock
from scraper.config import settings
from scraper.fetchers import canonical_text_hash
from scraper.models import Judgment
from scraper.tasks.pakistanlawsite import SOURCE_NAME
from scraper.tasks.spot_check import judgment_spot_check_hashes_match, judgment_spot_check_stored_text


async def pls_harvest_slot_busy(redis_client=None, *, slot: int = 1) -> bool:
    lock = SessionLock(f"{SOURCE_NAME}:search_harvest:slot{slot}", redis_client, max_holders=1)
    r = await lock._client()
    key = lock.exclusive_key
    busy = bool(await r.exists(key))
    if redis_client is None and lock._own_client:
        await r.aclose()
    return busy


def fingerprint_sample_pass() -> bool:
    rate = float(settings.CORPUS_VERIFIER_PASS_SAMPLE_RATE or 0)
    return rate > 0 and random.random() < rate


async def compare_judgment_fingerprint(j: Judgment, live_text: str) -> Tuple[str, str]:
    if judgment_spot_check_hashes_match(j, live_text):
        return "match", "normalized text fingerprint matches stored body"
    stored = judgment_spot_check_stored_text(j)
    return (
        "mismatch",
        f"stored_hash={canonical_text_hash(stored)[:12]} live_hash={canonical_text_hash(live_text)[:12]}",
    )
