from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_pcp_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _gazette_source(db, fixture_server, listings, *, target_kind: str = "instrument"):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "GazetteOfPakistan",
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


def test_normalize_pcp_public_url_handles_relative_download_path():
    rel = normalize_pcp_public_url(
        "/SiteImage/Downloads/10172 (22) Part-I.pdf",
        base_url="http://www.pcp.gov.pk/Download",
    )
    assert rel == "http://pcp.gov.pk/SiteImage/Downloads/10172%20(22)%20Part-I.pdf"


async def test_gazette_download_rows_route_direct_documents_with_row_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Download",
        """
        <html><body>
          <nav>
            <a href="/WeeklyNitifications">Weekly Notification</a>
          </nav>
          <table id="myTable">
            <thead>
              <tr>
                <th>Job ID</th>
                <th>Department</th>
                <th>Title</th>
                <th>Date</th>
                <th>Download</th>
                <th>Active</th>
                <th>Parts</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>14124(22)Ex Gaz-I</td>
                <td>National Assembly</td>
                <td>Code of Criminal Procedure (Amendment) Act, 2022.</td>
                <td>December 31, 2022</td>
                <td><a href="/SiteImage/Downloads/14124(22)Ex Gaz-I.pdf">Download</a></td>
                <td>True</td>
                <td>1</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/WeeklyNitifications",
        """
        <html><body>
          <table id="myTable">
            <thead>
              <tr><th>Date</th><th>Weekly Issue No</th><th>Download</th><th>Active</th></tr>
            </thead>
            <tbody></tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/SiteImage/Downloads/14124(22)Ex%20Gaz-I.pdf",
        text_pdf_bytes("Code of Criminal Procedure (Amendment) Act, 2022."),
        content_type="application/pdf",
    )
    source = await _gazette_source(db, fixture_server, ["/Download"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 2
    assert "/WeeklyNitifications" in fixture_server.hits
    assert "/SiteImage/Downloads/14124(22)Ex%20Gaz-I.pdf" in fixture_server.hits

    document_url = f"http://127.0.0.1:{fixture_server.port}/SiteImage/Downloads/14124(22)Ex%20Gaz-I.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GazetteOfPakistan",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["expect_pdf"] is True
    assert row.query_json["route"]["listing_fetch"] == "downloads_table"
    assert row.query_json["route"]["source_section"] == "download_notifications"
    assert row.query_json["route"]["gazette_job_id"] == "14124(22)Ex Gaz-I"
    assert row.query_json["route"]["gazette_department"] == "National Assembly"
    assert row.query_json["route"]["gazette_part"] == "1"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "siteimage-downloads-file"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "GazetteOfPakistan",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["gazette_job_id"] == "14124(22)Ex Gaz-I"
    assert prov.route_json["gazette_date"] == "December 31, 2022"


async def test_gazette_detail_page_routes_row_scoped_document(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Download",
        """
        <html><body>
          <table id="myTable">
            <thead>
              <tr><th>Job ID</th><th>Department</th><th>Title</th><th>Date</th><th>Download</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>20001(26)Ex Gaz-II</td>
                <td>Ministry of Finance</td>
                <td><a href="/Detail/finance-rules-20001">Finance Rules Notification, 2026</a></td>
                <td>August 20, 2026</td>
                <td></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Detail/finance-rules-20001",
        """
        <html><body>
          <h1>Finance Rules Notification, 2026</h1>
          <table>
            <tr><th>Job ID</th><td>20001(26)Ex Gaz-II</td></tr>
            <tr><th>Department</th><td>Ministry of Finance</td></tr>
            <tr><th>Date</th><td>August 20, 2026</td></tr>
          </table>
          <a href="/SiteImage/Downloads/20001(26)-Ex-Gaz-II.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/SiteImage/Downloads/20001(26)-Ex-Gaz-II.pdf",
        text_pdf_bytes("Finance Rules Notification, 2026"),
        content_type="application/pdf",
    )

    source = await _gazette_source(db, fixture_server, ["/Download"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert "/Detail/finance-rules-20001" in fixture_server.hits
    assert "/SiteImage/Downloads/20001(26)-Ex-Gaz-II.pdf" in fixture_server.hits

    doc_url = f"http://127.0.0.1:{fixture_server.port}/SiteImage/Downloads/20001(26)-Ex-Gaz-II.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GazetteOfPakistan",
                CrawlFrontier.query_key == f"instrument:{doc_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert row.query_json["route"]["detail_url"].endswith("/Detail/finance-rules-20001")
    assert row.query_json["route"]["gazette_job_id"] == "20001(26)Ex Gaz-II"


async def test_gazette_weekly_rows_route_issue_documents_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/WeeklyNitifications",
        """
        <html><body>
          <table id="myTable">
            <thead>
              <tr><th>Date</th><th>Weekly Issue No</th><th>Download</th><th>Active</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>October 19, 2022</td>
                <td>10172</td>
                <td><a href="/SiteImage/Downloads/10172 (22) Part-I.pdf">Download</a></td>
                <td>True</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/SiteImage/Downloads/10172%20(22)%20Part-I.pdf",
        text_pdf_bytes("THE GAZETTE OF PAKISTAN, OCTOBER 19, 2022 PART-I"),
        content_type="application/pdf",
    )

    source = await _gazette_source(db, fixture_server, ["/WeeklyNitifications"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    document_url = f"http://127.0.0.1:{fixture_server.port}/SiteImage/Downloads/10172%20(22)%20Part-I.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GazetteOfPakistan",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["expect_pdf"] is True
    assert row.query_json["route"]["listing_fetch"] == "weekly_table"
    assert row.query_json["route"]["source_section"] == "weekly_notifications"
    assert row.query_json["route"]["gazette_issue_no"] == "10172"
    assert row.query_json["route"]["gazette_part"] == "I"
    assert row.query_json["route"]["gazette_date"] == "October 19, 2022"


async def test_gazette_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Download",
        """
        <html><body>
          <table id="myTable">
            <thead>
              <tr><th>Job ID</th><th>Department</th><th>Title</th><th>Date</th><th>Download</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>10000(22)Ex Gaz-III</td>
                <td>Ministry of Interior</td>
                <td>Anti-Terrorism Act, 1997.</td>
                <td>December 16, 2022</td>
                <td><a href="/SiteImage/Downloads/10000(22)Ex-Gaz-III.pdf">Download</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/SiteImage/Downloads/10000(22)Ex-Gaz-III.pdf",
        "<html>not-a-pdf</html>",
        content_type="application/pdf",
    )

    source = await _gazette_source(db, fixture_server, ["/Download"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=25)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "GazetteOfPakistan")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/SiteImage/Downloads/10000(22)Ex-Gaz-III.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "GazetteOfPakistan",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")
