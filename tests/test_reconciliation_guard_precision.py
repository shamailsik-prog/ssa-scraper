"""Regression tests: reconciliation must not discard judicial words or paisa."""
from decimal import Decimal

import pytest

from scraper.reported_unreported_guard import (
    extract_monetary_amounts,
    reconciliation_pair_allowed,
)


@pytest.mark.parametrize("word", ["copy", "reported", "headnote", "layout", "formatting", "pagination"])
@pytest.mark.parametrize("guard_enabled", [True, False])
def test_substantive_editorial_looking_words_are_preserved(word, guard_enabled):
    left = f"The registry shall deliver {word} to the respondent within seven days."
    right = "The registry shall deliver to the respondent within seven days."
    assert not reconciliation_pair_allowed(left, right, guard_enabled=guard_enabled)


@pytest.mark.parametrize("guard_enabled", [True, False])
@pytest.mark.parametrize("left,right", [
    ("The accused shall deposit Rs. 1000.50.", "The accused shall deposit Rs. 1000.00."),
    ("The accused shall deposit Rs. 1.50.", "The accused shall deposit Rs. 1.00."),
])
def test_fractional_rupee_differences_block_merge(left, right, guard_enabled):
    assert not reconciliation_pair_allowed(left, right, guard_enabled=guard_enabled)


@pytest.mark.parametrize("guard_enabled", [True, False])
def test_explicit_terminal_reporter_suffix_is_cosmetic(guard_enabled):
    assert reconciliation_pair_allowed(
        "Application is allowed.",
        "Application is allowed. Reported copy formatting.",
        guard_enabled=guard_enabled,
    )


def test_rupee_units_and_paisa_are_exact():
    assert extract_monetary_amounts("Rs. 1.5 lakh") == frozenset({Decimal("150000")})
    assert extract_monetary_amounts("Rs. 1000.50") == frozenset({Decimal("1000.50")})
    assert reconciliation_pair_allowed(
        "Rs. 1.5 lakh shall be deposited.",
        "Rs. 150,000 shall be deposited.",
        guard_enabled=True,
    )


def test_property_fractional_rupees_cannot_collapse():
    for whole in range(1, 101):
        for paisa in (1, 25, 50, 75, 99):
            a = f"The accused shall deposit Rs. {whole}.{paisa:02d} as costs."
            b = f"The accused shall deposit Rs. {whole}.00 as costs."
            assert not reconciliation_pair_allowed(a, b, guard_enabled=True)
