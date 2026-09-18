from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_pap_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _punjab_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PunjabAssembly",
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


def test_normalize_pap_public_url_handles_relative_uploads_path():
    rel = normalize_pap_public_url(
        "/uploads/acts/301.html",
        base_url="https://www.pap.gov.pk/acts",
    )
    assert rel == "https://pap.gov.pk/uploads/acts/301.html"


async def test_punjab_assembly_structured_rows_enqueue_docs_with_route_and_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <table class="table table-striped">
            <thead>
              <tr>
                <th>Act No</th>
                <th>Act Title</th>
                <th>Passed on</th>
                <th>Assented on</th>
                <th>Year</th>
                <th>Type</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>XVI</td>
                <td><a href="/uploads/acts/301.html">The Punjab Provincial Assembly (Salaries, Allowances and Privileges of Members) Act, 1974</a></td>
                <td>17-Dec-1974</td>
                <td>21-Dec-1974</td>
                <td>1974</td>
                <td>Act</td>
              </tr>
              <tr>
                <td>XXV</td>
                <td><a href="/uploads/acts/999.pdf">The Punjab Transparency and Right to Information Act 2013</a></td>
                <td>12-Dec-2013</td>
                <td>14-Dec-2013</td>
                <td>2013</td>
                <td>Act</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/acts/301.html",
        """
        <html><body>
          <h1>THE PUNJAB PROVINCIAL ASSEMBLY (SALARIES, ALLOWANCES AND PRIVILEGES OF MEMBERS) ACT, 1974</h1>
          <p>This Act was passed by the Punjab Assembly on 17th December, 1974.</p>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/acts/999.pdf",
        text_pdf_bytes(
            "THE PUNJAB TRANSPARENCY AND RIGHT TO INFORMATION ACT 2013\n(Act XXV of 2013)\nProvincial Assembly of the Punjab"
        ),
        content_type="application/pdf",
    )

    source = await _punjab_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 2
    assert "/uploads/acts/301.html" in fixture_server.hits
    assert "/uploads/acts/999.pdf" in fixture_server.hits

    html_url = f"http://127.0.0.1:{fixture_server.port}/uploads/acts/301.html"
    html_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{html_url}",
            )
        )
    ).scalars().first()
    assert html_row is not None
    assert html_row.query_json["route"]["listing_fetch"] == "acts_table"
    assert html_row.query_json["route"]["act_no"] == "XVI"
    assert html_row.query_json["route"]["act_year"] == "1974"
    assert html_row.query_json["route"]["act_title"].startswith("The Punjab Provincial Assembly")

    pdf_url = f"http://127.0.0.1:{fixture_server.port}/uploads/acts/999.pdf"
    pdf_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{pdf_url}",
            )
        )
    ).scalars().first()
    assert pdf_row is not None
    assert pdf_row.query_json["expect_pdf"] is True
    assert pdf_row.query_json["route"]["pdf_endpoint_kind"] == "uploads-acts-file"

    html_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "PunjabAssembly",
                SourceProvenance.source_url == html_url,
            )
        )
    ).scalars().first()
    assert html_prov is not None
    assert html_prov.route_json["act_title"].startswith("The Punjab Provincial Assembly")

    pdf_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "PunjabAssembly",
                SourceProvenance.source_url == pdf_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert pdf_prov is not None
    assert pdf_prov.route_json["document_format"] == "pdf"


async def test_punjab_assembly_pdf_signature_gate_retires_non_pdf_statute_link(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <table>
            <thead>
              <tr><th>Act No</th><th>Act Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td><a href="/uploads/acts/fake.pdf">Fake Punjab Act</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/acts/fake.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _punjab_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "PunjabAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/acts/fake.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")
