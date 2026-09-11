"""Privacy (tests 1–3) and raw-first (tests 5–7), plus cache/cost (tests 13–15) and public engine (22–23)."""

from __future__ import annotations

import asyncio
import json

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.extractors.scrapegraph_base import ExtractionInput, PrivacyViolation, assert_public_material
from scraper.extractors.scrapegraph_local import LocalScrapeGraphEngine
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine
from scraper.fetchers import record_provenance, sha256_text, stage_judgment
from scraper.models import ExtractionAudit, ScrapegraphCache, ScraperStaging, SgaiUsageDaily, SourceProvenance
from scraper.parsers.text_cleaner import clean_html
from tests.fixtures import JUDGMENT_HTML, JUDGMENT_TEXT, FakeManagedClient, fake_local_transport

AI_RESULT = {"citations": ["PLD 2024 SC 101"], "case_title": "Muhammad Akram versus The State", "court": "Supreme Court of Pakistan", "judge_names": ["Qazi Faez Isa", "Syed Mansoor Ali Shah", "Muhammad Ali Mazhar"], "bench_size": 3, "bench_type": "full", "decision_date": "2024-03-12", "year": 2024, "extractor_confidence": 0.9, "field_evidence": {"citations": "PLD 2024 SC 101"}}


def managed_with(client: FakeManagedClient) -> ManagedScrapeGraphEngine:
    return ManagedScrapeGraphEngine(client_factory=lambda: client)


def local_with(payload, **kw) -> LocalScrapeGraphEngine:
    fake_local_transport.requests.clear()
    return LocalScrapeGraphEngine(base_url="http://ollama.local:11434", model="llama3", provider="ollama", transport=fake_local_transport(payload, **kw))


# --------------------------------------------------------------------------- 1
async def test_login_session_page_never_reaches_managed_client(db, login_source):
    client = FakeManagedClient(result=AI_RESULT)
    login_source.extraction_mode = "hybrid"
    extractor = HybridExtractor(db, login_source, force_ai=True, managed=managed_with(client), local=LocalScrapeGraphEngine(base_url="", model=""))
    outcome = await extractor.extract_judgment(html=JUDGMENT_HTML, source_meta={"url": "https://www.pakistanlawsite.com/case/1"})
    assert client.calls == []
    assert outcome.engine == "deterministic"
    assert outcome.ai_status in ("ai_skipped", "none")
    # even when an operator forces managed mode on the login source, the engine refuses
    login_source.extraction_mode = "scrapegraph_managed"
    outcome2 = await extractor.extract_judgment(html=JUDGMENT_HTML, source_meta={"url": "https://www.pakistanlawsite.com/case/1"})
    assert client.calls == [] and outcome2.ai_status == "privacy_blocked"
    inp = ExtractionInput(source_name="PakistanLawSite", access_method="login_session", content_hash="x", html=JUDGMENT_HTML)
    res = await managed_with(client).extract("judgment", inp)
    assert res.status == "privacy_blocked" and client.calls == []


# --------------------------------------------------------------------------- 2
async def test_cookies_and_storage_state_never_in_engine_payload(db, login_source):
    cookie = "ASP.NET_SessionId=abcdef0123456789abcdef"
    token = settings.encrypt_value(json.dumps({"cookies": [{"name": "sid", "value": "secret-cookie-value-xyz"}]}))
    html = JUDGMENT_HTML.replace("<body>", f"<body><!-- {cookie} Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.abcdefgh storage_state={token} -->")
    engine = local_with(AI_RESULT)
    extractor = HybridExtractor(db, login_source, force_ai=True, local=engine)
    await extractor.extract_judgment(html=html, source_meta={"url": "https://www.pakistanlawsite.com/case/1"})
    assert fake_local_transport.requests, "local engine should have been called"
    sent = b"".join(r.content for r in fake_local_transport.requests).decode()
    assert "secret-cookie-value" not in sent and token not in sent and "eyJhbGciOiJIUzI1NiJ9" not in sent
    assert "abcdef0123456789abcdef" not in sent
    with pytest.raises(PrivacyViolation):
        assert_public_material(ExtractionInput(source_name="x", access_method="public", content_hash="h", text="t", source_meta={"cookies": {"a": "b"}}), "managed")


# --------------------------------------------------------------------------- 3
async def test_managed_client_accepts_only_public_content():
    client = FakeManagedClient(result=AI_RESULT)
    eng = managed_with(client)
    ok = await eng.extract("judgment", ExtractionInput(source_name="SupremeCourt", access_method="public", content_hash="h1", html=JUDGMENT_HTML))
    assert ok.ok and len(client.calls) == 1
    for forbidden in ("cookies", "headers", "stealth"):
        assert forbidden not in client.calls[0]
    blocked = await eng.extract("judgment", ExtractionInput(source_name="PakistanLawSite", access_method="login_session", content_hash="h2", html=JUDGMENT_HTML))
    assert blocked.status == "privacy_blocked" and len(client.calls) == 1


# --------------------------------------------------------------------------- 5
async def test_provenance_and_staging_written_before_engine_call(db, source):
    order = []

    class RecordingClient(FakeManagedClient):
        def smartscraper(self, **kwargs):
            order.append("engine")
            return super().smartscraper(**kwargs)

    client = RecordingClient(result=AI_RESULT)
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/j1", content=JUDGMENT_HTML.encode(), content_kind="html", route={"t": 0})
    order.append("provenance")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=JUDGMENT_HTML, raw_text=clean_html(JUDGMENT_HTML), url="http://127.0.0.1/j1")
    order.append("staging")
    await db.flush()
    assert (await db.execute(select(func.count()).select_from(SourceProvenance))).scalar() == 1
    assert (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() == 1
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client), provenance_id=prov.id, staging_id=st.id)
    await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=prov.content_hash)
    assert order == ["provenance", "staging", "engine"]


# --------------------------------------------------------------------------- 6
async def test_engine_timeout_leaves_raw_page_stored(db, source, monkeypatch):
    monkeypatch.setattr(settings, "SGAI_TIMEOUT_SECONDS", 1)
    client = FakeManagedClient(result=AI_RESULT, delay=3.0)
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/j2", content=JUDGMENT_HTML.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=JUDGMENT_HTML, raw_text=clean_html(JUDGMENT_HTML), url="http://127.0.0.1/j2")
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client), provenance_id=prov.id, staging_id=st.id)
    outcome = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=prov.content_hash)
    assert outcome.ai_status == "ai_failed" and outcome.engine == "deterministic"
    assert outcome.data["citations"] == ["PLD 2024 SC 101"]
    from scraper.fetchers import read_raw

    assert read_raw(prov.raw_ref) == JUDGMENT_HTML.encode()
    audit = (await db.execute(select(ExtractionAudit))).scalars().first()
    assert audit.status == "ai_failed"


# --------------------------------------------------------------------------- 7
async def test_invalid_engine_json_does_not_lose_record(db, source):
    client = FakeManagedClient(result="this is not json at all")
    prov = await record_provenance(db, source=source, url="http://127.0.0.1/j3", content=JUDGMENT_HTML.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=JUDGMENT_HTML, raw_text=clean_html(JUDGMENT_HTML), url="http://127.0.0.1/j3")
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client), provenance_id=prov.id, staging_id=st.id)
    outcome = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=prov.content_hash)
    assert outcome.ai_status == "invalid_json"
    assert outcome.data["citations"] == ["PLD 2024 SC 101"] and outcome.data["full_text_candidate"]
    assert (await db.execute(select(func.count()).select_from(ScraperStaging))).scalar() == 1


# --------------------------------------------------------------------------- 13
async def test_identical_hash_and_schema_is_cache_hit(db, source):
    client = FakeManagedClient(result=AI_RESULT)
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client))
    o1 = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=sha256_text(JUDGMENT_HTML))
    o2 = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=sha256_text(JUDGMENT_HTML))
    assert o1.ai_status == "ok" and o2.ai_status == "cache_hit" and o2.engine == "hybrid:cache"
    assert len(client.calls) == 1
    assert (await db.execute(select(func.count()).select_from(ScrapegraphCache))).scalar() == 1
    # a new schema version must not reuse the cached result
    source.scrapegraph_schema_version = 2
    o3 = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=sha256_text(JUDGMENT_HTML))
    assert o3.ai_status == "ok" and len(client.calls) == 2


# --------------------------------------------------------------------------- 14
async def test_daily_credit_cap_reached_continues_deterministic(db, source, monkeypatch, caplog):
    monkeypatch.setattr(settings, "SGAI_DAILY_CREDIT_CAP", 1)
    client = FakeManagedClient(result=AI_RESULT)
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client))
    o1 = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash="a" * 64)
    assert o1.ai_status == "ok"
    import logging

    with caplog.at_level(logging.ERROR):
        o2 = await extractor.extract_judgment(html=JUDGMENT_HTML.replace("101", "102"), content_hash="b" * 64)
    assert o2.ai_status == "budget_exhausted" and o2.engine == "deterministic" and o2.data["citations"]
    assert "SGAI_BUDGET_EXHAUSTED" in caplog.text
    assert len(client.calls) == 1


# --------------------------------------------------------------------------- 15
async def test_circuit_breaker_open_continues_deterministic(db, source, monkeypatch):
    monkeypatch.setattr(settings, "SGAI_CIRCUIT_BREAKER_FAILURES", 2)
    from scraper.extractors.scrapegraph_base import MANAGED_BREAKER

    MANAGED_BREAKER.threshold = 2
    client = FakeManagedClient(error=RuntimeError("503 upstream"))
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client))
    for i in range(2):
        o = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash=f"{i}" * 64)
        assert o.ai_status == "ai_failed" and o.data["citations"]
    assert MANAGED_BREAKER.is_open
    o3 = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash="z" * 64)
    assert o3.ai_status == "circuit_open" and o3.engine == "deterministic" and o3.data["citations"] == ["PLD 2024 SC 101"]
    assert len(client.calls) == 2
    MANAGED_BREAKER.threshold = settings.SGAI_CIRCUIT_BREAKER_FAILURES


# --------------------------------------------------------------------------- 22 / 23
async def test_public_html_managed_extract_returns_schema_json(db, source):
    client = FakeManagedClient(result={**AI_RESULT, "unexpected_key": "dropped", "headnotes": None})
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client))
    o = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash="c" * 64)
    assert o.ai_status == "ok"
    from scraper.extractors.schemas import JudgmentExtraction

    JudgmentExtraction.model_validate({k: v for k, v in o.ai_json.items()})
    assert "unexpected_key" not in o.ai_json
    assert o.data["decision_date"] == "2024-03-12" and o.data["bench_size"] == 3


async def test_managed_unavailable_falls_back_to_deterministic(db, source):
    client = FakeManagedClient(error=ConnectionError("network down"))
    extractor = HybridExtractor(db, source, force_ai=True, managed=managed_with(client))
    o = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash="d" * 64)
    assert o.ai_status == "ai_failed" and o.engine == "deterministic" and o.data["citations"] == ["PLD 2024 SC 101"]
    usage = (await db.execute(select(SgaiUsageDaily).where(SgaiUsageDaily.source_name == "*"))).scalars().first()
    assert usage.failures == 1


async def test_local_engine_ok_for_login_session_on_prem_only(db, login_source):
    extractor = HybridExtractor(db, login_source, force_ai=True, local=local_with(AI_RESULT))
    o = await extractor.extract_judgment(html=JUDGMENT_HTML, content_hash="e" * 64)
    assert o.ai_status == "ok" and o.engine == "hybrid:local"
    # a public endpoint is refused for login-session text
    bad = LocalScrapeGraphEngine(base_url="https://api.example.com", model="m", transport=fake_local_transport(AI_RESULT))
    res = await bad.extract("judgment", ExtractionInput(source_name="PakistanLawSite", access_method="login_session", content_hash="f", html=JUDGMENT_HTML))
    assert res.status == "privacy_blocked"
