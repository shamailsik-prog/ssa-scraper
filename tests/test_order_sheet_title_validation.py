from scraper.config import settings
from scraper.extractors.validation import reconcile_judgment

HEADER = """1990 C L C 1205   [Peshawar]   Before J
Mst. KAUSAR BIBI---Petitioner
versus
MUHAMMAD MUSHTAQ---Respondents
Civil Revision No.329 of 1989, decided on 22nd November, 1989."""


def test_order_sheet_title_flag_overrides_deterministic_title(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
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
    assert "KAUSAR BIBI" in (out.data.get("case_title") or "")
