from scraper.config import settings
from scraper.extractors.validation import reconcile_judgment
from scraper.parsers.pld_citation_relabel import relabel_pld_citation
from scraper.pls_quality_guards import apply_pls_quality_guards
from scraper.parsers.pls_text_encoding import classify_mojibake, has_mojibake_run, strip_mojibake_headnote_lines


def test_mojibake_detector():
    assert has_mojibake_run("hello ???? world")
    assert not has_mojibake_run("hello ? world")


def test_pld_relabel_from_header():
    out = relabel_pld_citation("2026 PLD 511", "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J")
    assert out == "PLD 2026 Lahore 511"


def test_encoding_guard_quarantines(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    monkeypatch.setattr(settings, "SSA_PLD_CITATION_RELABEL", False)
    monkeypatch.setattr(settings, "SSA_COURT_HEADER_GUARD", False)
    text = "IN THE LAHORE HIGH COURT\nBefore X, J\n" + ("x" * 280) + " ???? " + ("y" * 280)
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
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    errors: list[str] = []
    out: dict = {"full_text_candidate": HEADNOTE_ONLY}
    apply_pls_quality_guards(out, raw_text=HEADNOTE_ONLY, source_name="PakistanLawSite", errors=errors)
    assert errors == []
    assert out.get("encoding_flag") == "urdu_headnote_mojibake"
    assert out.get("quality_flag_urdu_headnote_mojibake") is True
    assert "????" not in (out.get("encoding_headnote_stripped_preview") or "")


def test_encoding_guard_apply_appends_body_mojibake_error(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    text = "IN THE LAHORE HIGH COURT\nBefore X, J\n" + ("x" * 280) + " ???? " + ("y" * 280)
    errors: list[str] = []
    apply_pls_quality_guards({}, raw_text=text, source_name="PakistanLawSite", errors=errors)
    assert any("mojibake" in e for e in errors)


def test_mutant_m2_latin_then_mojibake_without_signature_is_body():
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The applicant seeks bail in this matter.\n"
        "???? ???? damaged line\n"
    )
    assert classify_mojibake(text) == "body"


def test_classify_latin_words_and_mojibake_after_signature_is_body():
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "Announced.\n"
        "J.\n"
        "The applicant ???? seeks relief here.\n"
    )
    assert classify_mojibake(text) == "body"


def test_classify_mojibake_before_signature_line_is_body():
    text = "IN THE HIGH COURT OF SINDH\nThe ???? applicant seeks bail.\nAnnounced.\nJ.\n"
    assert classify_mojibake(text) == "body"


def test_classify_all_mojibake_line_before_signature_is_body():
    """R4 M2: all-???? line before signature is body damage (pls_text_encoding before-block guard)."""
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "???? ???? ????\n"
        "The application is allowed.\n"
        "J.\n"
    )
    assert classify_mojibake(text) == "body"


def test_classify_single_mojibake_run_line_before_signature_is_body():
    """R6 M2: lone ???? before signature is body even when post-signature headnote tail exists."""
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "????\n"
        "The application is allowed.\n"
        "Sd/-\n"
        "????\n"
    )
    assert classify_mojibake(text) == "body"


def test_ref_prefix_post_signature_mojibake_line_is_body_and_survives_strip():
    """R6 R4: one Latin word + mojibake + 16+ other chars is body damage; strip keeps the line."""
    line = "Ref ???? 1234567890123456"
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "J.\n"
        f"{line}\n"
    )
    assert classify_mojibake(text) == "body"
    stripped = strip_mojibake_headnote_lines(text)
    assert line in stripped


def test_encoding_guard_off_body_damage_does_not_set_quality_flag(monkeypatch):
    """R6 S6: with SSA_ENCODING_GUARD off, body mojibake is not flagged for promotion."""
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", False)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    text = "IN THE LAHORE HIGH COURT\nBefore X, J\n" + ("x" * 280) + " ???? " + ("y" * 280)
    errors: list[str] = []
    out: dict = {"full_text_candidate": text}
    apply_pls_quality_guards(out, raw_text=text, source_name="PakistanLawSite", errors=errors)
    assert errors == []
    assert "quality_flag_urdu_headnote_mojibake" not in out
    assert "encoding_flag" not in out


def test_apply_pls_full_text_replacement_body_damage_keeps_headnote_flag_false():
    """R6 S6: body-damaged PLS text must not set quality_flag_urdu_headnote_mojibake on replacement."""
    from scraper.models import Judgment
    from scraper.tasks.promotion import _apply_pls_full_text_replacement

    text = "IN THE LAHORE HIGH COURT\nBefore X, J\n" + ("x" * 280) + " ???? " + ("y" * 280)
    judgment = Judgment(
        source_name="PakistanLawSite",
        canonical_citation="PLS body-flag test",
        full_text="",
        full_text_hash="a" * 64,
        quality_flag_urdu_headnote_mojibake=True,
    )
    _apply_pls_full_text_replacement(judgment, text, {"quality_flag_urdu_headnote_mojibake": True})
    assert classify_mojibake(text) == "body"
    assert judgment.quality_flag_urdu_headnote_mojibake is False


def test_classify_mojibake_between_english_lines_without_signature_is_body():
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "???? ????\n"
        "Further reasons follow here.\n"
    )
    assert classify_mojibake(text) == "body"


def test_sixteen_char_plain_line_after_signature_is_body_damage():
    """R4: substantive post-signature line (16+ non-? chars) with headnote tail is body, line kept."""
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "J.\n"
        "abcdefghijklmnop\n"
        "???? ????\n"
    )
    assert classify_mojibake(text) == "body"
    stripped = strip_mojibake_headnote_lines(text)
    assert "abcdefghijklmnop" in stripped


def test_dismissed_mojibake_after_signature_is_body_damage():
    """R5: short Latin word + mojibake after signature is body damage."""
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "J.\n"
        "Dismissed ????\n"
    )
    assert classify_mojibake(text) == "body"
    assert "Dismissed" in strip_mojibake_headnote_lines(text)


def test_strip_keeps_latin_mojibake_line_after_signature():
    from scraper.parsers.pls_text_encoding import strip_mojibake_headnote_lines

    raw = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "J.\n"
        "???? ????\n"
        "The applicant ???? seeks relief here.\n"
    )
    stripped = strip_mojibake_headnote_lines(raw)
    assert "The applicant ???? seeks relief here." in stripped
    tail = stripped.split("J.\n", 1)[1]
    assert "????" in tail
    assert tail.strip().startswith("The applicant")


def test_headnote_only_trailing_mojibake_without_signature():
    text = (
        "IN THE HIGH COURT OF SINDH\n"
        "The application is allowed.\n"
        "???? ???? ???? سرخی\n"
    )
    assert classify_mojibake(text) == "headnote"


def test_auditor_mojibake_baseline_is_6708():
    from scraper.parsers.pls_text_encoding import AUDITOR_2026_10_18_MOJIBAKE_ROWS

    assert AUDITOR_2026_10_18_MOJIBAKE_ROWS == 6708
