"""False citation aliases: caption-only own citations, Present bench lines, cleanup scanner."""

from __future__ import annotations

import pytest

from scraper.extractors.deterministic import citation_referred_in_text, extract_judgment_deterministic
from scraper.parsers.bench_parser import parse_bench
from scraper.parsers.citation_extractor import score_confidence
from scraper.parsers.case_metadata import judgment_caption_slice
from scraper.tasks.citation_alias_cleanup import alias_suspicion_reason, caption_citation_strings, legitimate_parallel_alias


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


def test_pld_1990_sc_1_present_scores_at_least_0_85_real_layout():
    text = (
        "PLD 1990 SC 1\n"
        "IN THE SUPREME COURT OF PAKISTAN\n"
        "Present. Muhammad Afzal Zullah and Usman Ali Shah, JJ\n"
        "Federation of Pakistan---Petitioner\n"
        "versus\n"
        "Province of Punjab---Respondent\n"
        "Constitution Petition No. 1 of 1990, decided on 1st March, 1990.\n"
        + _pad(80)
    )
    out = extract_judgment_deterministic(html=None, text=text, source_meta={"citation": "PLD 1990 SC 1"})
    title = out.get("case_title") or out.get("title")
    assert title and "versus" in title.lower()
    fields = {
        "citation": out["citations"][0],
        "court": "Supreme Court of Pakistan",
        "year": 1990,
        "case_title": title,
        "judges": out.get("judge_names") or [],
        "content": text,
    }
    assert score_confidence(fields) >= 0.85


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


def test_cleanup_does_not_flag_pld_scmr_parallel_heading():
    body = (
        "PLD 1990 SC 1\n1990 SCMR 500\nSUPREME COURT OF PAKISTAN\n"
        "Before A Judge, J\nX---Petitioner\nversus\nY---Respondent\n"
        "Civil Suit No. 1 of 1990, decided on 1st January, 1990.\n"
        "(a) Topic---\n"
        + _pad(20)
        + "\nAs held in 1990 SCMR 500 the test applies.\n"
        + _pad(10)
    )
    assert legitimate_parallel_alias("1990 SCMR 500", body, primary="PLD 1990 SC 1")
    assert alias_suspicion_reason("1990 SCMR 500", body, primary="PLD 1990 SC 1") is None


def test_cleanup_flags_clc_956_pld_lahore_body_mention():
    body = (
        "1992 C L C 956\n[Peshawar]\nBefore Muhammad Bashir Khan Jehangiri, J\n"
        "Mst. KAUSAR---Petitioner\nversus\nMUHAMMAD MUSHTAQ---Respondent\n"
        "Civil Revision No.329 of 1989, decided on 22nd November, 1989.\n"
        "(a) Civil Procedure Code---\n"
        + _pad(40)
        + "\nPLD 1990 Lah. 4 not applicable to the facts.\n"
        + _pad(20)
    )
    assert alias_suspicion_reason("PLD 1990 Lahore 4", body, primary="1992 CLC 956") == "ref-only mention"
    assert not legitimate_parallel_alias("PLD 1990 Lahore 4", body, primary="1992 CLC 956")


def test_caption_v_dot_cut_excludes_party_line_citations():
    """2017 CLD 1737-style SHC caption: party 'v.' must not widen the caption block."""
    body = (
        "2017 CLD 1737\n[Lahore]\n"
        "IN THE SINDH HIGH COURT\n"
        "Khalid Mehmood & others v. M/s. Multi Plus\n"
        "Before A Judge, J\n"
        "X---Petitioner\nversus\nY---Respondent\n"
        "Suit No. 1 of 2017, decided on 1st January, 2017.\n"
        "(a) Topic---\n"
        + _pad(15)
        + "\n2016 SCMR 213 discussed.\n"
    )
    cap = judgment_caption_slice(body)
    assert "2016 SCMR" not in cap
    assert "Multi Plus" not in cap or " v. " in cap
    out = extract_judgment_deterministic(html=None, text=body, source_meta={"citation": "2017 CLD 1737"})
    assert "2016 SCMR 213" not in out["citations"]


def test_headnote_ref_list_flags_every_cite_before_marker():
    from scraper.parsers.citation_extractor import extract_citations

    line = (
        "2003 CLC 1\n[Peshawar]\nBefore J, J\nA versus B\n"
        "decided on 1st January, 2003.\n"
        "(a) Topic---\n"
        "X 2003 SCMR 74; Y 2003 SCMR 774 and Z 2003 SCMR 1915 ref."
    )
    hits = [c for c in extract_citations(line) if "SCMR" in (c.get("normalized") or "")]
    assert len(hits) == 3
    for c in hits:
        assert citation_referred_in_text(line, c["span"][0], c["span"][1])


def test_caption_parallel_scmr_plc_slash_kept():
    body = (
        "1991 SCMR 100 / 1991 PLC 200\n[Lahore]\nBefore J, J\n"
        "A---Petitioner\nversus\nB---Respondent\n"
        "decided on 1st January, 1991.\n"
        + _pad(40)
    )
    assert legitimate_parallel_alias("1991 PLC 200", body, primary="1991 SCMR 100")
    assert alias_suspicion_reason("1991 PLC 200", body, primary="1991 SCMR 100") is None


def test_clc_1587_headnote_list_rel_flags_all_cites():
    headnote = (
        "1984 CLC 1587\n[Lahore]\nBefore J, J\nA versus B\n"
        "decided on 1st January, 1984.\n"
        "(a) Topic---\n"
        "1981 SCMR 829 and PLD 1980 Karachi 99 rel."
    )
    from scraper.parsers.citation_extractor import extract_citations, normalise_citation

    for c in extract_citations(headnote):
        norm = c.get("normalized") or normalise_citation(c["raw"])
        if norm == "1984 CLC 1587":
            continue
        assert citation_referred_in_text(headnote, c["span"][0], c["span"][1])


def test_also_reported_parallel_after_versus_kept():
    body = (
        "1990 PLD SC 1\n[Peshawar]\nBefore J, J\n"
        "A---Petitioner\nversus\nB---Respondent\n"
        "Also reported as 1990 SCMR 999\n"
        "decided on 1st March, 1990.\n"
        + _pad(40)
    )
    assert legitimate_parallel_alias("1990 SCMR 999", body, primary="PLD 1990 SC 1")


def test_extract_own_includes_also_reported_line():
    body = (
        "1990 PLD SC 1\n[Peshawar]\nBefore J, J\n"
        "A---Petitioner\nversus\nB---Respondent\n"
        "Also reported as 1990 SCMR 999\n"
        "decided on 1st March, 1990.\n"
        + _pad(40)
    )
    out = extract_judgment_deterministic(html=None, text=body, source_meta={"citation": "PLD 1990 SC 1"})
    assert "PLD 1990 SC 1" in out["citations"]
    assert "1990 SCMR 999" in out["citations"]


def test_alias_suspicion_reason_body_mention_without_ref():
    body = (
        "1992 CLC 956\nBefore J\nA versus B\n"
        "decided on 1st January, 1990.\n"
        "(a) Topic---\nThe court followed PLD 1990 Lahore 4 on the merits.\n"
    )
    assert alias_suspicion_reason("PLD 1990 Lahore 4", body, primary="1992 CLC 956") == "body mention"


def test_alias_suspicion_reason_ref_only_in_body():
    body = (
        "1992 CLC 956\nBefore J\nA versus B\n"
        "decided on 1st January, 1990.\n"
        "(a) Topic---\nPLD 1990 Lahore 4 ref.\n"
    )
    assert alias_suspicion_reason("PLD 1990 Lahore 4", body, primary="1992 CLC 956") == "ref-only mention"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backup_value, must_refuse",
    [
        (None, False),
        ("[]", False),
        ('[{"id":"00000000-0000-0000-0000-000000000001"}]', True),
        ("{}", True),
        ("not-json", True),
    ],
)
async def test_cleanup_undo_refuses_destructive_promoted_deletion(backup_value, must_refuse):
    """Undo cannot assume 'promoted' means 'newly created' or unchanged."""
    from types import SimpleNamespace

    from scraper.tasks.citation_alias_cleanup import _delete_requalify_judgments

    class FakeResult:
        def scalars(self):
            return self

        def first(self):
            return None if backup_value is None else SimpleNamespace(value=backup_value)

    class FakeDB:
        def __init__(self):
            self.delete_calls = 0

        async def execute(self, _query):
            return FakeResult()

        async def delete(self, _obj):
            self.delete_calls += 1
            raise AssertionError("No judgment may be deleted without independent proof")

    db = FakeDB()
    if must_refuse:
        with pytest.raises(RuntimeError, match="Refusing undo"):
            await _delete_requalify_judgments(db, "test-run")
    else:
        assert await _delete_requalify_judgments(db, "test-run") == 0
    assert db.delete_calls == 0
