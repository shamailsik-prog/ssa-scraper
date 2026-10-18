from scraper.config import settings
from scraper.extractors.validation import reconcile_judgment
from scraper.parsers.pld_citation_relabel import relabel_pld_citation
from scraper.parsers.pls_text_encoding import has_mojibake_run, try_redecode_text


def test_mojibake_detector():
    assert has_mojibake_run("hello ???? world")
    assert not has_mojibake_run("hello ? world")


def test_pld_relabel_from_header():
    out = relabel_pld_citation("2026 PLD 511", "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J")
    assert out == "PLD 2026 Lahore 511"


def test_encoding_guard_quarantines(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    text = "Before X, J\n" + "x" * 600 + "\n????????"
    det = {
        "citations": ["2020 CLC 1"],
        "court": "Lahore",
        "year": 2020,
        "full_text_candidate": text,
        "judge_names": ["X"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=text,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.quarantine
    assert any("mojibake" in e for e in out.errors)


def test_try_redecode_bytes():
    raw = "test".encode("utf-8")
    text, enc = try_redecode_text(raw)
    assert text == "test" and enc == "utf-8"
