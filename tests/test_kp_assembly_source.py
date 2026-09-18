from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_pakp_public_url, scrape_legislature
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
