"""Conservative PLD relabel: conflicting explicit court headings must not relabel."""
import pytest
from scraper.parsers.pld_citation_relabel import relabel_pld_citation

@pytest.mark.parametrize("header,expected", [
    ("IN THE SUPREME COURT OF PAKISTAN\nIN THE HIGH COURT OF SINDH", "2026 PLD 511"),
    ("IN THE HIGH COURT OF SINDH\nIN THE SUPREME COURT OF PAKISTAN", "2026 PLD 511"),
    ("IN THE LAHORE HIGH COURT\nIN THE PESHAWAR HIGH COURT", "2026 PLD 511"),
    ("IN THE SUPREME COURT OF PAKISTAN\nBefore X\nReference to Lahore High Court", "PLD 2026 SC 511"),
    ("IN THE HIGH COURT OF SINDH AT KARACHI\nBefore X", "PLD 2026 Sindh 511"),
    ("IN THE LAHORE HIGH COURT, LAHORE\nBefore X", "PLD 2026 Lahore 511"),
    ("The Supreme Court cited the Lahore High Court.", "2026 PLD 511"),
])
def test_conflicting_headings_fail_closed(header, expected):
    assert relabel_pld_citation("2026 PLD 511", header) == expected
