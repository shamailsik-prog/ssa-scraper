"""Dispositive-signal guard for reported/unreported automatic reconciliation (SSA_REPORTED_UNREPORTED_DISPOSITIVE_GUARD)."""

from __future__ import annotations

import re
from typing import FrozenSet, Optional, Set

# Outcome families that must not be collapsed into one judgment row.
_FAMILY_PATTERNS = (
    ("allowed", re.compile(r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+allowed\b|\b(?:partly\s+)?allowed\b", re.I)),
    ("dismissed", re.compile(r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+dismissed\b|\b(?:partly\s+)?dismissed\b", re.I)),
    ("remanded", re.compile(r"\bremanded\b", re.I)),
    ("disposed", re.compile(r"\bdisposed\s+of\b", re.I)),
    ("bail_granted", re.compile(r"\bbail\s+(?:is\s+)?granted\b|\bgranted\s+bail\b|\badmitted\s+to\s+bail\b", re.I)),
    ("bail_refused", re.compile(r"\bbail\s+(?:is\s+)?(?:refused|declined|rejected)\b|\brefused\s+bail\b", re.I)),
    ("acquitted", re.compile(r"\bacquitted\b", re.I)),
    ("convicted", re.compile(r"\bconvicted\b|\bconviction\s+(?:is\s+)?upheld\b", re.I)),
    ("stay_granted", re.compile(r"\bstay\s+(?:is\s+)?granted\b", re.I)),
    ("stay_vacated", re.compile(r"\bstay\s+(?:is\s+)?vacated\b|\bstay.{0,20}recalled\b", re.I)),
)

_CONFLICT_PAIRS = frozenset(
    {
        frozenset({"allowed", "dismissed"}),
        frozenset({"bail_granted", "bail_refused"}),
        frozenset({"acquitted", "convicted"}),
        frozenset({"stay_granted", "stay_vacated"}),
    }
)

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
    for name, pat in _FAMILY_PATTERNS:
        if pat.search(text):
            found.add(name)
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
    if lo and ro:
        for pair in _CONFLICT_PAIRS:
            if (pair & lo) and (pair & ro) and (pair & lo) != (pair & ro):
                return True
    la, ra = extract_monetary_amounts(left), extract_monetary_amounts(right)
    if la and ra and la != ra:
        return True
    return False


def reconciliation_pair_allowed(left: Optional[str], right: Optional[str], *, guard_enabled: bool) -> bool:
    if not guard_enabled:
        return True
    return not dispositive_signals_conflict(left, right)
