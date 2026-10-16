"""Conservative reconciliation regression tests: distinct judicial orders must survive."""
import pytest
from scraper.tasks.promotion import _reported_unreported_similarity, _safe_reconciliation_text
from tests.test_unreported_promotion import BODY


@pytest.mark.parametrize("different", [
    BODY.replace("application is allowed", "application is dismissed"),
    BODY.replace("application is allowed", "application is not allowed"),
    BODY.replace("Rs.100,000", "Rs.500,000"),
    BODY.replace("four months", "six months"),
    BODY + " The accused shall surrender his passport.",
    BODY + " The accused walks free.",
    BODY + " The applicant shall deposit Rs.500,000.",
    BODY + " Reporter formatting note. The appeal is dismissed.",
    BODY + " Reported copy formatting: new conditions.",
])
def test_materially_different_orders_are_not_automatically_reconciled(different):
    assert _reported_unreported_similarity(BODY, different) >= 0.92
    assert not _safe_reconciliation_text(BODY, different)


@pytest.mark.parametrize("same", [
    BODY,
    BODY.replace(" ", "   "),
    BODY + " Reported copy formatting.",
    "Reporter formatting note. " + BODY,
    BODY + " Editorial pagination note.",
])
def test_exact_and_inert_reporter_variants_still_reconcile(same):
    assert _safe_reconciliation_text(BODY, same)
