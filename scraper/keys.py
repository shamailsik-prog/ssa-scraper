"""Fit a lookup key into its VARCHAR column the same way everywhere it is built or bound."""

from __future__ import annotations

import hashlib

QUERY_KEY_LENGTH = 500


def fit_key(value: str, limit: int = QUERY_KEY_LENGTH) -> str:
    """A value longer than the column becomes its head plus a SHA-256 of the full value, so it fits, stays stable
    and stays distinct from keys that share a long prefix. Code that compares keys in Python must build them through
    this too, since the database holds the shortened form."""
    if limit and len(value) > limit:
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        return f"{value[: limit - len(digest) - 1]}#{digest}"
    return value
