from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_pas_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _sindh_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "SindhAssembly",
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


def test_normalize_pas_public_url_normalizes_details_and_uploads_paths():
    detail_rel = normalize_pas_public_url(
        "/index.php/acts/details/33/576",
        base_url="https://www.pas.gov.pk/index.php/acts",
    )
    assert detail_rel == "https://pas.gov.pk/index.php/acts/details/33/576"

    upload_abs = normalize_pas_public_url(
        "http://www.pas.gov.pk/uploads/acts/Sindh Act No.I of 2024.pdf",
        base_url="https://www.pas.gov.pk/index.php/acts/details/33/576",
    )
    assert upload_abs == "https://pas.gov.pk/uploads/acts/Sindh%20Act%20No.I%20of%202024.pdf"


async def test_sindh_assembly_listing_rows_route_detail_docs_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/index.php/acts",
        """
        <html><body>
          <table class="table table-bordered table-striped">
            <thead>
              <tr>
                <th>Act No.</th>
                <th>Title</th>
                <th>Date of Passing</th>
                <th>Date of Governor's Assent</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <td>Sindh Act No.I of 2024</td>
                <td><a href="/index.php/acts/details/33/576" title="The Registration (Sindh Amendment) Act, 2024">The Registration (Sindh Amendment) Act, 2024</a></td>
                <td>2024-05-24</td>
                <td>2024-06-21</td>
              </tr>
              <tr>
                <td>Sindh Act No.II of 2024</td>
                <td><a href="/index.php/acts/details/33/577" title="The Sindh Finance Act, 2024">The Sindh Finance Act, 2024</a></td>
                <td>2024-06-28</td>
                <td>2024-06-30</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/index.php/acts/details/33/576",
        """
        <html><body>
          <h2 class="act-title">The Registration (Sindh Amendment) Act, 2024</h2>
          <p><label>Act No:</label> Sindh Act No.I of 2024</p>
          <p><label>Passed On:</label> 24th May 2024</p>
          <p><label>Date of Enforcement:</label> 21st June 2024</p>
          <h3>Act Files</h3>
          <a href="/uploads/acts/sindh-act-no-i-of-2024.pdf">The Registration (Sindh Amendment) Act, 2024</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/index.php/acts/details/33/577",
        """
        <html><body>
          <h2 class="act-title">The Sindh Finance Act, 2024</h2>
          <p><label>Act No:</label> Sindh Act No.II of 2024</p>
          <p><label>Passed On:</label> 28th June 2024</p>
          <p><label>Date of Enforcement:</label> 30th June 2024</p>
          <h3>Act Files</h3>
          <a href="/uploads/acts/sindh-finance-act-2024.html">The Sindh Finance Act, 2024 (HTML)</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/uploads/acts/sindh-act-no-i-of-2024.pdf",
        text_pdf_bytes(
            "THE REGISTRATION (SINDH AMENDMENT) ACT, 2024\nSindh Act No.I of 2024\nProvincial Assembly of Sindh"
        ),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/uploads/acts/sindh-finance-act-2024.html",
        """
        <html><body>
          <h1>The Sindh Finance Act, 2024</h1>
          <p>Section 1. Short title and commencement.</p>
        </body></html>
        """,
    )

    source = await _sindh_assembly_source(db, fixture_server, ["/index.php/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 4
    assert "/index.php/acts/details/33/576" in fixture_server.hits
    assert "/uploads/acts/sindh-act-no-i-of-2024.pdf" in fixture_server.hits
    assert "/uploads/acts/sindh-finance-act-2024.html" in fixture_server.hits

    pdf_url = f"http://127.0.0.1:{fixture_server.port}/uploads/acts/sindh-act-no-i-of-2024.pdf"
    pdf_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhAssembly",
                CrawlFrontier.query_key == f"statute:{pdf_url}",
            )
        )
    ).scalars().first()
    assert pdf_row is not None
    assert pdf_row.query_json["expect_pdf"] is True
    assert pdf_row.query_json["route"]["listing_fetch"] == "acts_table"
    assert pdf_row.query_json["route"]["detail_fetch"] == "act_files_section"
    assert pdf_row.query_json["route"]["act_no"] == "Sindh Act No.I of 2024"
    assert pdf_row.query_json["route"]["act_year"] == "2024"
    assert pdf_row.query_json["route"]["pdf_endpoint_kind"] == "uploads-acts-file"
    assert pdf_row.query_json["route"]["detail_url"].endswith("/index.php/acts/details/33/576")

    pdf_prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "SindhAssembly",
                SourceProvenance.source_url == pdf_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert pdf_prov is not None
    assert pdf_prov.route_json["act_title"].startswith("The Registration (Sindh Amendment) Act")
    assert pdf_prov.route_json["detail_url"].endswith("/index.php/acts/details/33/576")
    assert pdf_prov.route_json["document_format"] == "pdf"


async def test_sindh_assembly_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/index.php/acts",
        """
        <html><body>
          <table>
            <thead>
              <tr><th>Act No.</th><th>Title</th><th>Date of Passing</th><th>Date of Governor's Assent</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>Sindh Act No.III of 2024</td>
                <td><a href="/index.php/acts/details/33/578">Fake Sindh Act</a></td>
                <td>2024-07-01</td>
                <td>2024-07-02</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/index.php/acts/details/33/578",
        """
        <html><body>
          <h2 class="act-title">Fake Sindh Act</h2>
          <h3>Act Files</h3>
          <a href="/uploads/acts/fake-sindh-act.pdf">Fake Sindh Act PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/uploads/acts/fake-sindh-act.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _sindh_assembly_source(db, fixture_server, ["/index.php/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "SindhAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/uploads/acts/fake-sindh-act.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")
