from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_na_public_url, scrape_legislature
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


def test_national_assembly_default_listings_include_bills_passed_variants():
    urls = [entry["url"] for entry in DEFAULT_LISTINGS["NationalAssembly"]]
    expected = {
        "https://na.gov.pk/en/bills.php?type=1",
        "https://na.gov.pk/en/bills.php?type=2",
        "https://na.gov.pk/en/bills.php?status=pass",
        "https://na.gov.pk/en/bills.php?status=majlis",
        "https://na.gov.pk/en/bills-15.php?status=pass",
        "https://na.gov.pk/en/bills-15.php?status=majlis",
        "https://na.gov.pk/en/bills-passed.php",
        "https://na.gov.pk/en/bills-passed.php?type=1",
        "https://na.gov.pk/en/bills-passed.php?type=2",
    }
    assert expected.issubset(set(urls))


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


async def test_national_assembly_listing_fans_out_to_detail_and_harvests_document(db, fixture_server):
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
                <td>12.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/en/bill-detail.php?bill_id=9001">The Defense Forces of Pakistan Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bill-detail.php?bill_id=9001",
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
        "/uploads/documents/detail-act-2026.pdf",
        text_pdf_bytes("The Defense Forces of Pakistan Act, 2026"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/bills.php?status=pass"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/en/bill-detail.php?bill_id=9001" in fixture_server.hits
    assert "/uploads/documents/detail-act-2026.pdf" in fixture_server.hits

    detail_listing_url = f"http://127.0.0.1:{fixture_server.port}/en/bill-detail.php?bill_id=9001"
    detail_listing_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"listing:{detail_listing_url}",
            )
        )
    ).scalars().first()
    assert detail_listing_row is not None
    assert detail_listing_row.query_json["meta"]["listing_fetch"] == "legislation_table"
    assert detail_listing_row.query_json["meta"]["source_section"] == "bills"
    assert detail_listing_row.query_json["meta"]["act_year"] == "2026"

    detail_doc_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/detail-act-2026.pdf"
    detail_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{detail_doc_url}",
            )
        )
    ).scalars().first()
    assert detail_doc_row is not None
    assert detail_doc_row.query_json["expect_pdf"] is True
    assert detail_doc_row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert detail_doc_row.query_json["route"]["detail_url"].endswith("/en/bill-detail.php?bill_id=9001")
    assert detail_doc_row.query_json["route"]["act_no"] == "XLVII of 2026"
    assert detail_doc_row.query_json["route"]["act_title"].startswith("The Defense Forces of Pakistan Act")

    detail_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "NationalAssembly",
                SourceProvenance.source_url == detail_doc_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert detail_prov is not None
    assert detail_prov.route_json["detail_url"].endswith("/en/bill-detail.php?bill_id=9001")
    assert detail_prov.route_json["act_title"].startswith("The Defense Forces of Pakistan Act")


async def test_national_assembly_detail_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
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
                <td><a href="/en/bill-detail.php?bill_id=9101">The Fake Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bill-detail.php?bill_id=9101",
        """
        <html><body>
          <h1>The Fake Bill, 2026</h1>
          <a href="/uploads/documents/fake-detail-bill.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/documents/fake-detail-bill.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _national_assembly_source(db, fixture_server, ["/en/bills.php?status=pass"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
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

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/fake-detail-bill.pdf"
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


async def test_national_assembly_detail_flow_is_idempotent_on_rerun(db, fixture_server):
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
                <td>14.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/en/bill-detail.php?bill_id=9201">The Idempotence Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bill-detail.php?bill_id=9201",
        """
        <html><body>
          <h1>The Idempotence Bill, 2026</h1>
          <a href="/uploads/documents/idempotence-bill.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/idempotence-bill.pdf",
        text_pdf_bytes("The Idempotence Bill, 2026"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/bills.php?status=pass"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        first = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()
    assert first["halted"] is False

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        second = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()
    assert second["halted"] is False
    assert second["discovered"] == 0

    detail_listing_url = f"http://127.0.0.1:{fixture_server.port}/en/bill-detail.php?bill_id=9201"
    detail_listing_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"listing:{detail_listing_url}",
            )
        )
    ).scalar()
    assert detail_listing_count == 1

    detail_doc_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/idempotence-bill.pdf"
    detail_doc_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{detail_doc_url}",
            )
        )
    ).scalar()
    assert detail_doc_count == 1


async def test_national_assembly_bills_passed_listing_fans_out_detail_and_harvests_document(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills-passed.php?type=1",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr><th>Sr No.</th><th>Date</th><th>Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>44.</td>
                <td>Friday, 01st May, 2026</td>
                <td><a href="/en/bill-detail.php?bill_id=9301">The Digital Signatures Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bill-detail.php?bill_id=9301",
        """
        <html><body>
          <h1>The Digital Signatures Act, 2026</h1>
          <table>
            <tr><th>Act No</th><td>XXI of 2026</td></tr>
            <tr><th>Date of Assent</th><td>May 1, 2026</td></tr>
          </table>
          <a href="/uploads/documents/digital-signatures-act-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/digital-signatures-act-2026.pdf",
        text_pdf_bytes("The Digital Signatures Act, 2026"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/bills-passed.php?type=1"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/en/bill-detail.php?bill_id=9301" in fixture_server.hits
    assert "/uploads/documents/digital-signatures-act-2026.pdf" in fixture_server.hits

    detail_listing_url = f"http://127.0.0.1:{fixture_server.port}/en/bill-detail.php?bill_id=9301"
    detail_listing_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"listing:{detail_listing_url}",
            )
        )
    ).scalars().first()
    assert detail_listing_row is not None
    assert detail_listing_row.query_json["meta"]["source_section"] == "bills"
    assert detail_listing_row.query_json["meta"]["act_type"] == "bill"
    assert detail_listing_row.query_json["meta"]["act_year"] == "2026"

    detail_doc_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/digital-signatures-act-2026.pdf"
    detail_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{detail_doc_url}",
            )
        )
    ).scalars().first()
    assert detail_doc_row is not None
    assert detail_doc_row.query_json["expect_pdf"] is True
    assert detail_doc_row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert detail_doc_row.query_json["route"]["source_section"] == "bills"
    assert detail_doc_row.query_json["route"]["act_no"] == "XXI of 2026"
    assert detail_doc_row.query_json["route"]["act_title"].startswith("The Digital Signatures Act")

    detail_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "NationalAssembly",
                SourceProvenance.source_url == detail_doc_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert detail_prov is not None
    assert detail_prov.route_json["detail_url"].endswith("/en/bill-detail.php?bill_id=9301")
    assert detail_prov.route_json["source_section"] == "bills"


async def test_national_assembly_bills_passed_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills-passed.php?type=2",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr><th>Sr No.</th><th>Date</th><th>Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>9.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/uploads/documents/fake-passed-bill.pdf">The Fake Passed Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/documents/fake-passed-bill.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _national_assembly_source(db, fixture_server, ["/en/bills-passed.php?type=2"], target_kind="instrument")
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

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/fake-passed-bill.pdf"
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


async def test_national_assembly_bills_passed_detail_flow_is_idempotent_on_rerun(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/bills-passed.php?type=1",
        """
        <html><body>
          <table class="table_bill table-bordered table-hover">
            <thead>
              <tr><th>Sr No.</th><th>Date</th><th>Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>29.</td>
                <td>Thursday, 20th August, 2026</td>
                <td><a href="/en/bill-detail.php?bill_id=9401">The Idempotent Passed Bill, 2026</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/en/bill-detail.php?bill_id=9401",
        """
        <html><body>
          <h1>The Idempotent Passed Bill, 2026</h1>
          <a href="/uploads/documents/idempotent-passed-bill.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/documents/idempotent-passed-bill.pdf",
        text_pdf_bytes("The Idempotent Passed Bill, 2026"),
        content_type="application/pdf",
    )

    source = await _national_assembly_source(db, fixture_server, ["/en/bills-passed.php?type=1"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        first = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()
    assert first["halted"] is False

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        second = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()
    assert second["halted"] is False
    assert second["discovered"] == 0

    detail_listing_url = f"http://127.0.0.1:{fixture_server.port}/en/bill-detail.php?bill_id=9401"
    detail_listing_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"listing:{detail_listing_url}",
            )
        )
    ).scalar()
    assert detail_listing_count == 1

    detail_doc_url = f"http://127.0.0.1:{fixture_server.port}/uploads/documents/idempotent-passed-bill.pdf"
    detail_doc_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "NationalAssembly",
                CrawlFrontier.query_key == f"instrument:{detail_doc_url}",
            )
        )
    ).scalar()
    assert detail_doc_count == 1


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
