from scraper.audit_baselines import AUDIT_2026_10_18
from scraper.config import settings
from scraper.parsers.statute_parser import looks_like_clause_fragment, looks_like_fragment_name, looks_like_garbage_name


def test_auditor_duplicate_and_year_baselines():
    assert AUDIT_2026_10_18["statutes_duplicate_names"] == 79
    assert AUDIT_2026_10_18["statutes_no_year"] == 588


def test_clause_fragments_from_auditor_examples():
    assert looks_like_clause_fragment("means the Income Tax Act")
    assert looks_like_clause_fragment("further to amend the Customs Act")
    assert looks_like_fragment_name("means the Income Tax Act")
    assert not looks_like_clause_fragment("Pakistan Penal Code, 1860")


def test_garbage_jpeg_and_pdf_bytes_as_names():
    assert looks_like_garbage_name("JFIF\x00\x00not-a-title")
    assert looks_like_garbage_name("%PDF-1.4 stream")
    assert looks_like_fragment_name("%PDF-1.4 stream")
    assert not looks_like_garbage_name("Sales Tax Act, 1990")


def test_name_quality_flag_defaults_off():
    assert settings.SSA_STATUTE_NAME_QUALITY is False
