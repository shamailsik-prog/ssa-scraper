from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_na_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _national_assembly_source(db, fixture_server, listings, *, target_kind: str):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "NationalAssembly",
            )
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 2
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "target_kind": target_kind,
    }
    await db.commit()
    return source


def test_normalize_na_public_url_handles_relative_documents_path():
    rel = normalize_na_public_url(
        "/uploads/documents/6a883f3193ef6_450.pdf",
        base_url="https://www.na.gov.pk/en/acts-tenure.php",
    )
    assert rel == "https://na.gov.pk/uploads/documents/6a883f3193ef6_450.pdf"


async def test_national_assembly_acts_rows_enqueue_pdf_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/acts-tenure.php",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr>
                <th>Sr No.</th>
                <th>Date</th>
                <th>Act Title</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>125.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/uploads/documents/act-2026.pdf">The National Command Authority (Amendment) Act, 2026 (Act No. XLVIII of 2026)</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/act-2026.pdf",
        text_pdf_bytes("The National Command Authority (Amendment) Act, 2026"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/acts-tenure.php"], target_kind="statute")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 1
    assert "/uploads/documents/act-2026.pdf" in fixture_server.hits

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/act-2026.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["expect_pdf"] is True
    assert row.query_json["route"]["listing_fetch"] == "legislation_table"
    assert row.query_json["route"]["source_section"] == "acts"
    assert row.query_json["route"]["act_no"] == "XLVIII of 2026"
    assert row.query_json["route"]["act_year"] == "2026"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "uploads-documents-file"
    assert row.query_json["route"]["act_type"] == "act"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "NationalAssembly",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["act_title"].startswith("The National Command Authority")


async def test_national_assembly_ordinance_listing_routes_instrument_pdf(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills.php?type=4",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr>
                <th>Sr No.</th>
                <th>Date</th>
                <th>Title</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>7.</td>
                <td>Monday, 21st July, 2025</td>
                <td><a href="/uploads/documents/ordinance-2025.pdf">The Tax Laws (Amendment) Ordinance, 2025</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/ordinance-2025.pdf",
        text_pdf_bytes("The Tax Laws (Amendment) Ordinance, 2025"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/bills.php?type=4"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/ordinance-2025.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["expect_pdf"] is True
    assert row.query_json["route"]["source_section"] == "ordinances"
    assert row.query_json["route"]["act_type"] == "ordinance"
    assert row.query_json["route"]["act_year"] == "2025"


async def test_national_assembly_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills.php?status=pass",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr><th>Sr No.</th><th>Date</th><th>Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>1.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/uploads/documents/fake-bill.pdf">The Fake Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/documents/fake-bill.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _national_assembly_source(db, fixture_server, ["/en/bills.php?status=pass"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=25)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "NationalAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/fake-bill.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")
