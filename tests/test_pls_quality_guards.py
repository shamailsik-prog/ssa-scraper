from scraper.config import settings
from scraper.extractors.validation import reconcile_judgment
from scraper.parsers.pld_citation_relabel import relabel_pld_citation
from scraper.pls_quality_guards import apply_pls_quality_guards
from scraper.parsers.pls_text_encoding import (
    classify_mojibake,
    has_mojibake_run,
    strip_mojibake_headnote_lines,
    try_redecode_text,
)


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


def test_hybrid_extractor_forwards_source_name_into_reconcile():
    import inspect

    from scraper.extractors.hybrid_extractor import HybridExtractor

    src = inspect.getsource(HybridExtractor.extract_judgment)
    assert "source_name=self.source.source_name" in src


HEADNOTE_ONLY = (
    "IN THE HIGH COURT OF SINDH\n"
    "The application is allowed.\n"
    "Announced.\n"
    "J.\n"
    "???? ???? ???? سرخی\n"
)


def test_classify_mojibake_headnote_after_signature():
    assert classify_mojibake(HEADNOTE_ONLY) == "headnote"
    stripped = strip_mojibake_headnote_lines(HEADNOTE_ONLY)
    assert "????" not in stripped
    assert "The application is allowed." in stripped


def test_classify_mojibake_body_damage_still_refetch():
    body = "IN THE HIGH COURT OF SINDH\nThe ???? applicant seeks bail.\nAnnounced.\nJ.\n"
    assert classify_mojibake(body) == "body"


def test_encoding_guard_flags_headnote_without_refetch_error(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    errors: list[str] = []
    out: dict = {"full_text_candidate": HEADNOTE_ONLY}
    apply_pls_quality_guards(out, raw_text=HEADNOTE_ONLY, source_name="PakistanLawSite", errors=errors)
    assert errors == []
    assert out.get("encoding_flag") == "urdu_headnote_mojibake"
    assert "????" not in (out.get("encoding_headnote_stripped_preview") or "")


def test_auditor_mojibake_baseline_is_6708():
    from scraper.parsers.pls_text_encoding import AUDITOR_2026_10_18_MOJIBAKE_ROWS

    assert AUDITOR_2026_10_18_MOJIBAKE_ROWS == 6708
