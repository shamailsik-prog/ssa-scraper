from scraper.config import settings
from scraper.extractors.validation import reconcile_judgment
from tests.test_order_sheet_title import EXPECTED_HEADER_TITLE, HEADER


def test_order_sheet_title_flag_overrides_deterministic_title(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    monkeypatch.setattr(settings, "SSA_PLD_CITATION_RELABEL", False)
    monkeypatch.setattr(settings, "SSA_COURT_HEADER_GUARD", False)
    det = {
        "case_title": "1999 CLC 1 cited case versus other",
        "citations": ["1990 CLC 1205"],
        "court": "Peshawar",
        "year": 1990,
        "full_text_candidate": HEADER,
        "judge_names": ["J"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=HEADER,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.data.get("case_title") == EXPECTED_HEADER_TITLE
