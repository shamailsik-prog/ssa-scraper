"""Dispositive-signal guard for reported/unreported automatic reconciliation (SSA_REPORTED_UNREPORTED_DISPOSITIVE_GUARD)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import FrozenSet, Optional, Set

# Outcome families that must not be collapsed into one judgment row.
_FAMILY_PATTERNS = (
    ("allowed", re.compile(r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+allowed\b|\b(?:partly\s+)?allowed\b", re.I)),
    ("not_allowed", re.compile(r"\b(?:application|petition|appeal|suit|revision|review)\s+is\s+not\s+allowed\b", re.I)),
    ("rejected", re.compile(r"\b(?:application|petition|appeal|suit|revision|review)\s+(?:is|stands)\s+rejected\b", re.I)),
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
    r"(?:rs\.?|pkr|rupees?)\s*([\d][\d,]*(?:\.\d+)?)\s*(lakh|lac|million|crore|m)?\b",
    re.IGNORECASE,
)


def _normalise_amount(raw: str, unit: str = "") -> int:
    """Parse rupee amounts without binary floating-point rounding."""
    try:
        value = Decimal(raw.replace(",", ""))
    except (InvalidOperation, AttributeError):
        return 0
    scale = {"lakh": 100_000, "lac": 100_000, "million": 1_000_000,
             "m": 1_000_000, "crore": 10_000_000}.get(unit.lower(), 1)
    return int(value * scale) if value > 0 else 0


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
        base = _normalise_amount(match.group(1), match.group(2) or "")
        if base <= 0:
            continue
        amounts.add(base)
    return frozenset(amounts)


def dispositive_signals_conflict(left: Optional[str], right: Optional[str]) -> bool:
    """Divergent or missing judicial signals are not affirmative identity evidence."""
    lo, ro = extract_dispositive_outcomes(left), extract_dispositive_outcomes(right)
    if lo != ro:
        return True
    la, ra = extract_monetary_amounts(left), extract_monetary_amounts(right)
    if la != ra:
        return True
    if not lo:
        normalized_left = re.sub(r"\s+", " ", left or "").strip().casefold()
        normalized_right = re.sub(r"\s+", " ", right or "").strip().casefold()
        return normalized_left != normalized_right
    return False


def reconciliation_pair_allowed(left: Optional[str], right: Optional[str], *, guard_enabled: bool) -> bool:
    if not guard_enabled:
        return True
    return not dispositive_signals_conflict(left, right)
