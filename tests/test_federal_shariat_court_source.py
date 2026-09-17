from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.federal_shariat_court import _classify_discovered_url, normalize_fsc_public_url, scrape_federal_shariat_court
from tests.fixtures import text_pdf_bytes


async def _fsc_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "FederalShariatCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {"listings": [fixture_server.url(path) for path in listings]}
    await db.commit()
    return source


def test_normalize_fsc_public_url_unwraps_and_normalizes():
    wayback = "https://web.archive.org/web/20241005132304/https://www.federalshariatcourt.gov.pk/Judments/Test File.pdf"
    out = normalize_fsc_public_url(wayback, base_url="https://www.federalshariatcourt.gov.pk/en/judgments/")
    assert out == "https://www.federalshariatcourt.gov.pk/Judgments/Test%20File.pdf"

    rel = normalize_fsc_public_url("Judgements/Order 2024.pdf", base_url="https://www.federalshariatcourt.gov.pk/alljud.php")
    assert rel == "https://www.federalshariatcourt.gov.pk/Judgments/Order%202024.pdf"


async def test_fsc_discovery_from_anchor_data_and_inline_script(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/judgments/",
        """
        <html><body>
          <a href="/judnew1.html">Reported Judgements</a>
          <a href="Judgments/Anchor Appeal.pdf">Anchor file</a>
          <div data-doc-url="Judments/Data Evidence.pdf">data-path</div>
          <script>
            const doc = "Judgements/Script Order.pdf";
            const nextListing = "alljud.php?page=2";
          </script>
        </body></html>
        """,
    )
    fixture_server.add("/judnew1.html", '<html><body><a href="alljud.php">All</a></body></html>')
    fixture_server.add("/alljud.php", '<html><body><a href="Judgments/Legacy Petition.pdf">legacy</a></body></html>')
    fixture_server.add("/alljud.php?page=2", '<html><body><a href="Judgments/Page Two Case.pdf">page2</a></body></html>')

    for idx, path in enumerate(
        (
        "/Judgments/Anchor%20Appeal.pdf",
        "/Judgments/Data%20Evidence.pdf",
        "/Judgments/Script%20Order.pdf",
        "/Judgments/Legacy%20Petition.pdf",
        "/Judgments/Page%20Two%20Case.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 FSC {idx}\nFEDERAL SHARIAT COURT\nShariat Petition No. {idx} of 2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _fsc_source(db, fixture_server, ["/en/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["discovered"] >= 5
    hits = set(fixture_server.hits)
    assert "/alljud.php?page=2" in hits
    assert "/Judgments/Data%20Evidence.pdf" in hits
    assert "/Judgments/Script%20Order.pdf" in hits

    pdf_rows = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.source_name == "FederalShariatCourt", SourceProvenance.content_kind == "pdf"))
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/Judgments/Data%%20Evidence.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/Judgments/Script%%20Order.pdf" % fixture_server.port in urls


async def test_pdf_signature_gate_retires_non_pdf_judgment_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/en/judgments/", '<html><body><a href="Judgments/Bad.pdf">bad</a></body></html>')
    fixture_server.add("/Judgments/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _fsc_source(db, fixture_server, ["/en/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "FederalShariatCourt"))).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")


async def test_pdf_signature_gate_keeps_retryable_http_failures_pending(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/en/judgments/", '<html><body><a href="Judgments/Flaky.pdf">flaky</a></body></html>')
    fixture_server.add("/Judgments/Flaky.pdf", "<html><body>temporary outage</body></html>", status=503, content_type="application/pdf")

    source = await _fsc_source(db, fixture_server, ["/en/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Flaky.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "pending"
    assert row.last_error == "HTTP 503"


def test_classify_paginated_judgments_path_as_listing():
    assert _classify_discovered_url("https://www.federalshariatcourt.gov.pk/Judgments/page/2") == "listing"
