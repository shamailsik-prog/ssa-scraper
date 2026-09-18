from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource
from scraper.tasks.nasirlawsite import scrape_nasirlawsite
from tests.fixtures import text_pdf_bytes


async def _nasir_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "NasirLawSite"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    configured = []
    for item in listings:
        if isinstance(item, dict) and item.get("url"):
            configured.append({**item, "url": fixture_server.url(item["url"])})
        elif isinstance(item, str):
            configured.append(fixture_server.url(item))
    source.config_json = {"listings": configured}
    await db.commit()
    return source


async def test_nasir_listing_discovery_enqueues_judgment_and_statute_frontier_rows(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/historic.htm",
        """
        <html><body>
          <a href="/historic/pld492.htm">PLD 1992 Supreme Court 492 (Benazir Bhutto Case)</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/laws.htm",
        """
        <html><body>
          <a href="/laws/ata.htm">Anti-Terrorism Act, 1997</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/historic/pld492.htm",
        """
        <html><head><title>PLD 1992 Supreme Court 492</title></head><body>
          <h1>PLD 1992 Supreme Court 492</h1>
          <a href="/historic/pld492.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/laws/ata.htm",
        """
        <html><head><title>Anti-Terrorism Act, 1997</title></head><body>
          <h1>Anti-Terrorism Act, 1997</h1>
          <p>1. Short title and commencement.</p>
        </body></html>
        """,
    )
    fixture_server.add("/historic/pld492.pdf", text_pdf_bytes("PLD 1992 Supreme Court 492"), content_type="application/pdf")

    source = await _nasir_source(
        db,
        fixture_server,
        [
            {"url": "/historic.htm", "target_kind": "judgment"},
            {"url": "/laws.htm", "target_kind": "statute"},
        ],
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_nasirlawsite(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["halted"] is False
    assert "/historic/pld492.htm" in fixture_server.hits
    assert "/laws/ata.htm" in fixture_server.hits

    historic_detail_url = f"http://127.0.0.1:{fixture_server.port}/historic/pld492.htm"
    historic_listing_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NasirLawSite",
                CrawlFrontier.query_key == f"listing:{historic_detail_url}",
            )
        )
    ).scalars().first()
    assert historic_listing_row is not None
    assert historic_listing_row.query_json["target_kind"] == "judgment"
    assert historic_listing_row.query_json["meta"]["reporter_hint"] == "PLD"
    assert "PLD 1992 Supreme Court 492" in historic_listing_row.query_json["meta"]["citation_hint"]

    law_detail_url = f"http://127.0.0.1:{fixture_server.port}/laws/ata.htm"
    law_listing_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NasirLawSite",
                CrawlFrontier.query_key == f"listing:{law_detail_url}",
            )
        )
    ).scalars().first()
    assert law_listing_row is not None
    assert law_listing_row.query_json["target_kind"] == "statute"
    assert law_listing_row.query_json["meta"]["act_year"] == "1997"
    assert "Anti-Terrorism Act" in law_listing_row.query_json["meta"]["act_title"]

    judgment_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NasirLawSite",
                CrawlFrontier.query_key == f"judgment:{historic_detail_url}",
            )
        )
    ).scalars().first()
    assert judgment_doc_row is not None
    assert judgment_doc_row.query_json["route"]["detail_fetch"] == "html_detail_page"

    statute_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NasirLawSite",
                CrawlFrontier.query_key == f"statute:{law_detail_url}",
            )
        )
    ).scalars().first()
    assert statute_doc_row is not None
    assert statute_doc_row.query_json["route"]["detail_fetch"] == "html_detail_page"


async def test_nasir_rejects_out_of_allow_list_discovery_candidates(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/historic.htm",
        """
        <html><body>
          <a href="/historic/pld139.htm">PLD 1972 Supreme Court 139</a>
          <a href="https://example.com/outside.pdf">Outside PDF</a>
          <a href="//example.com/outside-2.htm">Outside listing</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/historic/pld139.htm",
        """
        <html><head><title>PLD 1972 Supreme Court 139</title></head><body>
          <h1>PLD 1972 Supreme Court 139</h1>
        </body></html>
        """,
    )

    source = await _nasir_source(db, fixture_server, ["/historic.htm"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_nasirlawsite(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    assert stats["rejected_urls"] >= 2
    keys = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "NasirLawSite",
            )
        )
    ).scalars().all()
    assert all("example.com" not in key for key in keys)


async def test_nasir_pdf_signature_gate_retires_expect_pdf_candidates(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/laws.htm",
        """
        <html><body>
          <a href="/laws/sample-ordinance.htm">Sample Ordinance, 2026</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/laws/sample-ordinance.htm",
        """
        <html><head><title>Sample Ordinance, 2026</title></head><body>
          <h1>Sample Ordinance, 2026</h1>
          <a href="/downloads/sample-ordinance-2026.pdf">Download</a>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/sample-ordinance-2026.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _nasir_source(db, fixture_server, [{"url": "/laws.htm", "target_kind": "statute"}])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_nasirlawsite(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["halted"] is False
    pdf_url = f"http://127.0.0.1:{fixture_server.port}/downloads/sample-ordinance-2026.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NasirLawSite",
                CrawlFrontier.query_key == f"instrument:{pdf_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


async def test_nasir_rerun_is_idempotent_without_duplicate_frontier_keys(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/historic.htm",
        """
        <html><body>
          <a href="/historic/pld657.htm">PLD 1977 Supreme Court 657</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/historic/pld657.htm",
        """
        <html><head><title>PLD 1977 Supreme Court 657</title></head><body>
          <h1>PLD 1977 Supreme Court 657</h1>
        </body></html>
        """,
    )

    source = await _nasir_source(db, fixture_server, ["/historic.htm"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_nasirlawsite(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    keys_before = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "NasirLawSite",
            )
        )
    ).scalars().all()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_nasirlawsite(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    keys_after = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "NasirLawSite",
            )
        )
    ).scalars().all()

    assert len(keys_before) == len(keys_after)
    assert set(keys_before) == set(keys_after)
    total = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(CrawlFrontier.source_name == "NasirLawSite")
        )
    ).scalar()
    assert total == len(set(keys_after))
