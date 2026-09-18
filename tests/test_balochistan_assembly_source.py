from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_pab_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _balochistan_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "BalochistanAssembly",
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


def test_normalize_pab_public_url_handles_relative_storage_path():
    rel = normalize_pab_public_url(
        "/storage/9339/act-2026.pdf",
        base_url="https://www.pabalochistan.gov.pk/acts",
    )
    assert rel == "https://pabalochistan.gov.pk/storage/9339/act-2026.pdf"


def test_normalize_pab_public_url_canonicalizes_balochistan_code_host():
    rel = normalize_pab_public_url(
        "//www.balochistancode.gob.pk/Document.aspx?docc=1248&docid=1303&wise=download",
        base_url="https://balochistancode.gob.pk/laws_rules.aspx?opento=1&wise=srbdl",
    )
    assert rel == "https://balochistancode.gob.pk/Document.aspx?docc=1248&docid=1303&wise=download"


def test_balochistan_assembly_default_listings_include_laws_portal_seed():
    urls = [entry["url"] for entry in DEFAULT_LISTINGS["BalochistanAssembly"]]
    expected = {
        "https://www.pabalochistan.gov.pk/acts",
        "https://balochistancode.gob.pk/laws_rules.aspx?opento=1&wise=srbdl",
    }
    assert expected.issubset(set(urls))


async def test_balochistan_assembly_structured_rows_enqueue_pdf_with_provenance_route_meta(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <div id="tenureAccordion">
            <div class="accordion-item">
              <h2 class="accordion-header"><button>2024-2029</button></h2>
              <div class="accordion-body">
                <div class="accordion-item">
                  <h2 class="accordion-header"><button>2026</button></h2>
                  <div class="accordion-body">
                    <table class="table table-bordered table-striped">
                      <thead><tr><th>Act No</th><th>Act Title</th><th>Passed on</th><th>Assented on</th><th>Type</th></tr></thead>
                      <tbody>
                        <tr>
                          <td>15</td>
                          <td><a href="/storage/9339/the-balochistan-prisons-act-2026.pdf">THE BALOCHISTAN PRISONS ACT, 2026, ACT NO. XV OF 2026.</a></td>
                          <td>14/05/2026</td>
                          <td>19/05/2026</td>
                          <td>Government</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/storage/9339/the-balochistan-prisons-act-2026.pdf",
        text_pdf_bytes(
            "THE BALOCHISTAN PRISONS ACT, 2026\nACT NO. XV OF 2026\nProvincial Assembly of Balochistan"
        ),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 1
    assert "/storage/9339/the-balochistan-prisons-act-2026.pdf" in fixture_server.hits

    document_url = f"http://127.0.0.1:{fixture_server.port}/storage/9339/the-balochistan-prisons-act-2026.pdf"
    frontier = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert frontier is not None
    assert frontier.query_json["expect_pdf"] is True
    assert frontier.query_json["route"]["listing_fetch"] == "acts_table"
    assert frontier.query_json["route"]["act_year"] == "2026"
    assert frontier.query_json["route"]["act_no"] == "15"
    assert frontier.query_json["route"]["pdf_endpoint_kind"] == "storage-file"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "BalochistanAssembly",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["act_title"].startswith("THE BALOCHISTAN PRISONS ACT")
    assert prov.route_json["document_format"] == "pdf"
    assert prov.route_json["tenure"] == "2024-2029"


async def test_balochistan_assembly_pdf_signature_gate_retires_non_pdf_statute_link(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <div id="tenureAccordion">
            <div class="accordion-item">
              <div class="accordion-body">
                <div class="accordion-item">
                  <div class="accordion-body">
                    <table class="table table-bordered table-striped">
                      <thead><tr><th>Act No</th><th>Act Title</th></tr></thead>
                      <tbody>
                        <tr>
                          <td>1</td>
                          <td><a href="/storage/1000/fake-act.pdf">Fake Act</a></td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add("/storage/1000/fake-act.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _balochistan_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "BalochistanAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/storage/1000/fake-act.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")


async def test_balochistan_laws_portal_listing_fans_out_detail_then_document_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/laws_rules.aspx?opento=1&wise=srbdl",
        """
        <html><body>
          <table>
            <thead>
              <tr><th>Law No.</th><th>Title</th><th>Year</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>XV of 2010</td>
                <td><a href="/Document.aspx?docc=1248&docid=1303&wise=opendoc">Balochistan Local Government Act 2010</a></td>
                <td>2010</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=1248&docid=1303&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Local Government Act 2010, 2010</h2>
          <table>
            <tr><th>Act No</th><td>XV of 2010</td></tr>
            <tr><th>Promulgation Date</th><td>2010-06-15</td></tr>
          </table>
          <a href="/Document.aspx?docc=1248&docid=1303&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=1248&docid=1303&wise=download",
        text_pdf_bytes("Balochistan Local Government Act 2010"),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/laws_rules.aspx?opento=1&wise=srbdl"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/Document.aspx?docc=1248&docid=1303&wise=opendoc" in fixture_server.hits
    assert "/Document.aspx?docc=1248&docid=1303&wise=download" in fixture_server.hits

    detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=1248&docid=1303&wise=opendoc"
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["meta"]["listing_fetch"] == "balochistancode_table"
    assert detail_row.query_json["meta"]["source_section"] == "laws_portal"
    assert detail_row.query_json["meta"]["act_no"] == "XV of 2010"
    assert detail_row.query_json["meta"]["act_year"] == "2010"

    document_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=1248&docid=1303&wise=download"
    doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert doc_row is not None
    assert doc_row.query_json["expect_pdf"] is True
    assert doc_row.query_json["route"]["detail_fetch"] == "balochistancode_detail_download"
    assert doc_row.query_json["route"]["detail_url"] == detail_url
    assert doc_row.query_json["route"]["pdf_endpoint_kind"] == "balochistancode-download-file"
    assert doc_row.query_json["route"]["source_section"] == "laws_portal"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "BalochistanAssembly",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["detail_url"] == detail_url
    assert prov.route_json["act_no"] == "XV of 2010"


async def test_balochistan_laws_portal_pdf_signature_gate_retires_non_pdf_download(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/laws_rules.aspx?opento=1&wise=srbdl",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>I of 2026</td>
                <td><a href="/Document.aspx?docc=1&docid=2&wise=opendoc">Fake Balochistan Act 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=1&docid=2&wise=opendoc",
        """
        <html><body>
          <h2>Fake Balochistan Act 2026</h2>
          <a href="/Document.aspx?docc=1&docid=2&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add("/Document.aspx?docc=1&docid=2&wise=download", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _balochistan_assembly_source(db, fixture_server, ["/laws_rules.aspx?opento=1&wise=srbdl"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "BalochistanAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=1&docid=2&wise=download"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")


async def test_balochistan_laws_portal_detail_flow_is_idempotent_on_rerun(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/laws_rules.aspx?opento=1&wise=srbdl",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>XI of 2015</td>
                <td><a href="/Document.aspx?docc=10&docid=20&wise=opendoc">Balochistan Idempotence Act 2015</a></td>
                <td>2015</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=10&docid=20&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Idempotence Act 2015</h2>
          <a href="/Document.aspx?docc=10&docid=20&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=10&docid=20&wise=download",
        text_pdf_bytes("Balochistan Idempotence Act 2015"),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/laws_rules.aspx?opento=1&wise=srbdl"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        first = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()
    assert first["halted"] is False

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        second = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()
    assert second["halted"] is False
    assert second["discovered"] == 0

    detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=10&docid=20&wise=opendoc"
    detail_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalar()
    assert detail_count == 1

    document_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=10&docid=20&wise=download"
    document_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalar()
    assert document_count == 1
