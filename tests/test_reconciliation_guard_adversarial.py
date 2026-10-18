from scraper.reported_unreported_guard import (
    extract_monetary_amounts,
    dispositive_signals_conflict,
    reconciliation_pair_allowed,
)


def test_lakh_decimal():
    assert extract_monetary_amounts("surety Rs. 1.5 lakh") == frozenset({150000})


def test_legal_negation():
    assert dispositive_signals_conflict("The application is allowed.", "The application is not allowed.")


def test_bail_granted_refused():
    assert dispositive_signals_conflict("Bail is granted.", "Bail is refused.")


def test_dismissed_vs_rejected():
    assert dispositive_signals_conflict("The application is dismissed.", "The application is rejected.")


def test_bail_monetary_variation():
    assert dispositive_signals_conflict("Bail granted on surety Rs. 100,000", "Bail granted on surety Rs. 200,000")


def test_identical():
    assert not dispositive_signals_conflict("Application is allowed. Surety Rs. 100,000.", "Application is allowed. Surety Rs. 100,000.")


def test_guard_on_blocks_incompatible_pair():
    assert not reconciliation_pair_allowed("Bail is granted.", "Bail is refused.", guard_enabled=True)


def test_lakh_equivalence_to_rupees():
    assert extract_monetary_amounts("Surety Rs. 1.5 lakh") == extract_monetary_amounts("Surety Rs. 150,000")


def test_million_equivalence_to_rupees():
    assert extract_monetary_amounts("Costs PKR 1.25 million") == frozenset({1250000})


def test_no_recognized_disposition_different_text_blocks():
    assert dispositive_signals_conflict("The court finds no merit.", "The court finds merit.")


def test_no_recognized_disposition_same_text_allows():
    assert not dispositive_signals_conflict("The court finds merit.", "  THE court finds merit.  ")


def test_missing_outcome_blocks():
    assert dispositive_signals_conflict("Application is allowed.", "Judgment delivered.")


def test_missing_amount_blocks():
    assert dispositive_signals_conflict("Application is allowed. Surety Rs. 50,000.", "Application is allowed. Surety imposed.")
