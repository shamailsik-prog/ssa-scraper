from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_kpcode_public_url, normalize_pakp_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _kp_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "KPAssembly",
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


def test_normalize_pakp_public_url_handles_relative_act_and_upload_links():
    detail_rel = normalize_pakp_public_url(
        "/act/the-khyber-pakhtunkhwa-finance-act-2026/",
        base_url="https://www.pakp.gov.pk/act/",
    )
    assert detail_rel == "https://pakp.gov.pk/act/the-khyber-pakhtunkhwa-finance-act-2026/"

    upload_abs = normalize_pakp_public_url(
        "http://www.pakp.gov.pk/wp-content/uploads/2026/08/Khyber Pakhtunkhwa Finance Act.pdf",
        base_url="https://www.pakp.gov.pk/act/the-khyber-pakhtunkhwa-finance-act-2026/",
    )
    assert upload_abs == "https://pakp.gov.pk/wp-content/uploads/2026/08/Khyber%20Pakhtunkhwa%20Finance%20Act.pdf"


def test_normalize_kpcode_public_url_handles_relative_details_and_uploads():
    detail_rel = normalize_kpcode_public_url(
        "/homepage/lawDetails/1619",
        base_url="https://kpcode.kp.gov.pk/homepage/list_all_law",
    )
    assert detail_rel == "https://kpcode.kp.gov.pk/homepage/lawDetails/1619"

    upload_abs = normalize_kpcode_public_url(
        "https://www.kpcode.kp.gov.pk/uploads/The_KP_Trade_Testing_Board_Act_2025 Watermarked.pdf",
        base_url="https://kpcode.kp.gov.pk/homepage/lawDetails/1619",
    )
    assert upload_abs == "https://kpcode.kp.gov.pk/uploads/The_KP_Trade_Testing_Board_Act_2025%20Watermarked.pdf"


async def test_kp_assembly_table_rows_route_detail_and_direct_docs_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/act/",
        """
        <html><body>
          <table>
            <thead>
              <tr><th>Sr. #</th><th>Act #</th><th>Title</th><th>Passage Date</th><th>Enforcement Date</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td><a href="/act/the-khyber-pakhtunkhwa-finance-act-2026/">Khyber Pakhtunkhwa Act No. XIII of 2026</a></td>
                <td><a href="/wp-content/uploads/2026/08/kp-finance-act-2026.pdf">The Khyber Pakhtunkhwa Finance Act, 2026</a></td>
                <td>24 Jun 2026</td>
                <td>2026-06-29</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/act/the-khyber-pakhtunkhwa-finance-act-2026/",
        """
        <html><body>
          <div class="sinpost-content">
            <h1>The Khyber Pakhtunkhwa Finance Act, 2026</h1>
            <div class="row leg-row">
              <div class="col-lg-3"><span class="act-title">Act #:</span></div>
              <div class="col-lg-9"><span class="act-info">Khyber Pakhtunkhwa Act No. XIII of 2026</span></div>
            </div>
            <div class="row leg-row">
              <div class="col-lg-3"><span class="act-title">Passage Date:</span></div>
              <div class="col-lg-9"><span class="act-info">24 Jun 2026</span></div>
            </div>
            <div class="row leg-row">
              <div class="col-lg-3"><span class="act-title">Enforcement Date:</span></div>
              <div class="col-lg-9"><span class="act-info">2026-06-29</span></div>
            </div>
            <div class="row leg-row">
              <div class="col-lg-3"><span class="act-title">Act Document:</span></div>
              <div class="col-lg-9"><span class="act-info"><a href="/wp-content/uploads/2026/08/kp-finance-act-2026.html">Download ACT HTML</a></span></div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/wp-content/uploads/2026/08/kp-finance-act-2026.pdf",
        text_pdf_bytes(
            "THE KHYBER PAKHTUNKHWA FINANCE ACT, 2026\nKhyber Pakhtunkhwa Act No. XIII of 2026\nProvincial Assembly of Khyber Pakhtunkhwa"
        ),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/wp-content/uploads/2026/08/kp-finance-act-2026.html",
        """
        <html><body>
          <h1>The Khyber Pakhtunkhwa Finance Act, 2026</h1>
          <p>Section 1. Short title and commencement.</p>
        </body></html>
        """,
    )

    source = await _kp_assembly_source(db, fixture_server, ["/act/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 3
    assert "/act/the-khyber-pakhtunkhwa-finance-act-2026/" in fixture_server.hits
    assert "/wp-content/uploads/2026/08/kp-finance-act-2026.pdf" in fixture_server.hits
    assert "/wp-content/uploads/2026/08/kp-finance-act-2026.html" in fixture_server.hits

    pdf_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2026/08/kp-finance-act-2026.pdf"
    pdf_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"statute:{pdf_url}",
            )
        )
    ).scalars().first()
    assert pdf_row is not None
    assert pdf_row.query_json["expect_pdf"] is True
    assert pdf_row.query_json["route"]["listing_fetch"] == "acts_table"
    assert pdf_row.query_json["route"]["act_no"] == "Khyber Pakhtunkhwa Act No. XIII of 2026"
    assert pdf_row.query_json["route"]["act_year"] == "2026"
    assert pdf_row.query_json["route"]["pdf_endpoint_kind"] == "wp-content-uploads-file"

    html_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2026/08/kp-finance-act-2026.html"
    html_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"statute:{html_url}",
            )
        )
    ).scalars().first()
    assert html_row is not None
    assert html_row.query_json["route"]["detail_fetch"] == "act_document_row"
    assert html_row.query_json["route"]["detail_url"].endswith("/act/the-khyber-pakhtunkhwa-finance-act-2026/")

    pdf_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "KPAssembly",
                SourceProvenance.source_url == pdf_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert pdf_prov is not None
    assert pdf_prov.route_json["act_title"] == "The Khyber Pakhtunkhwa Finance Act, 2026"
    assert pdf_prov.route_json["document_format"] == "pdf"


async def test_kp_assembly_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/act/",
        """
        <html><body>
          <table>
            <thead>
              <tr><th>Sr. #</th><th>Act #</th><th>Title</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>1</td>
                <td><a href="/act/fake-act/">Khyber Pakhtunkhwa Act No. I of 2026</a></td>
                <td><a href="/wp-content/uploads/2026/08/fake-kp-act.pdf">Fake KP Act PDF</a></td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/act/fake-act/",
        """
        <html><body><div class="sinpost-content"><h1>Fake KP Act</h1></div></body></html>
        """,
    )
    fixture_server.add("/wp-content/uploads/2026/08/fake-kp-act.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _kp_assembly_source(db, fixture_server, ["/act/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "KPAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2026/08/fake-kp-act.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")


async def test_kpcode_listing_and_rule_detail_route_statute_and_instrument_docs(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/homepage/list_all_law",
        """
        <html><body>
          <div class="artlist"><a href="/homepage/lawDetails/1619">THE KHYBER PAKHTUNKHWA TRADE TESTING BOARD ACT, 2025.</a></div>
          <div class="artdets">Industries Department | Act No. XV of 2025 | Promulgation Date: 19-05-2025 | Year: 2025</div>
          <ul class="pagination">
            <li><a href="/homepage/list_all_law/0/15/15">2</a></li>
          </ul>
        </body></html>
        """,
    )
    fixture_server.add(
        "/homepage/list_all_law/0/15/15",
        """
        <html><body>
          <div class="artlist"><a href="/homepage/RuleDetails/1311">Punjab Stamp Rules, 1934</a></div>
          <div class="artdets">Revenue Department | Rule No. 4 of 1934 | Promulgation Date: 14-02-1934 | Year: 1934</div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/homepage/lawDetails/1619",
        """
        <html><body>
          <h2>Khyber Pakhtunkhwa Code</h2>
          <h2>THE KHYBER PAKHTUNKHWA TRADE TESTING BOARD ACT, 2025.</h2>
          <table>
            <tr><th>Department:</th><td>Industries Department</td></tr>
            <tr><th>Main Category:</th><td>Acts</td></tr>
            <tr><th>Specific Category Name:</th><td>To reconstitute trade testing board.</td></tr>
            <tr><th>Year</th><td>2025</td></tr>
            <tr><th>Promulgation Date:</th><td>19-05-2025</td></tr>
          </table>
          <a href="/uploads/kp-trade-testing-board-act-2025.pdf">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/homepage/RuleDetails/1311",
        """
        <html><body>
          <h2>Khyber Pakhtunkhwa Code</h2>
          <h2>Punjab Stamp Rules, 1934</h2>
          <table>
            <tr><th>Department:</th><td>Revenue and Estate Department</td></tr>
            <tr><th>Main Category:</th><td>Rules</td></tr>
            <tr><th>Year</th><td>1934</td></tr>
            <tr><th>Promulgation Date:</th><td>14-02-1934</td></tr>
          </table>
          <a href="/uploads/punjab-stamp-rules-1934.pdf">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/kp-trade-testing-board-act-2025.pdf",
        text_pdf_bytes("THE KHYBER PAKHTUNKHWA TRADE TESTING BOARD ACT, 2025"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/uploads/punjab-stamp-rules-1934.pdf",
        text_pdf_bytes("PUNJAB STAMP RULES, 1934"),
        content_type="application/pdf",
    )

    source = await _kp_assembly_source(db, fixture_server, ["/homepage/list_all_law"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert "/homepage/lawDetails/1619" in fixture_server.hits
    assert "/homepage/RuleDetails/1311" in fixture_server.hits
    assert "/uploads/kp-trade-testing-board-act-2025.pdf" in fixture_server.hits
    assert "/uploads/punjab-stamp-rules-1934.pdf" in fixture_server.hits

    statute_url = f"http://127.0.0.1:{fixture_server.port}/uploads/kp-trade-testing-board-act-2025.pdf"
    statute_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"statute:{statute_url}",
            )
        )
    ).scalars().first()
    assert statute_row is not None
    assert statute_row.query_json["route"]["detail_fetch"] == "kpcode_detail_download"
    assert statute_row.query_json["route"]["detail_url"].endswith("/homepage/lawDetails/1619")
    assert statute_row.query_json["route"]["detail_category"] == "Acts"

    instrument_url = f"http://127.0.0.1:{fixture_server.port}/uploads/punjab-stamp-rules-1934.pdf"
    instrument_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"instrument:{instrument_url}",
            )
        )
    ).scalars().first()
    assert instrument_row is not None
    assert instrument_row.query_json["expect_pdf"] is True
    assert instrument_row.query_json["route"]["detail_category"] == "Rules"
    assert instrument_row.query_json["route"]["source_section"] == "rules"

    instrument_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "KPAssembly",
                SourceProvenance.source_url == instrument_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert instrument_prov is not None
    assert instrument_prov.route_json["detail_url"].endswith("/homepage/RuleDetails/1311")


async def test_kpcode_pdf_signature_gate_retires_non_pdf_rule_download(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/homepage/list_all_law",
        """
        <html><body>
          <div class="artlist"><a href="/homepage/RuleDetails/1400">Fake Rules Listing</a></div>
          <div class="artdets">Revenue Department | Rule No. 1 of 2026 | Year: 2026</div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/homepage/RuleDetails/1400",
        """
        <html><body>
          <h2>Fake Rules, 2026</h2>
          <table>
            <tr><th>Main Category:</th><td>Rules</td></tr>
            <tr><th>Year</th><td>2026</td></tr>
          </table>
          <a href="/uploads/fake-rules-2026.pdf">Download</a>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/fake-rules-2026.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _kp_assembly_source(db, fixture_server, ["/homepage/list_all_law"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "KPAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/fake-rules-2026.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "KPAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")
