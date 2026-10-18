from __future__ import annotations

from scraper.reported_unreported_guard import (
    dispositive_signals_conflict,
    extract_dispositive_outcomes,
    extract_monetary_amounts,
    reconciliation_pair_allowed,
)

BODY_ALLOWED = (
    "Having heard the parties and perused the record, the case of the applicant calls for further inquiry. "
    "The application is allowed subject to his furnishing solvent surety in the sum of Rs.100,000 "
    "and a personal bond in the like amount to the satisfaction of the trial court."
)


def test_extracts_allowed_outcome_and_amount():
    assert "allowed" in extract_dispositive_outcomes(BODY_ALLOWED)
    assert 100_000 in extract_monetary_amounts(BODY_ALLOWED)


def test_allowed_vs_dismissed_with_high_boilerplate_similarity_conflicts():
    dismissed = BODY_ALLOWED.replace("allowed", "dismissed").replace("Rs.100,000", "Rs.50,000")
    assert dispositive_signals_conflict(BODY_ALLOWED, dismissed)


def test_guard_off_permits_legacy_merge_decision():
    dismissed = BODY_ALLOWED.replace("allowed", "dismissed").replace("Rs.100,000", "Rs.50,000")
    assert reconciliation_pair_allowed(BODY_ALLOWED, dismissed, guard_enabled=False)


def test_guard_on_blocks_incompatible_pair():
    dismissed = BODY_ALLOWED.replace("allowed", "dismissed").replace("Rs.100,000", "Rs.50,000")
    assert not reconciliation_pair_allowed(BODY_ALLOWED, dismissed, guard_enabled=True)


def test_same_outcome_and_amount_is_compatible():
    twin = BODY_ALLOWED + "  Reported copy formatting."
    assert not dispositive_signals_conflict(BODY_ALLOWED, twin)
    assert reconciliation_pair_allowed(BODY_ALLOWED, twin, guard_enabled=True)
