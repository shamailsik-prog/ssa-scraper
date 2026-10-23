"""B4: PLS judgment-content guards default on; explicit env only disables."""

from __future__ import annotations

import asyncio
import inspect

import pytest

from scraper.config import Settings, settings
from scraper.extractors.validation import reconcile_judgment
from scraper.pls_quality_guards import SHORT_TEXT_MIN_CHARS, apply_pls_quality_guards
from tests.test_pls_quality_guards import HEADNOTE_ONLY as HEADNOTE_ONLY_TEXT
from tests.test_order_sheet_title_validation import HEADER as ORDER_SHEET_HEADER

PLS_GUARD_FLAGS = (
    "SSA_ORDER_SHEET_TITLE",
    "SSA_ENCODING_GUARD",
    "SSA_COURT_HEADER_GUARD",
    "SSA_PLD_CITATION_RELABEL",
    "SSA_SHORT_TEXT_GUARD",
)


def test_pls_guard_flags_default_on_in_settings_singleton():
    for name in PLS_GUARD_FLAGS:
        assert getattr(settings, name) is True


def test_pls_guard_flags_default_on_fresh_settings_without_env(monkeypatch):
    for key in PLS_GUARD_FLAGS:
        monkeypatch.delenv(key, raising=False)
    fresh = Settings()
    for name in PLS_GUARD_FLAGS:
        assert getattr(fresh, name) is True


@pytest.mark.parametrize("env_name", PLS_GUARD_FLAGS)
def test_pls_guard_env_zero_disables(monkeypatch, env_name):
    monkeypatch.setenv(env_name, "0")
    fresh = Settings()
    assert getattr(fresh, env_name) is False


def test_mutant_b4_config_encoding_guard_default_on():
    assert Settings.model_fields["SSA_ENCODING_GUARD"].default is True


def test_mutant_b4_config_court_header_guard_default_on():
    assert Settings.model_fields["SSA_COURT_HEADER_GUARD"].default is True


def test_mutant_b4_config_short_text_guard_default_on():
    assert Settings.model_fields["SSA_SHORT_TEXT_GUARD"].default is True


def test_mutant_b4_config_pld_relabel_default_on():
    assert Settings.model_fields["SSA_PLD_CITATION_RELABEL"].default is True


def test_mutant_b4_config_order_sheet_title_default_on():
    assert Settings.model_fields["SSA_ORDER_SHEET_TITLE"].default is True


def test_mutant_b4_config_pls_court_resolution_default_on():
    assert Settings.model_fields["SSA_PLS_COURT_RESOLUTION"].default is True


def test_encoding_body_mojibake_quarantines_with_defaults_on(monkeypatch):
    monkeypatch.delenv("SSA_ENCODING_GUARD", raising=False)
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
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


def test_encoding_guard_off_skips_body_quarantine(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", False)
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
    assert not any("mojibake" in e for e in out.errors)


def test_headnote_classification_with_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", True)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    errors: list[str] = []
    out: dict = {"full_text_candidate": HEADNOTE_ONLY_TEXT}
    apply_pls_quality_guards(out, raw_text=HEADNOTE_ONLY_TEXT, source_name="PakistanLawSite", errors=errors)
    assert errors == []
    assert out.get("encoding_flag") == "urdu_headnote_mojibake"
    assert out.get("quality_flag_urdu_headnote_mojibake") is True


def test_headnote_flag_off_skips_classification(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ENCODING_GUARD", False)
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    errors: list[str] = []
    out: dict = {"full_text_candidate": HEADNOTE_ONLY_TEXT}
    apply_pls_quality_guards(out, raw_text=HEADNOTE_ONLY_TEXT, source_name="PakistanLawSite", errors=errors)
    assert "encoding_flag" not in out


def test_court_header_guard_mismatch_with_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_COURT_HEADER_GUARD", True)
    raw = "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J\n" + "y" * 600
    errors: list[str] = []
    out: dict = {"court": "Supreme Court of Pakistan", "citations": ["2020 CLC 1"]}
    apply_pls_quality_guards(out, raw_text=raw, source_name="PakistanLawSite", errors=errors)
    assert any("court mismatch" in e for e in errors)


def test_court_header_guard_off_skips_mismatch(monkeypatch):
    monkeypatch.setattr(settings, "SSA_COURT_HEADER_GUARD", False)
    raw = "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J\n" + "y" * 600
    errors: list[str] = []
    out: dict = {"court": "Supreme Court of Pakistan", "citations": ["2020 CLC 1"]}
    apply_pls_quality_guards(out, raw_text=raw, source_name="PakistanLawSite", errors=errors)
    assert not any("court mismatch" in e for e in errors)


def test_short_text_guard_with_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    short = "x" * (SHORT_TEXT_MIN_CHARS - 1)
    errors: list[str] = []
    out: dict = {"citations": ["unreported docket only"]}
    apply_pls_quality_guards(out, raw_text=short, source_name="PakistanLawSite", errors=errors)
    assert any("shorter than" in e for e in errors)


def test_short_text_guard_499_chars_errors(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    short = "x" * 499
    errors: list[str] = []
    out: dict = {"citations": ["misc unreported"]}
    apply_pls_quality_guards(out, raw_text=short, source_name="PakistanLawSite", errors=errors)
    assert any("shorter than" in e for e in errors)


def test_short_text_guard_500_chars_ok(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    ok = "x" * 500
    errors: list[str] = []
    out: dict = {"citations": ["misc"]}
    apply_pls_quality_guards(out, raw_text=ok, source_name="PakistanLawSite", errors=errors)
    assert errors == []


def test_short_text_guard_exempts_reported_citation_at_499(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    short = "x" * 499
    errors: list[str] = []
    out: dict = {"citations": ["PLD 2020 SC 1"]}
    apply_pls_quality_guards(out, raw_text=short, source_name="PakistanLawSite", errors=errors)
    assert errors == []


def test_mutant_m4_short_text_guard_disabled_branch(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    short = "x" * 10
    errors: list[str] = []
    out: dict = {"citations": []}
    apply_pls_quality_guards(out, raw_text=short, source_name="PakistanLawSite", errors=errors)
    assert errors == []


def test_pls_quality_guards_skipped_for_non_pls_source(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", True)
    errors: list[str] = []
    apply_pls_quality_guards({}, raw_text="x" * 10, source_name="SupremeCourt", errors=errors)
    assert errors == []


def test_mutant_m10_order_sheet_not_applied_off_pls(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    from tests.test_order_sheet_title_validation import HEADER

    det = {
        "case_title": "headnote wrong versus line",
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
        source_name="SupremeCourt",
    )
    assert out.data.get("case_title") == "headnote wrong versus line"


def test_short_text_guard_off_skips(monkeypatch):
    monkeypatch.setattr(settings, "SSA_SHORT_TEXT_GUARD", False)
    short = "x" * (SHORT_TEXT_MIN_CHARS - 1)
    errors: list[str] = []
    out: dict = {"citations": ["2020 CLC 1"]}
    apply_pls_quality_guards(out, raw_text=short, source_name="PakistanLawSite", errors=errors)
    assert errors == []


def test_pld_relabel_in_reconcile_with_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLD_CITATION_RELABEL", True)
    raw = "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J\n" + "z" * 600
    det = {
        "citations": ["2026 PLD 511"],
        "court": "Lahore",
        "year": 2026,
        "full_text_candidate": raw,
        "judge_names": ["X"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=raw,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.data["citations"][0] == "PLD 2026 Lahore 511"


def test_pld_relabel_off_leaves_citation(monkeypatch):
    monkeypatch.setattr(settings, "SSA_PLD_CITATION_RELABEL", False)
    raw = "IN THE LAHORE HIGH COURT, LAHORE\nBefore X, J\n" + "z" * 600
    det = {
        "citations": ["2026 PLD 511"],
        "court": "Lahore",
        "year": 2026,
        "full_text_candidate": raw,
        "judge_names": ["X"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=raw,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.data["citations"][0] == "2026 PLD 511"


def test_order_sheet_title_with_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", True)
    det = {
        "case_title": "1999 CLC 1 cited case versus other",
        "citations": ["1990 CLC 1205"],
        "court": "Peshawar",
        "year": 1990,
        "full_text_candidate": ORDER_SHEET_HEADER,
        "judge_names": ["J"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=ORDER_SHEET_HEADER,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert "KAUSAR BIBI" in (out.data.get("case_title") or "")


def test_order_sheet_title_off_keeps_deterministic(monkeypatch):
    monkeypatch.setattr(settings, "SSA_ORDER_SHEET_TITLE", False)
    det = {
        "case_title": "1999 CLC 1 cited case versus other",
        "citations": ["1990 CLC 1205"],
        "court": "Peshawar",
        "year": 1990,
        "full_text_candidate": ORDER_SHEET_HEADER,
        "judge_names": ["J"],
    }
    out = reconcile_judgment(
        deterministic=det,
        ai=None,
        raw_text=ORDER_SHEET_HEADER,
        court_directory={},
        min_confidence=0.5,
        source_name="PakistanLawSite",
    )
    assert out.data.get("case_title") == "1999 CLC 1 cited case versus other"


def test_mutant_b4_validation_order_sheet_flag_check_present():
    src = inspect.getsource(
        __import__("scraper.extractors.validation", fromlist=["reconcile_judgment"]).reconcile_judgment
    )
    assert "SSA_ORDER_SHEET_TITLE" in src
    assert "case_title_from_order_sheet" in src


def test_pls_backfill_clis_default_dry_run(monkeypatch, capsys):
    from scraper.tasks import pls_admin

    captured: dict[str, bool] = {}

    async def _meta(**kwargs):
        captured["meta"] = kwargs.get("dry_run", False)
        return {"dry_run": True}

    async def _titles(**kwargs):
        captured["titles"] = kwargs.get("dry_run", False)
        return {"dry_run": True}

    async def _pld(**kwargs):
        captured["pld"] = kwargs.get("dry_run", False)
        return {"dry_run": True}

    monkeypatch.setattr(pls_admin, "run_async", lambda coro: asyncio.run(coro))

    import scraper.tasks.pls_backfill as pls_backfill

    monkeypatch.setattr(pls_backfill, "run_backfill_metadata", _meta)
    monkeypatch.setattr(pls_backfill, "run_backfill_order_sheet_titles", _titles)
    monkeypatch.setattr(pls_backfill, "run_backfill_pld_citations", _pld)

    pls_admin.main(["backfill-metadata"])
    pls_admin.main(["backfill-order-sheet-titles"])
    pls_admin.main(["backfill-pld-citations"])
    assert captured == {"meta": True, "titles": True, "pld": True}

    captured.clear()
    pls_admin.main(["backfill-metadata", "--apply"])
    assert captured["meta"] is False
