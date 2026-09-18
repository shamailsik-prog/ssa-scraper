from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import listings_for, normalize_pas_public_url, scrape_legislature
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

    sindhlaws_detail = normalize_pas_public_url(
        "http://www.sindhlaws.gov.pk/GazetteDetail.aspx?X=ACT&Year=2026",
        base_url="https://sindhlaws.gov.pk/",
    )
    assert sindhlaws_detail == "https://sindhlaws.gov.pk/GazetteDetail.aspx?X=ACT&Year=2026"


async def test_sindh_default_listings_include_pas_and_sindhlaws_seed_urls(db):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "SindhAssembly",
            )
        )
    ).scalars().first()
    source.config_json = {}
    await db.commit()

    listing_urls = {row["url"] for row in listings_for(source)}
    assert "https://www.pas.gov.pk/index.php/acts" in listing_urls
    assert "https://sindhlaws.gov.pk/" in listing_urls


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


async def test_sindhlaws_listing_fans_out_to_detail_and_documents_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/",
        """
        <html><body>
          <a href="/Gazette.aspx?pg=ACT">Gazette Legislation</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Gazette.aspx?pg=ACT",
        """
        <html><body>
          <a href="/GazetteDetail.aspx?X=ACT&Year=2026">2026</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/GazetteDetail.aspx?X=ACT&Year=2026",
        """
        <html><body>
          <h1>Gazette Legislation (Acts)</h1>
          <table>
            <thead>
              <tr><th>#</th><th>TITLE</th><th>ENGLISH</th><th>DATE</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>I</td>
                <td>CONSTITUTIONAL BENCHES OF HIGH COURT OF SINDH (PRACTICE AND PROCEDURE) ACT, 2026</td>
                <td><a href="/setup/publications/PUB-26-000006.pdf">Download</a></td>
                <td>Jan 26, 2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/setup/publications/PUB-26-000006.pdf",
        text_pdf_bytes(
            "CONSTITUTIONAL BENCHES OF HIGH COURT OF SINDH (PRACTICE AND PROCEDURE) ACT, 2026"
        ),
        content_type="application/pdf",
    )

    source = await _sindh_assembly_source(db, fixture_server, ["/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 3
    assert "/Gazette.aspx?pg=ACT" in fixture_server.hits
    assert "/GazetteDetail.aspx?X=ACT&Year=2026" in fixture_server.hits
    assert "/setup/publications/PUB-26-000006.pdf" in fixture_server.hits

    pdf_url = f"http://127.0.0.1:{fixture_server.port}/setup/publications/PUB-26-000006.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhAssembly",
                CrawlFrontier.query_key == f"statute:{pdf_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["expect_pdf"] is True
    assert row.query_json["route"]["listing_fetch"] == "gazette_year_grid"
    assert row.query_json["route"]["detail_fetch"] == "gazette_detail_table"
    assert row.query_json["route"]["source_section"] == "acts"
    assert row.query_json["route"]["act_year"] == "2026"
    assert row.query_json["route"]["act_no"] == "I"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "setup-publications-file"
    assert row.query_json["route"]["detail_url"].endswith("/GazetteDetail.aspx?X=ACT&Year=2026")

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "SindhAssembly",
                SourceProvenance.source_url == pdf_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["detail_fetch"] == "gazette_detail_table"
    assert "CONSTITUTIONAL BENCHES OF HIGH COURT OF SINDH" in prov.route_json["act_title"]


async def test_sindhlaws_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/",
        """
        <html><body><a href="/Gazette.aspx?pg=ACT">Gazette Legislation</a></body></html>
        """,
    )
    fixture_server.add(
        "/Gazette.aspx?pg=ACT",
        """
        <html><body><a href="/GazetteDetail.aspx?X=ACT&Year=2026">2026</a></body></html>
        """,
    )
    fixture_server.add(
        "/GazetteDetail.aspx?X=ACT&Year=2026",
        """
        <html><body>
          <table><tbody><tr><td>I</td><td>FAKE ACT, 2026</td><td><a href="/setup/publications/fake-sindhlaws-act.pdf">Download</a></td><td>Jan 26, 2026</td></tr></tbody></table>
        </body></html>
        """,
    )
    fixture_server.add("/setup/publications/fake-sindhlaws-act.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _sindh_assembly_source(db, fixture_server, ["/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
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

    document_url = f"http://127.0.0.1:{fixture_server.port}/setup/publications/fake-sindhlaws-act.pdf"
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


async def test_sindhlaws_rerun_is_idempotent_without_duplicate_frontier_keys(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/",
        """
        <html><body><a href="/Gazette.aspx?pg=ACT">Gazette Legislation</a></body></html>
        """,
    )
    fixture_server.add(
        "/Gazette.aspx?pg=ACT",
        """
        <html><body><a href="/GazetteDetail.aspx?X=ACT&Year=2024">2024</a></body></html>
        """,
    )
    fixture_server.add(
        "/GazetteDetail.aspx?X=ACT&Year=2024",
        """
        <html><body>
          <table><tbody><tr><td>I</td><td>SINDH IDP ACT, 2024</td><td><a href="/setup/publications/PUB-24-000001.pdf">Download</a></td><td>May 24, 2024</td></tr></tbody></table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/setup/publications/PUB-24-000001.pdf",
        text_pdf_bytes("SINDH IDP ACT, 2024"),
        content_type="application/pdf",
    )

    source = await _sindh_assembly_source(db, fixture_server, ["/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    keys_before = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "SindhAssembly",
            )
        )
    ).scalars().all()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    keys_after = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "SindhAssembly",
            )
        )
    ).scalars().all()

    assert len(keys_before) == len(keys_after)
    assert set(keys_before) == set(keys_after)
    total = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(CrawlFrontier.source_name == "SindhAssembly")
        )
    ).scalar()
    assert total == len(set(keys_after))
