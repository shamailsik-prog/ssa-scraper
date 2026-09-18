from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_gb_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _gb_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "GBAssembly",
            )
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 2
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "target_kind": "statute",
    }
    await db.commit()
    return source


def test_normalize_gb_public_url_canonicalizes_host():
    normalized = normalize_gb_public_url(
        "//www.gilgitbaltistan.gov.pk/storage/downloads/GB%20Public%20Service%20Act%202024.pdf",
        base_url="https://gilgitbaltistan.gov.pk/pages/acts",
    )
    assert normalized == "https://gilgitbaltistan.gov.pk/storage/downloads/GB%20Public%20Service%20Act%202024.pdf"


def test_gb_assembly_default_listings_include_public_acts_seed():
    urls = [entry["url"] for entry in DEFAULT_LISTINGS["GBAssembly"]]
    assert "https://gilgitbaltistan.gov.pk/pages/acts" in urls


async def test_gb_acts_listing_fans_out_direct_pdfs_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/pages/acts",
        """
        <html><body>
          <h1>Act Downloads</h1>
          <a href="/storage/downloads/gb-public-service-act-2024.pdf">GB Public Service Act 2024</a>
          <a href="/storage/downloads/gb-finance-act-2024.pdf">GB Finance Act 2024</a>
          <a href="/pages/contact">Contact</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/storage/downloads/gb-public-service-act-2024.pdf",
        text_pdf_bytes("GB Public Service Act 2024"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/storage/downloads/gb-finance-act-2024.pdf",
        text_pdf_bytes("GB Finance Act 2024"),
        content_type="application/pdf",
    )

    source = await _gb_assembly_source(db, fixture_server, ["/pages/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    primary_doc_url = f"http://127.0.0.1:{fixture_server.port}/storage/downloads/gb-public-service-act-2024.pdf"
    secondary_doc_url = f"http://127.0.0.1:{fixture_server.port}/storage/downloads/gb-finance-act-2024.pdf"

    primary_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GBAssembly",
                CrawlFrontier.query_key == f"statute:{primary_doc_url}",
            )
        )
    ).scalars().first()
    secondary_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GBAssembly",
                CrawlFrontier.query_key == f"statute:{secondary_doc_url}",
            )
        )
    ).scalars().first()
    assert primary_doc is not None
    assert secondary_doc is not None
    assert primary_doc.query_json["expect_pdf"] is True
    assert primary_doc.query_json["route"]["listing_fetch"] == "acts_document_links"
    assert primary_doc.query_json["route"]["pdf_endpoint_kind"] == "storage-download-file"
    assert primary_doc.query_json["route"]["act_title"] == "GB Public Service Act 2024"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "GBAssembly",
                SourceProvenance.source_url == secondary_doc_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["source_section"] == "acts"
    assert prov.route_json["act_title"] == "GB Finance Act 2024"


async def test_gb_acts_pdf_signature_gate_retires_non_pdf_payload(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/pages/acts",
        """
        <html><body>
          <a href="/storage/downloads/fake-gb-act-2025.pdf">Fake GB Act 2025</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/storage/downloads/fake-gb-act-2025.pdf",
        "<html>not a pdf</html>",
        content_type="application/pdf",
    )

    source = await _gb_assembly_source(db, fixture_server, ["/pages/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "GBAssembly")
        )
    ).scalar()
    assert staged == 0

    fake_doc_url = f"http://127.0.0.1:{fixture_server.port}/storage/downloads/fake-gb-act-2025.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GBAssembly",
                CrawlFrontier.query_key == f"statute:{fake_doc_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")
