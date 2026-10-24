"""False citation aliases: caption-only own citations, Present bench lines, cleanup scanner."""

from __future__ import annotations

import pytest

from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.parsers.bench_parser import parse_bench
from scraper.parsers.citation_extractor import score_confidence
from scraper.parsers.case_metadata import judgment_caption_slice
from scraper.tasks.citation_alias_cleanup import caption_citation_strings, _alias_suspicion_reason


def _pad(n: int = 80) -> str:
    return "The court held that the appeal lacks merit. " * n


def test_clc_956_body_pld_not_applicable_is_not_an_alias():
    """1992 CLC 956-style: body says PLD 1990 Lahore 4 not applicable — must not become an alias."""
    body = (
        "1992 C L C 956\n[Peshawar]\nBefore Muhammad Bashir Khan Jehangiri, J\n"
        "Mst. KAUSAR---Petitioner\nversus\nMUHAMMAD MUSHTAQ---Respondent\n"
        "Civil Revision No.329 of 1989, decided on 22nd November, 1989.\n"
        "(a) Civil Procedure Code---\n"
        + _pad(40)
        + "\nPLD 1990 Lah. 4 not applicable to the facts.\n"
        + _pad(20)
    )
    out = extract_judgment_deterministic(html=None, text=body, source_meta={"citation": "1992 CLC 956"})
    assert out["citations"][0] == "1992 CLC 956"
    assert "PLD 1990 Lahore 4" not in out["citations"]
    assert "PLD 1990 Lahore 4" in out["citations_cited"]


def test_clc_2152_body_pld_sc_cite_is_not_an_alias():
    """1993 CLC 2152-style: judgment body cites PLD 1990 SC 1 — must not become an alias."""
    body = (
        "1993 C L C 2152\n[Lahore]\nBefore Khalid Mahmood, J\n"
        "ABDUL HAQUE---Petitioner\nversus\nTHE STATE---Respondent\n"
        "Criminal Appeal No. 12 of 1992, decided on 5th January, 1993.\n"
        "(a) Penal Code---\n"
        + _pad(30)
        + "\nThe principle in PLD 1990 SC 1 was discussed.\n"
        + _pad(20)
    )
    out = extract_judgment_deterministic(html=None, text=body, source_meta={"citation": "1993 CLC 2152"})
    assert "1993 CLC 2152" in out["citations"]
    assert "PLD 1990 SC 1" not in out["citations"]
    assert "PLD 1990 SC 1" in out["citations_cited"]


def test_parallel_citation_in_caption_is_kept():
    body = (
        "1990 PLD SC 1 1990 SCMR 500\n[Peshawar]\nBefore A Judge, J\n"
        "X---Petitioner\nversus\nY---Respondent\n"
        "Civil Suit No. 1 of 1990, decided on 1st January, 1990.\n"
        + _pad(60)
    )
    out = extract_judgment_deterministic(html=None, text=body, source_meta={"citation": "PLD 1990 SC 1"})
    assert "PLD 1990 SC 1" in out["citations"]
    assert "1990 SCMR 500" in out["citations"]


def test_present_dot_bench_line_parses_judges():
    head = (
        "PLD 1990 SC 1\nSUPREME COURT OF PAKISTAN\n"
        "Present. Muhammad Afzal Zullah and Usman Ali Shah, JJ\n"
        "Federation of Pakistan---Petitioner\nversus\nProvince of Punjab---Respondent\n"
    )
    bench = parse_bench(head + _pad(30))
    assert len(bench.judge_names) >= 2
    assert any("Zullah" in n for n in bench.judge_names)


def test_pld_1990_sc_1_present_scores_at_least_0_8():
    text = (
        "PLD 1990 SC 1\nSUPREME COURT OF PAKISTAN\n"
        "Present: Muhammad Afzal Zullah and Usman Ali Shah, JJ\n"
        "Federation---Petitioner\nversus\nProvince---Respondent\n"
        "Constitution Petition No. 1 of 1990, decided on 1st March, 1990.\n"
        + _pad(80)
    )
    out = extract_judgment_deterministic(html=None, text=text, source_meta={"citation": "PLD 1990 SC 1"})
    fields = {
        "citation": out["citations"][0],
        "court": "Supreme Court of Pakistan",
        "year": 1990,
        "case_title": out.get("title") or "Federation versus Province",
        "judges": out.get("judge_names") or [],
        "content": text,
    }
    assert score_confidence(fields) >= 0.8


def test_score_confidence_missing_judges_penalty():
    fields = {
        "citation": "PLD 2024 SC 1",
        "court": "Supreme Court of Pakistan",
        "year": 2024,
        "judges": [],
        "content": "x" * 600,
    }
    assert score_confidence(fields) == 0.6
    fields_with_judges = {
        **fields,
        "case_title": "Alpha versus Beta Corporation Limited",
        "judges": ["A Judge"],
    }
    assert score_confidence(fields_with_judges) == 1.0


def test_caption_slice_excludes_versus_block():
    body = "1992 CLC 956\n[Peshawar]\nBefore J\nA versus B\nPLD 1990 Lahore 4\n"
    cap = judgment_caption_slice(body)
    assert "versus" not in cap.lower() or cap.index("versus") > cap.find("Before")
    assert "PLD 1990" not in cap


def test_alias_suspicion_reason_ref_only_in_body():
    body = (
        "1992 CLC 956\nBefore J\nA versus B\n"
        "decided on 1st January, 1990.\n"
        "(a) Topic---\nPLD 1990 Lahore 4 ref.\n"
    )
    cap = caption_citation_strings(body)
    assert _alias_suspicion_reason("PLD 1990 Lahore 4", body, cap) == "ref-only mention"
