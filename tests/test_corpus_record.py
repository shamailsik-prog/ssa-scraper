"""The corpus record format (scraper/corpus_record.py): one shape for every scraper's output."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from types import SimpleNamespace

from scraper.corpus_record import SCHEMA_VERSION, THIN_TEXT_CHARS, judgment_record, statute_section_record, text_is_thin, validate_record
from scraper.parsers.pdf_writer import RENDERED_COPY_LABEL

LEGACY_KEYS = {
    "canonical_citation", "case_title", "court", "judge_names", "bench_size", "bench_type", "decision_date", "year",
    "source_name", "source_url", "access_method", "full_text_sha256_canonical", "has_original_pdf", "document_label", "judgment_id",
}


def _judgment(**over):
    base = dict(
        id=uuid.uuid4(), canonical_citation="PLD 2024 SC 300", case_title="A v. B", court_name="Supreme Court of Pakistan",
        judge_names=["X J"], bench_size=1, bench_type="single", decision_date=date(2024, 3, 1), year=2024,
        reporter="PLD", volume="2024", page_number=300, docket_number="C.P. 1/2024", petitioner="A", respondent="B",
        source_name="supreme_court", source_url="https://www.supremecourt.gov.pk/x", access_method="public",
        full_text_hash="a" * 64, full_text="JUDGMENT " * 100, language="en", promoted_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_judgment_record_keeps_every_legacy_key_and_validates():
    rec = judgment_record(_judgment(), has_original_pdf=True)
    assert LEGACY_KEYS <= set(rec)
    assert rec["schema_version"] == SCHEMA_VERSION and rec["record_kind"] == "judgment"
    assert rec["files"] == {"document": "original.pdf", "text": "judgment.txt"}
    assert rec["reported"] is True and rec["needs_ocr"] is False
    assert validate_record(rec) == []
    json.dumps(rec)  # serialisable as it stands


def test_rendered_copy_keeps_its_original_label():
    rec = judgment_record(_judgment(reporter=None), has_original_pdf=False)
    assert rec["document_label"] == RENDERED_COPY_LABEL
    assert rec["files"]["document"] == "rendered_copy.pdf" and rec["reported"] is False
    assert validate_record(rec) == []


def test_thin_text_is_marked_for_ocr_by_ingestion():
    assert text_is_thin("x" * (THIN_TEXT_CHARS - 1)) and not text_is_thin("x" * THIN_TEXT_CHARS)
    rec = judgment_record(_judgment(full_text="   "), has_original_pdf=True)
    assert rec["needs_ocr"] is True and validate_record(rec) == []


def test_validate_names_each_problem():
    rec = judgment_record(_judgment(), has_original_pdf=True)
    rec.update(year=1900, full_text_sha256_canonical="nothex", needs_ocr="no")
    rec["files"] = {"document": "rendered_copy.pdf", "text": "judgment.txt"}
    del rec["court"]
    problems = validate_record(rec)
    assert any("court is missing" in p for p in problems)
    assert any("1947-2100" in p for p in problems)
    assert any("sha256" in p for p in problems)
    assert any("needs_ocr" in p for p in problems)
    assert any("has_original_pdf" in p for p in problems)
    assert validate_record({"canonical_citation": "x"})[0].startswith("schema_version")
    assert validate_record([]) == ["record is not a JSON object"]


def test_statute_section_record_validates_and_allows_pre_1947_acts():
    statute = SimpleNamespace(name="Pakistan Penal Code", short_name="PPC", jurisdiction="Federal", statute_type="act", year_enacted=1860, is_repealed=False, source_name="pakistancode", source_url="https://pakistancode.gov.pk/x")
    section = SimpleNamespace(section_number="302", section_title="Punishment of qatl-i-amd", chapter="XVI", part=None)
    version = SimpleNamespace(version_no=1, effective_from=None, effective_to=None, text_hash="b" * 64, section_text="Whoever commits qatl-i-amd shall...")
    rec = statute_section_record(statute, section, version)
    assert rec["year_enacted"] == 1860 and rec["needs_ocr"] is False
    assert validate_record(rec) == []
    version.section_text = ""
    assert statute_section_record(statute, section, version)["needs_ocr"] is True
