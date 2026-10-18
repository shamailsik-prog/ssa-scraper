"""Dispositive-signal guard for reported/unreported automatic reconciliation (SSA_REPORTED_UNREPORTED_DISPOSITIVE_GUARD)."""

from __future__ import annotations

import re
from typing import FrozenSet, Optional, Set, Tuple

# Outcome families that must not be collapsed into one judgment row.
_OUTCOME_ALLOWED = re.compile(
    r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+allowed\b|\b(?:partly\s+)?allowed\b",
    re.IGNORECASE,
)
_OUTCOME_DISMISSED = re.compile(
    r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+dismissed\b|\b(?:partly\s+)?dismissed\b",
    re.IGNORECASE,
)
_OUTCOME_REMANDED = re.compile(r"\bremanded\b", re.IGNORECASE)
_OUTCOME_DISPOSED = re.compile(r"\bdisposed\s+of\b", re.IGNORECASE)
_MONEY_RE = re.compile(
    r"(?:rs\.?|pkr|rupees?)\s*([\d][\d,]*(?:\.\d+)?)\s*(?:lakh|lac|million|m)?",
    re.IGNORECASE,
)


def _normalise_amount(raw: str) -> int:
    digits = re.sub(r"[^\d.]", "", raw or "")
    if not digits:
        return 0
    value = float(digits)
    return int(round(value))


def extract_dispositive_outcomes(text: Optional[str]) -> FrozenSet[str]:
    if not text:
        return frozenset()
    found: Set[str] = set()
    if _OUTCOME_ALLOWED.search(text):
        found.add("allowed")
    if _OUTCOME_DISMISSED.search(text):
        found.add("dismissed")
    if _OUTCOME_REMANDED.search(text):
        found.add("remanded")
    if _OUTCOME_DISPOSED.search(text):
        found.add("disposed")
    return frozenset(found)


def extract_monetary_amounts(text: Optional[str]) -> FrozenSet[int]:
    if not text:
        return frozenset()
    amounts: Set[int] = set()
    for match in _MONEY_RE.finditer(text):
        base = _normalise_amount(match.group(1))
        if base <= 0:
            continue
        tail = (match.group(0) or "").lower()
        if "lakh" in tail or "lac" in tail:
            base *= 100_000
        elif "million" in tail or re.search(r"\bm\b", tail):
            base *= 1_000_000
        amounts.add(base)
    return frozenset(amounts)


def dispositive_signals_conflict(left: Optional[str], right: Optional[str]) -> bool:
    """True when both texts carry incompatible dispositive signals (outcome or surety amount)."""
    lo, ro = extract_dispositive_outcomes(left), extract_dispositive_outcomes(right)
    if lo and ro and lo != ro:
        return True
    la, ra = extract_monetary_amounts(left), extract_monetary_amounts(right)
    if la and ra and la != ra:
        return True
    return False


def reconciliation_pair_allowed(left: Optional[str], right: Optional[str], *, guard_enabled: bool) -> bool:
    if not guard_enabled:
        return True
    return not dispositive_signals_conflict(left, right)
