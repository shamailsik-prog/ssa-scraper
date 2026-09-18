from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_senate_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _senate_source(db, fixture_server, listings, *, target_kind: str = "statute"):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "Senate",
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


def test_normalize_senate_public_url_handles_relative_documents_path():
    rel = normalize_senate_public_url(
        "/uploads/documents/1787311922_693.pdf",
        base_url="https://www.senate.gov.pk/en/acts.php?id=-1&catid=186",
    )
    assert rel == "https://senate.gov.pk/uploads/documents/1787311922_693.pdf"


def test_normalize_senate_public_url_canonicalizes_malformed_double_question_query():
    canonical = normalize_senate_public_url(
        "https://www.senate.gov.pk/en/bills.php??id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills",
        base_url="https://www.senate.gov.pk/en/acts.php?id=-1&catid=186",
    )
    assert canonical == "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills"


def test_normalize_senate_public_url_is_idempotent_for_canonical_bills_listing():
    url = "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills"
    assert normalize_senate_public_url(url, base_url=url) == url


def test_senate_default_listings_include_canonical_bills_and_legacy_variants():
    senate_default_rows = DEFAULT_LISTINGS["Senate"]
    senate_urls = {row["url"] for row in senate_default_rows}
    assert "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=368&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/pbs.php?catid=186&subcatid=276&leftcatid=278&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/pbna.php?catid=186&subcatid=276&leftcatid=278&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/gbs.php?catid=186&subcatid=276&leftcatid=279&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/gbna.php?catid=186&subcatid=276&leftcatid=279&cattitle=Bills" in senate_urls
    assert "https://senate.gov.pk/en/bs.php?catid=186&subcatid=276&leftcatid=368&cattitle=Bills" in senate_urls
    assert all(row["target_kind"] == "instrument" for row in senate_default_rows if "/en/bills.php" in row["url"] or "/en/p" in row["url"] or "/en/g" in row["url"] or "/en/bs.php" in row["url"])


async def test_senate_bills_listing_normalizes_malformed_links_and_fans_out_to_detail_and_documents(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills",
        """
        <html><body>
          <table class="table table-bordered">
            <thead>
              <tr>
                <th>S. No.</th>
                <th>Title</th>
                <th>Act No</th>
                <th>File</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td><a href="/en/essence.php??id=9010&catid=186&cattitle=Legislation">The Senate Public Procurement Bill, 2026</a></td>
                <td>Bill No. XVIII of 2026</td>
                <td>
                  <a href="/uploads/documents/senate-bill-direct-2026.pdf">Download</a>
                  <a href="/en/bills.php??id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills">Govt Bills Senate</a>
                </td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills",
        "<html><body><p>Government bills listing mirror page.</p></body></html>",
    )
    fixture_server.add(
        "/en/essence.php?id=9010&catid=186&cattitle=Legislation",
        """
        <html><body>
          <h1>The Senate Public Procurement Bill, 2026</h1>
          <table>
            <tr><th>Act No</th><td>Bill No. XVIII of 2026</td></tr>
          </table>
          <a href="/uploads/documents/senate-bill-detail-2026.pdf">Bill PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/senate-bill-direct-2026.pdf",
        text_pdf_bytes("The Senate Public Procurement Bill, 2026"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/uploads/documents/senate-bill-detail-2026.pdf",
        text_pdf_bytes("The Senate Public Procurement Bill, 2026"),
        content_type="application/pdf",
    )

    source = await _senate_source(
        db,
        fixture_server,
        ["/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=278&cattitle=Bills"],
        target_kind="instrument",
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert "/en/essence.php?id=9010&catid=186&cattitle=Legislation" in fixture_server.hits
    assert "/uploads/documents/senate-bill-direct-2026.pdf" in fixture_server.hits
    assert "/uploads/documents/senate-bill-detail-2026.pdf" in fixture_server.hits

    fanout_url = f"http://127.0.0.1:{fixture_server.port}/en/bills.php?id=-1&catid=186&subcatid=276&leftcatid=279&cattitle=Bills"
    fanout_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "Senate",
                CrawlFrontier.query_key == f"listing:{fanout_url}",
            )
        )
    ).scalars().first()
    assert fanout_row is not None

    detail_doc_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/senate-bill-detail-2026.pdf"
    detail_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "Senate",
                CrawlFrontier.query_key == f"instrument:{detail_doc_url}",
            )
        )
    ).scalars().first()
    assert detail_doc_row is not None
    assert detail_doc_row.query_json["expect_pdf"] is True
    assert detail_doc_row.query_json["route"]["source_section"] == "bills"
    assert detail_doc_row.query_json["route"]["detail_fetch"] == "essence_documents"
    assert "??" not in detail_doc_row.query_json["route"]["detail_url"]


async def test_senate_legislation_rows_route_direct_and_detail_documents_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/acts.php?id=-1&catid=186&subcatid=285&cattitle=Acts",
        """
        <html><body>
          <table class="table table-bordered">
            <thead>
              <tr>
                <th>S. No.</th>
                <th>Title</th>
                <th>Act No</th>
                <th>Date of Assent</th>
                <th>Date of Publication in the Gazette</th>
                <th>File</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td>The National Command Authority (Amendment) Act, 2026</td>
                <td>XLVIII of 2026</td>
                <td>August 20, 2026</td>
                <td>August 20, 2026</td>
                <td><a href="/uploads/documents/direct-act-2026.pdf">Download</a></td>
              </tr>
              <tr>
                <td>2</td>
                <td><a href="/en/essence.php?id=9001&catid=186&cattitle=Legislation">The Defense Forces of Pakistan Act, 2026</a></td>
                <td>XLVII of 2026</td>
                <td>August 20, 2026</td>
                <td>August 20, 2026</td>
                <td></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/essence.php?id=9001&catid=186&cattitle=Legislation",
        """
        <html><body>
          <h1>The Defense Forces of Pakistan Act, 2026</h1>
          <table>
            <tr><th>Act No</th><td>XLVII of 2026</td></tr>
            <tr><th>Date of Assent</th><td>August 20, 2026</td></tr>
          </table>
          <a href="/uploads/documents/detail-act-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/direct-act-2026.pdf",
        text_pdf_bytes("The National Command Authority (Amendment) Act, 2026"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/uploads/documents/detail-act-2026.pdf",
        text_pdf_bytes("The Defense Forces of Pakistan Act, 2026"),
        content_type="application/pdf",
    )

    source = await _senate_source(db, fixture_server, ["/en/acts.php?id=-1&catid=186&subcatid=285&cattitle=Acts"], target_kind="statute")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 3
    assert "/uploads/documents/direct-act-2026.pdf" in fixture_server.hits
    assert "/en/essence.php?id=9001&catid=186&cattitle=Legislation" in fixture_server.hits
    assert "/uploads/documents/detail-act-2026.pdf" in fixture_server.hits

    direct_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/direct-act-2026.pdf"
    direct_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "Senate",
                CrawlFrontier.query_key == f"statute:{direct_url}",
            )
        )
    ).scalars().first()
    assert direct_row is not None
    assert direct_row.query_json["expect_pdf"] is True
    assert direct_row.query_json["route"]["listing_fetch"] == "legislation_table"
    assert direct_row.query_json["route"]["source_section"] == "acts"
    assert direct_row.query_json["route"]["act_no"] == "XLVIII of 2026"
    assert direct_row.query_json["route"]["pdf_endpoint_kind"] == "uploads-documents-file"

    detail_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/detail-act-2026.pdf"
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "Senate",
                CrawlFrontier.query_key == f"statute:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["route"]["detail_fetch"] == "essence_documents"
    assert detail_row.query_json["route"]["detail_url"].endswith("/en/essence.php?id=9001&catid=186&cattitle=Legislation")
    assert detail_row.query_json["route"]["act_no"] == "XLVII of 2026"

    detail_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "Senate",
                SourceProvenance.source_url == detail_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert detail_prov is not None
    assert detail_prov.route_json["detail_url"].endswith("/en/essence.php?id=9001&catid=186&cattitle=Legislation")
    assert detail_prov.route_json["act_title"].startswith("The Defense Forces of Pakistan Act")


async def test_senate_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/ordinance.php?id=-1&catid=186&subcatid=304&cattitle=Ordinances",
        """
        <html><body>
          <table>
            <thead>
              <tr>
                <th>S. No.</th>
                <th>Title and Ordinance No.</th>
                <th>Date of Promulgation</th>
                <th>Date of laying in the Senate</th>
                <th>File</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td>The Islamabad Capital Territory Ordinance, 2026 (Ordinance No. II of 2026)</td>
                <td>January 9, 2026</td>
                <td>April 7, 2026</td>
                <td><a href="/uploads/documents/fake-ordinance.pdf">Download</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/documents/fake-ordinance.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _senate_source(
        db,
        fixture_server,
        ["/en/ordinance.php?id=-1&catid=186&subcatid=304&cattitle=Ordinances"],
        target_kind="instrument",
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=25)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "Senate")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/fake-ordinance.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "Senate",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")
