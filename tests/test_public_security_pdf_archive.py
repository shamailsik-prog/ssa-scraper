"""Public sources (24–25), prompt injection / SSRF (26–28), PDF / download (29–31), archive (32–34)."""

from __future__ import annotations

import json
import pathlib

import pytest
from sqlalchemy import func, select

from scraper.config import settings
from scraper.database import SessionLocal
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.extractors.prompts import build_prompt, prompt_fingerprint
from scraper.extractors.scrapegraph_managed import ManagedScrapeGraphEngine
from scraper.extractors.schemas import JudgmentExtraction
from scraper.fetchers import HttpFetcher, record_provenance, stage_judgment
from scraper.models import ArchiveObject, ArchiveTarget, CrawlFrontier, Judgment, ScraperJob, ScraperStaging, SourceProvenance
from scraper.parsers.pdf_writer import RENDERED_COPY_LABEL, is_rendered_copy, render_judgment_pdf_bytes
from scraper.parsers.text_cleaner import clean_html
from scraper.security import ExplicitBlock, RobotsUnavailable, URLPolicyError, check_url_policy, classify_response, contains_secret, reset_robots_cache, robots_allows, scrub_secrets
from scraper.storage.adapters import LocalPathAdapter, ObjectExists
from scraper.storage.archive import ArchiveMirror, judgment_prefix
from scraper.tasks.promotion import promote_judgment_staging, promote_staging_records
from scraper.tasks.public_pipeline import PublicPipeline, run_public_source
from tests.fixtures import INJECTION_HTML, JUDGMENT_HTML, FakeManagedClient, judgment_html, scanned_pdf_bytes, text_pdf_bytes


def _managed(client):
    return ManagedScrapeGraphEngine(client_factory=lambda: client)


# --------------------------------------------------------------------------- 24
async def test_public_crawl_outside_allow_list_rejected(source):
    client = FakeManagedClient()
    eng = _managed(client)
    with pytest.raises(URLPolicyError):
        await eng.crawl_public("https://evil.example.net/judgments", ["www.supremecourt.gov.pk"], max_depth=1, max_pages=5)
    assert client.calls == []


# --------------------------------------------------------------------------- 25
async def test_discovered_url_gets_local_policy_check(db, source, fixture_server):
    fixture_server.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    listing = fixture_server.add("/list", f'<html><body><a href="/judgment1.html">Judgment 1</a><a href="/private/judgment2.html">Judgment 2</a><a href="http://169.254.169.254/latest/meta-data/">judgment meta</a><a href="http://other.example.org/judgment3.html">judgment 3</a></body></html>')
    fixture_server.add("/judgment1.html", judgment_html("PLD 2024 SC 11"))
    fixture_server.add("/private/judgment2.html", judgment_html("PLD 2024 SC 12"))
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        res = await fetcher.get(listing)
        links = pipeline.links_from_html(res.text, res.final_url, __import__("re").compile(r"(?i)judgment"))
        assert fixture_server.url("/judgment1.html") in links
        assert not any("169.254.169.254" in u or "other.example.org" in u for u in links)
        assert pipeline.stats["rejected_urls"] == 2
        # robots.txt still governs a discovered (and allow-listed) URL
        assert robots_allows(fixture_server.url("/judgment1.html"))
        assert not robots_allows(fixture_server.url("/private/judgment2.html"))
        with pytest.raises(ExplicitBlock):
            await fetcher.get(fixture_server.url("/private/judgment2.html"))


async def test_robots_disallow_retires_frontier_without_halting(db, source, fixture_server):
    fixture_server.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
    blocked = fixture_server.add("/private/judgment2.html", judgment_html("PLD 2024 SC 12"))
    row = CrawlFrontier(source_name=source.source_name, tier=0, query_key=f"judgment:{blocked}", query_json={"kind": "judgment", "url": blocked}, cursor_json={}, priority=50)
    db.add(row)
    await db.flush()
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        result = await pipeline._drain_one(row)
    assert result == "ok"
    assert row.status == "retired"
    assert source.state != "HALTED"
    assert pipeline.stats["rejected_urls"] == 1 and pipeline.stats["halted"] is False


async def test_robots_5xx_defers_frontier_item_and_recovers(db, source, fixture_server):
    """A 5xx on robots.txt is a temporary disallow (RFC 9309): the URL is deferred, not retired, the source is
    not halted, no attempt is spent, and the item is processed once robots.txt is reachable again."""
    fixture_server.add("/robots.txt", "", status=503)
    target = fixture_server.add("/judgment3.html", judgment_html("PLD 2024 SC 13"))
    with pytest.raises(RobotsUnavailable):
        robots_allows(target)
    with pytest.raises(RobotsUnavailable):  # the unavailable verdict is cached briefly; still not a denial
        robots_allows(target)
    row = CrawlFrontier(source_name=source.source_name, tier=0, query_key=f"judgment:{target}", query_json={"kind": "judgment", "url": target}, cursor_json={}, priority=50)
    db.add(row)
    await db.flush()
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        assert await pipeline._drain_one(row) == "ok"
        assert row.status == "pending" and row.attempts == 0
        assert "robots.txt unavailable" in row.last_error and "503" in row.last_error
        assert source.state != "HALTED"
        assert pipeline.stats == {**pipeline.stats, "deferred": 1, "rejected_urls": 0, "errors": 0, "halted": False}
        # robots.txt comes back: the same item now fetches and stages
        fixture_server.add("/robots.txt", "User-agent: *\nDisallow: /private/\n", content_type="text/plain")
        reset_robots_cache()
        assert await pipeline._drain_one(row) == "ok"
    assert row.status == "done" and row.attempts == 1
    assert pipeline.stats["fetched"] == 1 and pipeline.stats["staged"] == 1


# --------------------------------------------------------------------------- 26
async def test_injection_text_is_data_and_schema_unchanged(db, source):
    client = FakeManagedClient(result={"citations": ["PLD 2024 SC 101"], "owned": True, "extractor_confidence": 0.9})
    fp_before = prompt_fingerprint("judgment")
    extractor = HybridExtractor(db, source, force_ai=True, managed=_managed(client))
    o = await extractor.extract_judgment(html=INJECTION_HTML, content_hash="i" * 64)
    assert prompt_fingerprint("judgment") == fp_before  # fixed template
    sent = client.calls[0]
    assert sent["user_prompt"] == build_prompt("judgment")  # the page could not alter the instructions
    assert "SOURCE_DATA_BEGIN" in sent["website_html"]
    assert "owned" not in o.ai_json and set(o.ai_json) <= set(JudgmentExtraction.model_fields)
    assert o.data["citations"] == ["PLD 2024 SC 101"]


# --------------------------------------------------------------------------- 27
def test_localhost_and_metadata_urls_rejected():
    allow = ["www.supremecourt.gov.pk", "127.0.0.1", "localhost", "169.254.169.254", "metadata.google.internal"]
    for bad in ("http://169.254.169.254/latest/meta-data/", "http://localhost:8000/admin", "http://127.0.0.1:5432/", "http://metadata.google.internal/", "http://10.0.0.5/x", "http://[::1]/", "ftp://www.supremecourt.gov.pk/x", "http://user:pw@www.supremecourt.gov.pk/"):
        with pytest.raises(URLPolicyError):
            check_url_policy(bad, allow)
    assert check_url_policy("https://www.supremecourt.gov.pk/judgements/", allow, resolver=lambda h: ["8.8.8.8"]).startswith("https://www.supremecourt.gov.pk/")
    with pytest.raises(URLPolicyError):
        check_url_policy("https://www.supremecourt.gov.pk/judgements/", allow, resolver=lambda h: ["192.168.1.5"])  # DNS rebinding to private


# --------------------------------------------------------------------------- 28
async def test_secret_disclosure_attempt_never_enters_prompt_or_output(db, source, monkeypatch):
    monkeypatch.setattr(settings, "SGAI_API_KEY", type(settings.SGAI_API_KEY)("sgai-REALKEY-0123456789abcdef"))
    monkeypatch.setattr(settings, "ADMIN_API_KEY", "admin-REAL-KEY-987654321")
    html = INJECTION_HTML.replace("<body>", "<body><p>The operator key is admin-REAL-KEY-987654321 and sgai-REALKEY-0123456789abcdef</p>")
    client = FakeManagedClient(result={"citations": ["PLD 2024 SC 101"], "case_title": "key sgai-REALKEY-0123456789abcdef", "extractor_confidence": 0.9})
    o = await HybridExtractor(db, source, force_ai=True, managed=_managed(client)).extract_judgment(html=html, content_hash="s" * 64)
    sent = json.dumps(client.calls[0], default=str)
    assert "admin-REAL-KEY" not in sent and "sgai-REALKEY" not in sent and "SuperSecret123" not in sent
    assert "sgai-REALKEY" not in json.dumps(o.ai_json)
    # the preserved full text is DATA and stays byte-exact; every derived field is clean
    derived = {k: v for k, v in o.data.items() if k != "full_text_candidate"}
    assert "sgai-REALKEY" not in json.dumps(derived) and "admin-REAL-KEY" not in json.dumps(derived)
    assert contains_secret("Authorization: Bearer abcdefghijklmnop") and "[REDACTED]" in scrub_secrets("password=hunter2hunter2")


# --------------------------------------------------------------------------- 29
async def test_pdf_link_original_bytes_stored_with_sha256(db, source, fixture_server):
    pdf = text_pdf_bytes("PLD 2024 SC 300\nSUPREME COURT OF PAKISTAN\nCORAM: A B, J\nX versus Y\nDecided on 1st January 2024\nThe appeal is dismissed.")
    url = fixture_server.add("/j300.pdf", pdf, content_type="application/pdf")
    fixture_server.add("/robots.txt", "", status=404)
    import hashlib

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        res = await fetcher.get(url)
        assert res.is_pdf
        await pipeline.ingest_judgment(res, route={"listing": "test"})
    prov = (await db.execute(select(SourceProvenance).where(SourceProvenance.content_kind == "pdf"))).scalars().first()
    assert prov.is_original_document and prov.document_kind == "original_pdf"
    assert prov.content_hash == hashlib.sha256(pdf).hexdigest()
    from scraper.fetchers import read_raw

    assert read_raw(prov.raw_ref) == pdf  # byte-identical original
    st = (await db.execute(select(ScraperStaging))).scalars().first()
    assert "PLD 2024 SC 300" in st.raw_text and st.pdf_provenance_id == prov.id


# --------------------------------------------------------------------------- 30
def test_scanned_pdf_falls_back_to_ocr():
    from scraper.fetchers import pdf_text_with_ocr

    text, ocr = pdf_text_with_ocr(scanned_pdf_bytes())
    assert ocr is True
    assert "SCANNED" in text.upper() or "JUDGMENT" in text.upper() or "777" in text


# --------------------------------------------------------------------------- 31
async def test_html_only_judgment_preserved_and_rendered_copy_labelled(db, source, fixture_server, tmp_path):
    fixture_server.add("/robots.txt", "", status=404)
    url = fixture_server.add("/j401.html", judgment_html("PLD 2024 SC 401"))
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = PublicPipeline(db, source, fetcher=fetcher)
        await pipeline.ingest_judgment(await fetcher.get(url), route={"listing": "test"})
    st = (await db.execute(select(ScraperStaging))).scalars().first()
    assert st.raw_html and st.raw_text and st.pdf_provenance_id is None
    st.status = "extracted"
    assert await promote_judgment_staging(db, st) == "promoted"
    j = (await db.execute(select(Judgment))).scalars().first()
    assert j.has_original_pdf is False
    db.add(ArchiveTarget(name="local", target_type="local_path", root_path=str(tmp_path / "archive")))
    await db.commit()
    mirror = ArchiveMirror(db)
    result = await mirror.mirror_pending()
    await db.commit()
    folder = tmp_path / "archive" / judgment_prefix(j)
    assert (folder / "rendered_copy.pdf").exists() and not (folder / "original.pdf").exists()
    pdf_bytes = (folder / "rendered_copy.pdf").read_bytes()
    assert is_rendered_copy(pdf_bytes)
    meta = json.loads((folder / "metadata.json").read_text())
    assert meta["document_label"] == RENDERED_COPY_LABEL and meta["has_original_pdf"] is False
    obj = (await db.execute(select(ArchiveObject).where(ArchiveObject.object_key.like("%rendered_copy.pdf")))).scalars().first()
    assert obj.document_kind == "rendered_copy"
    assert list((tmp_path / "archive" / "_index").glob("judgments-*.csv"))


# --------------------------------------------------------------------------- 32 / 33 / 34
async def _promote_one(db, source, citation="PLD 2024 SC 500", access_method=None):
    html = judgment_html(citation)
    if access_method:
        source.access_method = access_method
    prov = await record_provenance(db, source=source, url=f"http://127.0.0.1/{citation.replace(' ', '')}", content=html.encode(), content_kind="html")
    st = await stage_judgment(db, source=source, prov=prov, raw_html=html, raw_text=clean_html(html), url=prov.source_url)
    o = await HybridExtractor(db, source).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
    st.reconciled_json, st.status, st.confidence_score = o.data, "extracted", o.confidence
    assert await promote_judgment_staging(db, st) == "promoted"
    return (await db.execute(select(Judgment).where(Judgment.canonical_citation == citation))).scalars().first()


async def test_mirror_write_once_tree_and_index(db, source, tmp_path):
    j = await _promote_one(db, source)
    root = tmp_path / "arch"
    db.add(ArchiveTarget(name="local", target_type="local_path", root_path=str(root)))
    await db.commit()
    mirror = ArchiveMirror(db)
    r1 = await mirror.mirror_pending()
    await db.commit()
    assert r1["summary"]["local"]["written"] >= 4  # rendered copy, text, metadata, index
    folder = root / judgment_prefix(j)
    assert {p.name for p in folder.iterdir()} == {"rendered_copy.pdf", "judgment.txt", "metadata.json"}
    # write-once: a second run writes nothing new for the same judgment and never overwrites
    before = {p: p.stat().st_mtime_ns for p in folder.iterdir()}
    r2 = await ArchiveMirror(db).mirror_pending()
    await db.commit()
    assert {p: p.stat().st_mtime_ns for p in folder.iterdir()} == before
    adapter = LocalPathAdapter({"path": str(root)})
    with pytest.raises(ObjectExists):
        adapter.put(f"{judgment_prefix(j)}/judgment.txt", b"tampered")
    rec = await ArchiveMirror(db).reconcile()
    assert rec["local"]["missing"] == 0 and rec["local"]["ok"] >= 4
    (folder / "judgment.txt").unlink()
    rec2 = await ArchiveMirror(db).reconcile()
    assert rec2["local"]["missing"] == 1


async def test_one_target_failing_others_succeed(db, source, tmp_path):
    await _promote_one(db, source, "PLD 2024 SC 501")
    good = tmp_path / "good"
    db.add(ArchiveTarget(name="good", target_type="local_path", root_path=str(good)))
    db.add(ArchiveTarget(name="broken", target_type="sftp", root_path="/x", config_encrypted=settings.encrypt_value(json.dumps({"host": "127.0.0.1", "port": 1, "user": "u", "password": "p"}))))
    await db.commit()
    mirror = ArchiveMirror(db)
    r = await mirror.mirror_pending()
    await db.commit()
    assert r["summary"]["good"]["written"] >= 4 and r["summary"]["broken"]["failed"] >= 1
    broken = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == "broken"))).scalars().first()
    assert broken.consecutive_failures == 1 and broken.last_error
    assert (good / "_index").exists()


async def test_login_session_mirror_policy_flag_respected(db, source, tmp_path, monkeypatch):
    j = await _promote_one(db, source, "PLD 2024 SC 502", access_method="login_session")
    assert j.access_method == "login_session"
    root = tmp_path / "pol"
    t = ArchiveTarget(name="pol", target_type="local_path", root_path=str(root), mirror_login_session_rows=True)
    db.add(t)
    await db.commit()
    monkeypatch.setattr(settings, "MIRROR_LOGIN_SESSION_ROWS", False)
    r = await ArchiveMirror(db).mirror_pending()
    await db.commit()
    assert r["summary"]["pol"]["skipped_policy"] == 1 and not (root / judgment_prefix(j)).exists()
    monkeypatch.setattr(settings, "MIRROR_LOGIN_SESSION_ROWS", True)
    t.mirror_login_session_rows = False
    await db.commit()
    r2 = await ArchiveMirror(db).mirror_pending()
    assert r2["summary"]["pol"]["skipped_policy"] == 1  # target flag still false
    t.mirror_login_session_rows = True
    await db.commit()
    r3 = await ArchiveMirror(db).mirror_pending()
    await db.commit()
    assert r3["summary"]["pol"]["written"] >= 3 and (root / judgment_prefix(j) / "judgment.txt").exists()


async def test_http_fetcher_sets_browser_like_accept_headers(source):
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        assert fetcher._client is not None
        headers = fetcher._client.headers
        assert headers["User-Agent"] == settings.SCRAPER_USER_AGENT
        assert headers["Accept"] == "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
        assert headers["Accept-Language"] == "en-US,en;q=0.9"


@pytest.mark.parametrize("initial_status", ["done", "retired"])
async def test_run_public_source_refresh_reopens_finished_or_retired_seed(db, source, fixture_server, initial_status):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    listing_url = fixture_server.add("/refresh-seed", "<html><body>seed</body></html>")
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"listing:{listing_url}",
        query_json={"kind": "listing", "url": listing_url, "target_kind": "judgment", "depth": 0},
        cursor_json={},
        priority=30,
        status=initial_status,
        attempts=2,
        last_error="old error",
    )
    db.add(row)
    await db.flush()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await run_public_source(
            db,
            source,
            seed_listings=[{"url": listing_url, "target_kind": "judgment", "refresh": True}],
            fetcher=fetcher,
            limit=0,
        )

    assert row.status == "pending"
    assert row.last_error is None
    assert row.attempts == 0


class _PersistProbePipeline(PublicPipeline):
    committed_job_counters = []

    async def _persist_frontier_progress(self) -> None:  # type: ignore[override]
        await super()._persist_frontier_progress()
        if self.job_id is None:
            return
        async with SessionLocal() as verify:
            job = (await verify.execute(select(ScraperJob).where(ScraperJob.id == self.job_id))).scalars().first()
            assert job is not None
            self.__class__.committed_job_counters.append((job.pages_scraped, job.records_extracted))


async def test_run_public_source_persists_mid_run_job_counters(db, source, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    listing_url = fixture_server.add("/progress-listing", '<html><body><a href="/progress-a.html">Judgment A</a></body></html>')
    fixture_server.add("/progress-a.html", judgment_html("PLD 2026 SC 101"))

    job = ScraperJob(
        source_id=source.id,
        source_name=source.source_name,
        job_type="scrape",
        status="running",
    )
    db.add(job)
    await db.commit()

    _PersistProbePipeline.committed_job_counters = []
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await run_public_source(
            db,
            source,
            seed_listings=[{"url": listing_url, "target_kind": "judgment"}],
            fetcher=fetcher,
            limit=20,
            job_id=job.id,
            pipeline_cls=_PersistProbePipeline,
        )

    assert stats["staged"] >= 1
    assert len(_PersistProbePipeline.committed_job_counters) >= 2
    assert any(pages > 0 for pages, _ in _PersistProbePipeline.committed_job_counters)
    assert any(records > 0 for _, records in _PersistProbePipeline.committed_job_counters)
    latest_job = (await db.execute(select(ScraperJob).where(ScraperJob.id == job.id))).scalars().first()
    assert latest_job is not None
    assert latest_job.pages_scraped >= 1
    assert latest_job.records_extracted >= 1


# --------------------------------------------------------------------------- public run end-to-end
async def test_public_source_listing_to_promotion(db, source, fixture_server, monkeypatch):
    fixture_server.add("/robots.txt", "", status=404)
    fixture_server.add("/judgments/", '<html><body><a href="/judgments/a.html">Judgment A</a><a href="/judgments/b.pdf">Judgment B (pdf)</a><a href="/forbidden.html">Judgment C</a></body></html>')
    fixture_server.add("/judgments/a.html", judgment_html("PLD 2024 SC 601", title="Abdul Aziz versus Bashir Ahmad"))
    fixture_server.add("/judgments/b.pdf", text_pdf_bytes("PLD 2024 SC 602\nSUPREME COURT OF PAKISTAN\nCORAM: Umar Ata Bandial, CJ, Ijaz ul Ahsan, J and Munib Akhtar, J\nMuhammad Aslam versus Federation of Pakistan\nDecided on 2nd February 2024\nThe petitioner relied on 2015 SCMR 100. Appeal dismissed.\n" + "\n".join(["The reasons for dismissing the appeal are recorded in the paragraphs that follow this line."] * 12)), content_type="application/pdf")
    fixture_server.add("/forbidden.html", "<html><body>Access denied</body></html>", status=403)
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await run_public_source(db, source, seed_listings=[{"url": fixture_server.url("/judgments/"), "target_kind": "judgment"}], fetcher=fetcher, limit=20)
    await db.commit()
    assert stats["staged"] >= 2 and stats["halted"] is True, stats  # the 403 halted the source, after preserving what was fetched
    assert source.state == "HALTED"
    assert (await db.execute(select(func.count()).select_from(CrawlFrontier).where(CrawlFrontier.tier == 0))).scalar() >= 4
    counts = await promote_staging_records()
    assert counts["promoted"] >= 2


def test_classify_response_ignores_block_and_login_phrases_inside_a_judgment_body():
    """Criminal and banking judgments say "access denied", "suspicious activity", "verification code"
    in their own text. Those words inside a document must never HALT the source or expire the slot."""
    body = "<html><body><a href='/logout'>Logout</a><div>Citation Name: 2024 CLC 1234</div><div>Before Ali, J.</div>"
    body += "<p>" + ("The accused was found guilty of unusual activity and access denied to the premises. " * 1500) + "</p>"
    body += "<p>The verification code was sent as a one-time password to the complainant. There was suspicious activity. Please log in was the message.</p>"
    body += "<div class='modal'><input type='password' name='UpdateSubscriber.Password'></div></body></html>"
    assert len(body) > 60_000
    verdict = classify_response(200, body, "https://www.pakistanlawsite.com/Login/ReferenceCaseLawSearch?CaseName=x")
    assert verdict.kind == "ok", verdict

    # A short notice page still classifies as before.
    assert classify_response(200, "<html><body><h1>Access denied</h1></body></html>").kind == "block"
    assert classify_response(200, "<html><body>Verify you are human</body></html>").kind == "verification"
    assert classify_response(200, "<html><body><form id='mainLoginForm'><input type='password'></form></body></html>").kind == "login"
    assert classify_response(403, "").kind == "block"


def test_classify_response_authenticated_case_page_with_password_modal_is_ok():
    body = "<html><body><a href='/logout'>Logout</a><h2>Citation Name: PLD 2024 SC 1</h2><p>JUDGMENT</p>"
    body += "<div id='UpdateSubscriber'><input type='password' name='Password'></div></body></html>"
    assert classify_response(200, body, "https://www.pakistanlawsite.com/Login/Check").kind == "ok"
