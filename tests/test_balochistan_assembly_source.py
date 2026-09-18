from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_pab_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _balochistan_assembly_source(db, fixture_server, listings, *, target_kind: str = "statute"):
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
        "target_kind": target_kind,
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


def test_balochistan_assembly_default_listings_include_instrument_section_seeds():
    listing_map = {entry["url"]: entry["target_kind"] for entry in DEFAULT_LISTINGS["BalochistanAssembly"]}
    assert listing_map["https://www.pabalochistan.gov.pk/acts"] == "statute"
    assert listing_map["https://balochistancode.gob.pk/laws_rules.aspx?opento=1&wise=srbdl"] == "statute"
    assert listing_map["https://www.pabalochistan.gov.pk/ordinance-laid"] == "instrument"
    assert listing_map["https://www.pabalochistan.gov.pk/bills"] == "instrument"
    assert listing_map["https://www.pabalochistan.gov.pk/notifications"] == "instrument"


async def test_balochistan_pab_notifications_seed_fans_out_listing_and_instrument_pdf(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/notifications",
        """
        <html><body>
          <a href="/notifications?page=2">Next</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/notifications?page=2",
        """
        <html><body>
          <a href="/storage/9100/recruitment-rules-2026.pdf">Recruitment Rules 2026</a>
        </body></html>
        """,
    )
    fixture_server.add("/storage/9100/recruitment-rules-2026.pdf", text_pdf_bytes("Recruitment Rules 2026"), content_type="application/pdf")

    source = await _balochistan_assembly_source(db, fixture_server, ["/notifications"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["halted"] is False
    assert "/notifications?page=2" in fixture_server.hits
    assert "/storage/9100/recruitment-rules-2026.pdf" in fixture_server.hits

    paged_listing_url = fixture_server.url("/notifications?page=2")
    paged_listing = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"listing:{paged_listing_url}",
            )
        )
    ).scalars().first()
    assert paged_listing is not None
    assert paged_listing.query_json["target_kind"] == "instrument"

    document_url = fixture_server.url("/storage/9100/recruitment-rules-2026.pdf")
    instrument_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    statute_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert instrument_row is not None
    assert statute_row is None
    assert instrument_row.query_json["expect_pdf"] is True
    assert instrument_row.query_json["route"]["source_section"] == "notifications"


async def test_balochistan_pab_bill_seed_non_pdf_candidate_fails_closed_as_instrument(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/bills",
        """
        <html><body>
          <a href="/storage/9200/fake-balochistan-bill.pdf">Fake Balochistan Bill</a>
        </body></html>
        """,
    )
    fixture_server.add("/storage/9200/fake-balochistan-bill.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _balochistan_assembly_source(db, fixture_server, ["/bills"], target_kind="instrument")
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

    document_url = fixture_server.url("/storage/9200/fake-balochistan-bill.pdf")
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


async def test_balochistan_pab_ordinance_seed_rerun_remains_idempotent_with_instrument_key(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/ordinance-laid",
        """
        <html><body>
          <a href="/storage/9300/balochistan-sample-ordinance-2026.pdf">Balochistan Sample Ordinance 2026</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/storage/9300/balochistan-sample-ordinance-2026.pdf",
        text_pdf_bytes("Balochistan Sample Ordinance 2026"),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/ordinance-laid"], target_kind="instrument")
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    keys_before = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
            )
        )
    ).scalars().all()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    keys_after = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
            )
        )
    ).scalars().all()

    assert len(keys_before) == len(keys_after)
    assert set(keys_before) == set(keys_after)
    document_url = fixture_server.url("/storage/9300/balochistan-sample-ordinance-2026.pdf")
    assert f"instrument:{document_url}" in set(keys_after)
    assert f"statute:{document_url}" not in set(keys_after)


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


async def test_balochistan_laws_portal_routes_ordinance_and_rules_to_instrument(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/laws_rules.aspx?opento=1&wise=srbdl",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th><th>Type</th></tr></thead>
            <tbody>
              <tr>
                <td>I of 2021</td>
                <td><a href="/Document.aspx?docc=11&docid=21&wise=opendoc">Balochistan Tourism Act 2021</a></td>
                <td>2021</td>
                <td>Act</td>
              </tr>
              <tr>
                <td>II of 2022</td>
                <td><a href="/Document.aspx?docc=12&docid=22&wise=opendoc">Balochistan Markets Ordinance 2022</a></td>
                <td>2022</td>
                <td>Ordinance</td>
              </tr>
              <tr>
                <td>III of 2023</td>
                <td><a href="/Document.aspx?docc=13&docid=23&wise=opendoc">Balochistan Food Rules 2023</a></td>
                <td>2023</td>
                <td>Rules</td>
              </tr>
              <tr>
                <td>IV of 2020</td>
                <td><a href="/Document.aspx?docc=14&docid=24&wise=opendoc">Balochistan Fiscal Digest 2020</a></td>
                <td>2020</td>
                <td>Circular</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=11&docid=21&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Tourism Act 2021</h2>
          <a href="/Document.aspx?docc=11&docid=21&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=12&docid=22&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Markets Ordinance 2022</h2>
          <table><tr><th>Type</th><td>Ordinance</td></tr></table>
          <a href="/Document.aspx?docc=12&docid=22&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=13&docid=23&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Food Rules 2023</h2>
          <table><tr><th>Type</th><td>Rules</td></tr></table>
          <a href="/Document.aspx?docc=13&docid=23&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=14&docid=24&wise=opendoc",
        """
        <html><body>
          <h2>Balochistan Fiscal Digest 2020</h2>
          <table><tr><th>Type</th><td>Circular</td></tr></table>
          <a href="/Document.aspx?docc=14&docid=24&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=11&docid=21&wise=download",
        text_pdf_bytes("Balochistan Tourism Act 2021"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/Document.aspx?docc=12&docid=22&wise=download",
        text_pdf_bytes("Balochistan Markets Ordinance 2022"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/Document.aspx?docc=13&docid=23&wise=download",
        text_pdf_bytes("Balochistan Food Rules 2023"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/Document.aspx?docc=14&docid=24&wise=download",
        text_pdf_bytes("Balochistan Fiscal Digest 2020"),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/laws_rules.aspx?opento=1&wise=srbdl"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=120)
    await db.commit()

    assert stats["halted"] is False

    act_detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=11&docid=21&wise=opendoc"
    ordinance_detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=12&docid=22&wise=opendoc"
    rules_detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=13&docid=23&wise=opendoc"
    unknown_detail_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=14&docid=24&wise=opendoc"

    detail_rows = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key.in_(
                    [
                        f"listing:{act_detail_url}",
                        f"listing:{ordinance_detail_url}",
                        f"listing:{rules_detail_url}",
                        f"listing:{unknown_detail_url}",
                    ]
                ),
            )
        )
    ).scalars().all()
    detail_by_url = {row.query_json["url"]: row for row in detail_rows}
    assert detail_by_url[act_detail_url].query_json["target_kind"] == "statute"
    assert detail_by_url[ordinance_detail_url].query_json["target_kind"] == "instrument"
    assert detail_by_url[rules_detail_url].query_json["target_kind"] == "instrument"
    assert detail_by_url[unknown_detail_url].query_json["target_kind"] == "statute"

    act_doc_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=11&docid=21&wise=download"
    ordinance_doc_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=12&docid=22&wise=download"
    rules_doc_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=13&docid=23&wise=download"
    unknown_doc_url = f"http://127.0.0.1:{fixture_server.port}/Document.aspx?docc=14&docid=24&wise=download"

    act_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{act_doc_url}",
            )
        )
    ).scalars().first()
    ordinance_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"instrument:{ordinance_doc_url}",
            )
        )
    ).scalars().first()
    rules_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"instrument:{rules_doc_url}",
            )
        )
    ).scalars().first()
    unknown_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{unknown_doc_url}",
            )
        )
    ).scalars().first()
    assert act_doc is not None
    assert ordinance_doc is not None
    assert rules_doc is not None
    assert unknown_doc is not None
    assert ordinance_doc.query_json["route"]["detail_url"] == ordinance_detail_url
    assert rules_doc.query_json["route"]["detail_url"] == rules_detail_url

    statute_ordinance = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{ordinance_doc_url}",
            )
        )
    ).scalar()
    assert statute_ordinance == 0


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
                <td><a href="/Document.aspx?docc=1&docid=2&wise=opendoc">Fake Balochistan Ordinance 2026</a></td>
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
          <h2>Fake Balochistan Ordinance 2026</h2>
          <table><tr><th>Type</th><td>Ordinance</td></tr></table>
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
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


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
                <td><a href="/Document.aspx?docc=10&docid=20&wise=opendoc">Balochistan Idempotence Rules 2015</a></td>
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
          <h2>Balochistan Idempotence Rules 2015</h2>
          <table><tr><th>Type</th><td>Rules</td></tr></table>
          <a href="/Document.aspx?docc=10&docid=20&wise=download">Download</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Document.aspx?docc=10&docid=20&wise=download",
        text_pdf_bytes("Balochistan Idempotence Rules 2015"),
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
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalar()
    assert document_count == 1
