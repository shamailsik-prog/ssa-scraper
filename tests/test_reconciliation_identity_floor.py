"""Adversarial material-difference cases for reported/unreported identity.

These are pure guard tests; database-backed reconciliation remains a separate CI requirement.
"""
import pytest
from scraper.reported_unreported_guard import reconciliation_pair_allowed, extract_monetary_amounts


@pytest.mark.parametrize("a,b", [
    ("The applicant shall be released from custody.", "The applicant shall not be released from custody."),
    ("The surety shall be furnished within 7 days.", "The surety shall be furnished within 30 days."),
    ("The application is allowed with costs.", "The application is allowed without costs."),
    ("Bail is granted upon furnishing bond Rs. 100,000.", "Bail is granted upon furnishing bond Rs. 100,000 and additional separate surety."),
    ("Respondent 1 shall comply with the order.", "Respondent 2 shall comply with the order."),
    ("The accused is acquitted.", "The accused is convicted."),
    ("The petition is allowed.", "The petition is not allowed."),
    ("Bail is granted on Rs. 100,000 surety.", "Bail is refused on Rs. 100,000 surety."),
])
def test_material_change_never_auto_merges(a, b):
    assert not reconciliation_pair_allowed(a, b, guard_enabled=True)
    assert not reconciliation_pair_allowed(a, b, guard_enabled=False)


@pytest.mark.parametrize("a,b", [
    ("Application is allowed. Surety Rs. 100,000.", "Application is allowed. Surety Rs.100000."),
    ("Application is allowed. Surety Rs. 1.5 lakh.", "Application is allowed. Surety Rs. 150,000."),
    ("The application is allowed.", "THE application is allowed!"),
    ("The application is allowed.", "The application is allowed. Reported copy formatting."),
])
def test_cosmetic_change_can_reconcile(a, b):
    assert reconciliation_pair_allowed(a, b, guard_enabled=True)


def test_different_text_without_disposition_blocks():
    assert not reconciliation_pair_allowed("The court finds merit.", "The court finds no merit.", guard_enabled=True)


def test_amount_parser():
    assert extract_monetary_amounts("Rs. 1.5 lakh") == frozenset({150000})


def test_urdu_text_difference_is_not_ignored():
    assert not reconciliation_pair_allowed("عدالت نے درخواست منظور کی۔", "عدالت نے درخواست مسترد کی۔", guard_enabled=True)


def test_mixed_english_urdu_difference_is_not_ignored():
    assert not reconciliation_pair_allowed("Application is allowed. درخواست منظور۔", "Application is allowed. درخواست مسترد۔", guard_enabled=False)
