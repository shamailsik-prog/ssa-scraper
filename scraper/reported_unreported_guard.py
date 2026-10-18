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


# Editorial tokens may be added by a reporter without altering an order. Every other
# insertion, deletion, or substitution is treated as potentially dispositive.
# This deliberately favours separate records/manual review over a destructive false merge.
_EDITORIAL_TOKENS = frozenset({"reported", "copy", "formatting", "layout", "pagination", "headnote"})


def _identity_tokens(value: Optional[str]) -> list[str]:
    """Ignore typography, but preserve all judicial words, numbers, and negations."""
    def canonical_money(match: re.Match[str]) -> str:
        return f" pkramount{_normalise_amount(match.group(1), match.group(2) or '')} "

    canonical = _MONEY_RE.sub(canonical_money, value or "")
    return re.findall(r"[^\W_]+", canonical.casefold(), flags=re.UNICODE)


def _material_text_difference(left: Optional[str], right: Optional[str]) -> bool:
    """Linear-time conservative identity check; never rely on 92% similarity alone.

    The only tolerated word differences are known reporter/editorial labels.
    Every judicial word, number, party identifier and negation must match in order.
    """
    a, b = _identity_tokens(left), _identity_tokens(right)
    if not a or not b:
        return True
    return [t for t in a if t not in _EDITORIAL_TOKENS] != [
        t for t in b if t not in _EDITORIAL_TOKENS
    ]


def dispositive_signals_conflict(left: Optional[str], right: Optional[str]) -> bool:
    """Refuse automatic reconciliation when any substantive difference is unresolved."""
    if extract_dispositive_outcomes(left) != extract_dispositive_outcomes(right):
        return True
    if extract_monetary_amounts(left) != extract_monetary_amounts(right):
        return True
    return _material_text_difference(left, right)


def reconciliation_pair_allowed(left: Optional[str], right: Optional[str], *, guard_enabled: bool) -> bool:
    # The flag may disable the additional outcome extractor, but must never disable
    # the non-destructive identity floor. Existing .env files can still set it to 0.
    if _material_text_difference(left, right):
        return False
    return not guard_enabled or not dispositive_signals_conflict(left, right)
