"""The read-only corpus API for the Sikander legal research and drafting agent (cursor-init
docs/agents/sikander-legal-agent): search, a judgment by citation, citation verification, citing judgments
with their treatment, and a statute section. Its own key (CORPUS_API_KEY), never the admin key, and every
query runs as the corpus_writer role in a read-only transaction."""

from __future__ import annotations

from datetime import date

import pytest

from scraper.config import settings
from scraper.models import Citation, Judgment, Statute, StatuteSection, StatuteSectionVersion, Treatment

KEY = "test-corpus-key"
H = {"X-Corpus-Key": KEY}


@pytest.fixture(autouse=True)
def _corpus_key(monkeypatch):
    from scraper.routers import corpus_read

    monkeypatch.setattr(settings, "CORPUS_API_KEY", KEY)
    corpus_read._KEY_FAILS.clear()
    yield
    corpus_read._KEY_FAILS.clear()


async def _seed(db):
    bail = Judgment(
        canonical_citation="PLD 2020 SC 1",
        case_title="Muhammad Aslam v. The State",
        court_name="Supreme Court of Pakistan",
        year=2020,
        decision_date=date(2020, 3, 4),
        judge_names=["Justice A", "Justice B"],
        bench_size=2,
        bench_type="division",
        reporter="PLD",
        full_text="The petitioner seeks post-arrest bail. The case calls for further inquiry under section 497(2) CrPC.",
        source_url="https://example.test/pld2020sc1",
    )
    later = Judgment(
        canonical_citation="2022 SCMR 50",
        case_title="Zahid v. The State",
        court_name="Supreme Court of Pakistan",
        year=2022,
        reporter="SCMR",
        full_text="Relying on PLD 2020 SC 1, bail is granted as the matter calls for further inquiry.",
    )
    land = Judgment(
        canonical_citation="2019 CLC 77",
        case_title="Ahmad v. Province of Punjab",
        court_name="Lahore High Court",
        year=2019,
        reporter="CLC",
        full_text="A mutation entry does not confer title.",
    )
    db.add_all([bail, later, land])
    await db.flush()
    db.add(Citation(judgment_id=bail.id, citation_string="PLD 2020 SC 1", is_primary=True))
    db.add(Citation(judgment_id=bail.id, citation_string="2020 SCMR 999"))
    db.add(
        Treatment(
            citing_judgment_id=later.id,
            cited_judgment_id=bail.id,
            cited_citation="PLD 2020 SC 1",
            label="followed",
            confidence=0.9,
            evidence_passage="Relying on PLD 2020 SC 1, bail is granted",
            method="deterministic",
        )
    )
    crpc = Statute(name="Code of Criminal Procedure, 1898", short_name="CrPC", year_enacted=1898)
    db.add(crpc)
    await db.flush()
    sec = StatuteSection(statute_id=crpc.id, section_number="497", section_title="When bail may be taken in case of non-bailable offence")
    db.add(sec)
    await db.flush()
    ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text="(1) When any person accused of any non-bailable offence...", text_hash="h497", version_confidence=0.5)
    db.add(ver)
    await db.flush()
    sec.current_version_id = ver.id
    await db.commit()


async def test_the_api_needs_its_own_key(db, client, monkeypatch):
    assert client.get("/corpus/v1/judgments/search", params={"q": "bail"}).status_code == 401
    assert client.get("/corpus/v1/judgments/search", params={"q": "bail"}, headers={"X-Corpus-Key": "wrong"}).status_code == 401
    # the admin key is not a corpus key
    assert client.get("/corpus/v1/judgments/search", params={"q": "bail"}, headers={"X-Corpus-Key": "test-admin-key"}).status_code == 401
    monkeypatch.setattr(settings, "CORPUS_API_KEY", "")
    off = client.get("/corpus/v1/judgments/search", params={"q": "bail"}, headers=H)
    assert off.status_code == 503 and "not configured" in off.json()["detail"]


async def test_wrong_corpus_keys_lock_out_only_the_corpus_api(db, client, admin_headers):
    """A misconfigured agent hammering the corpus API must not lock the operator out of /admin."""
    from scraper.routers import auth

    before = auth._auth_failures("testclient")
    for _ in range(20):
        assert client.get("/corpus/v1/judgments/search", params={"q": "bail"}, headers={"X-Corpus-Key": "wrong"}).status_code == 401
    assert client.get("/corpus/v1/judgments/search", params={"q": "bail"}, headers=H).status_code == 429
    assert auth._auth_failures("testclient") == before  # the admin counter did not move


async def test_search_matches_every_word_in_the_text_and_filters_by_court_and_year(db, client):
    await _seed(db)
    hits = client.get("/corpus/v1/judgments/search", params={"q": "further inquiry bail"}, headers=H).json()["results"]
    assert [h["citation"] for h in hits] == ["2022 SCMR 50", "PLD 2020 SC 1"]  # newest first
    assert "further inquiry" in hits[1]["snippet"].lower()
    filtered = client.get(
        "/corpus/v1/judgments/search",
        params={"q": "bail", "court": "supreme", "year_from": 2021, "year_to": 2023},
        headers=H,
    ).json()["results"]
    assert [h["citation"] for h in filtered] == ["2022 SCMR 50"]
    assert client.get("/corpus/v1/judgments/search", params={"q": "mutation", "court": "Lahore"}, headers=H).json()["results"][0]["citation"] == "2019 CLC 77"
    assert client.get("/corpus/v1/judgments/search", params={"q": "x"}, headers=H).status_code == 422  # too short


async def test_a_judgment_is_fetched_by_any_of_its_citations_with_full_text(db, client):
    await _seed(db)
    j = client.get("/corpus/v1/judgments/by-citation", params={"citation": "PLD 2020 SC 1"}, headers=H).json()
    assert j["case_title"] == "Muhammad Aslam v. The State" and "497(2)" in j["full_text"]
    assert j["judges"] == ["Justice A", "Justice B"] and j["decision_date"] == "2020-03-04"
    alt = client.get("/corpus/v1/judgments/by-citation", params={"citation": "2020 SCMR 999"}, headers=H).json()
    assert alt["citation"] == "PLD 2020 SC 1"
    missing = client.get("/corpus/v1/judgments/by-citation", params={"citation": "PLD 1999 SC 12345"}, headers=H)
    assert missing.status_code == 404


async def test_verify_says_found_or_not_and_never_guesses(db, client):
    await _seed(db)
    ok = client.get("/corpus/v1/citations/verify", params={"citation": "PLD 2020 SC 1"}, headers=H).json()
    assert ok["found"] is True and ok["canonical_citation"] == "PLD 2020 SC 1"
    assert ok["court"] == "Supreme Court of Pakistan" and ok["year"] == 2020
    assert sorted(ok["alternative_citations"]) == ["2020 SCMR 999"]
    no = client.get("/corpus/v1/citations/verify", params={"citation": "PLD 2031 SC 1"}, headers=H).json()
    assert no["found"] is False and no["canonical_citation"] is None


async def test_citing_judgments_carry_their_treatment_and_evidence(db, client):
    await _seed(db)
    body = client.get("/corpus/v1/judgments/citing", params={"citation": "PLD 2020 SC 1"}, headers=H).json()
    assert body["found"] is True
    assert body["citing"] == [
        {
            "citation": "2022 SCMR 50",
            "case_title": "Zahid v. The State",
            "court": "Supreme Court of Pakistan",
            "year": 2022,
            "treatment": "followed",
            "confidence": 0.9,
            "evidence": "Relying on PLD 2020 SC 1, bail is granted",
            "reviewed": False,
        }
    ]
    none = client.get("/corpus/v1/judgments/citing", params={"citation": "2019 CLC 77"}, headers=H).json()
    assert none == {"citation": "2019 CLC 77", "found": True, "citing": []}


async def test_a_statute_section_is_returned_with_its_current_version(db, client):
    await _seed(db)
    for statute in ("CrPC", "code of criminal procedure"):
        p = client.get("/corpus/v1/statutes/section", params={"statute": statute, "section": "497"}, headers=H).json()
        assert p["statute_name"] == "Code of Criminal Procedure, 1898"
        assert p["section_number"] == "497" and p["text"].startswith("(1) When any person")
        assert p["version_no"] == 1 and p["version_confidence"] == 0.5
    assert client.get("/corpus/v1/statutes/section", params={"statute": "CrPC", "section": "9999"}, headers=H).status_code == 404


async def test_queries_run_as_the_read_only_reader_role(db, client):
    from scraper.routers.corpus_read import reader_session

    async for s in reader_session():
        from sqlalchemy import text

        assert (await s.execute(text("SELECT current_user"))).scalar() == settings.CORPUS_WRITER_ROLE
        assert (await s.execute(text("SHOW transaction_read_only"))).scalar() == "on"
