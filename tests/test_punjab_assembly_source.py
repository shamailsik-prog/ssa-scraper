from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import listings_for, normalize_pap_public_url, normalize_punjab_public_url, scrape_legislature
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


async def test_punjab_default_listings_include_pap_and_punjablaws_seeds(db):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PunjabAssembly",
            )
        )
    ).scalars().first()
    source.config_json = {}
    await db.commit()

    listing_map = {row["url"]: row["target_kind"] for row in listings_for(source)}
    assert listing_map["https://www.pap.gov.pk/acts"] == "statute"
    assert listing_map["https://punjablaws.gov.pk/index.html"] == "statute"
    assert listing_map["https://punjablaws.gov.pk/ordinances"] == "instrument"
    assert listing_map["https://punjablaws.gov.pk/rules"] == "instrument"
    assert listing_map["https://punjablaws.gov.pk/bills"] == "instrument"


def test_normalize_pap_public_url_handles_relative_uploads_path():
    rel = normalize_pap_public_url(
        "/uploads/acts/301.html",
        base_url="https://www.pap.gov.pk/acts",
    )
    assert rel == "https://pap.gov.pk/uploads/acts/301.html"


def test_normalize_punjab_public_url_canonicalizes_punjablaws_host():
    rel = normalize_punjab_public_url(
        "//www.punjablaws.gov.pk/acts/punjab%20green%20act%202025",
        base_url="https://punjablaws.gov.pk/index.html",
    )
    assert rel == "https://punjablaws.gov.pk/acts/punjab%20green%20act%202025"


async def test_punjablaws_root_listing_overrides_target_kind_by_section(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/index.html",
        """
        <html><body>
          <a href="/acts/list-2026.html">Acts</a>
          <a href="/ordinances/list-2026.html">Ordinances</a>
          <a href="/rules/list-2026.html">Rules</a>
          <a href="/bills/list-2026.html">Bills</a>
        </body></html>
        """,
    )
    fixture_server.add("/acts/list-2026.html", "<html><body></body></html>")
    fixture_server.add("/ordinances/list-2026.html", "<html><body></body></html>")
    fixture_server.add("/rules/list-2026.html", "<html><body></body></html>")
    fixture_server.add("/bills/list-2026.html", "<html><body></body></html>")

    source = await _punjab_assembly_source(db, fixture_server, ["/index.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False

    act_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{fixture_server.url('/acts/list-2026.html')}",
            )
        )
    ).scalars().first()
    ordinance_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{fixture_server.url('/ordinances/list-2026.html')}",
            )
        )
    ).scalars().first()
    rules_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{fixture_server.url('/rules/list-2026.html')}",
            )
        )
    ).scalars().first()
    bills_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{fixture_server.url('/bills/list-2026.html')}",
            )
        )
    ).scalars().first()

    assert act_row is not None
    assert ordinance_row is not None
    assert rules_row is not None
    assert bills_row is not None
    assert act_row.query_json["target_kind"] == "statute"
    assert ordinance_row.query_json["target_kind"] == "instrument"
    assert rules_row.query_json["target_kind"] == "instrument"
    assert bills_row.query_json["target_kind"] == "instrument"
    assert ordinance_row.query_json["meta"]["source_section"] == "ordinances"
    assert rules_row.query_json["meta"]["source_section"] == "rules"
    assert bills_row.query_json["meta"]["source_section"] == "bills"


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


async def test_punjab_assembly_punjablaws_listing_routes_detail_then_pdf_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/index.html",
        """
        <html><body>
          <table class="table table-striped">
            <thead>
              <tr><th>Law No.</th><th>Title</th><th>Year</th></tr>
            </thead>
            <tbody>
              <tr>
                <td>XX</td>
                <td><a href="/acts/punjab-green-act-2025">Punjab Green Act, 2025</a></td>
                <td>2025</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/punjab-green-act-2025",
        """
        <html><body>
          <h1>Punjab Green Act, 2025</h1>
          <table>
            <tr><th>Act No</th><td>XX of 2025</td></tr>
            <tr><th>Date of Passing</th><td>10 July 2025</td></tr>
          </table>
          <a href="/downloads/punjab-green-act-2025.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/downloads/punjab-green-act-2025.pdf",
        text_pdf_bytes("Punjab Green Act, 2025"),
        content_type="application/pdf",
    )

    source = await _punjab_assembly_source(db, fixture_server, ["/index.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/acts/punjab-green-act-2025" in fixture_server.hits
    assert "/downloads/punjab-green-act-2025.pdf" in fixture_server.hits

    detail_url = f"http://127.0.0.1:{fixture_server.port}/acts/punjab-green-act-2025"
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["route"]["listing_fetch"] == "punjablaws_table"
    assert detail_row.query_json["route"]["act_no"] == "XX"
    assert detail_row.query_json["route"]["act_year"] == "2025"

    document_url = f"http://127.0.0.1:{fixture_server.port}/downloads/punjab-green-act-2025.pdf"
    doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert doc_row is not None
    assert doc_row.query_json["expect_pdf"] is True
    assert doc_row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert doc_row.query_json["route"]["detail_url"] == detail_url
    assert doc_row.query_json["route"]["act_no"] == "XX"
    assert doc_row.query_json["route"]["act_year"] == "2025"
    assert doc_row.query_json["route"]["pdf_endpoint_kind"] == "punjablaws-download-file"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "PunjabAssembly",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["act_title"] == "Punjab Green Act, 2025"
    assert prov.route_json["detail_url"] == detail_url


async def test_punjablaws_mixed_section_fanout_preserves_statute_and_instrument_keys(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/index.html",
        """
        <html><body>
          <a href="/acts/list-2026.html">Acts</a>
          <a href="/ordinances/list-2026.html">Ordinances</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/list-2026.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>XIII</td>
                <td><a href="/acts/punjab-labor-act-2026">Punjab Labor Act, 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/ordinances/list-2026.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>IV</td>
                <td><a href="/ordinances/punjab-public-order-ordinance-2026">Punjab Public Order Ordinance, 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/punjab-labor-act-2026",
        """
        <html><body>
          <h1>Punjab Labor Act, 2026</h1>
          <table><tr><th>Act No</th><td>XIII of 2026</td></tr></table>
          <a href="/downloads/punjab-labor-act-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/ordinances/punjab-public-order-ordinance-2026",
        """
        <html><body>
          <h1>Punjab Public Order Ordinance, 2026</h1>
          <table><tr><th>Act No</th><td>IV of 2026</td></tr></table>
          <a href="/downloads/punjab-public-order-ordinance-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/punjab-labor-act-2026.pdf", text_pdf_bytes("Punjab Labor Act, 2026"), content_type="application/pdf")
    fixture_server.add(
        "/downloads/punjab-public-order-ordinance-2026.pdf",
        text_pdf_bytes("Punjab Public Order Ordinance, 2026"),
        content_type="application/pdf",
    )

    source = await _punjab_assembly_source(db, fixture_server, ["/index.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=90)
    await db.commit()

    assert stats["halted"] is False
    assert "/acts/punjab-labor-act-2026" in fixture_server.hits
    assert "/ordinances/punjab-public-order-ordinance-2026" in fixture_server.hits
    assert "/downloads/punjab-labor-act-2026.pdf" in fixture_server.hits
    assert "/downloads/punjab-public-order-ordinance-2026.pdf" in fixture_server.hits

    act_doc_url = fixture_server.url("/downloads/punjab-labor-act-2026.pdf")
    ordinance_doc_url = fixture_server.url("/downloads/punjab-public-order-ordinance-2026.pdf")

    act_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{act_doc_url}",
            )
        )
    ).scalars().first()
    ordinance_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"instrument:{ordinance_doc_url}",
            )
        )
    ).scalars().first()
    ordinance_statute_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{ordinance_doc_url}",
            )
        )
    ).scalars().first()
    ordinance_detail_url = fixture_server.url("/ordinances/punjab-public-order-ordinance-2026")
    ordinance_detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{ordinance_detail_url}",
            )
        )
    ).scalars().first()

    assert act_doc_row is not None
    assert ordinance_doc_row is not None
    assert ordinance_statute_row is None
    assert ordinance_detail_row is not None
    assert ordinance_detail_row.query_json["target_kind"] == "instrument"
    assert ordinance_doc_row.query_json["route"]["source_section"] == "ordinances"


async def test_punjablaws_ordinance_listing_nav_link_to_act_remains_statute(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/ordinances/list-2026.html",
        """
        <html><body>
          <a href="/acts/punjab-labor-act-2026">Punjab Labor Act, 2026</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/punjab-labor-act-2026",
        """
        <html><body>
          <h1>Punjab Labor Act, 2026</h1>
          <a href="/downloads/punjab-labor-act-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/punjab-labor-act-2026.pdf", text_pdf_bytes("Punjab Labor Act, 2026"), content_type="application/pdf")

    source = await _punjab_assembly_source(db, fixture_server, ["/ordinances/list-2026.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/acts/punjab-labor-act-2026" in fixture_server.hits
    assert "/downloads/punjab-labor-act-2026.pdf" in fixture_server.hits

    detail_url = fixture_server.url("/acts/punjab-labor-act-2026")
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["target_kind"] == "statute"
    assert detail_row.query_json["meta"]["source_section"] == "acts"

    document_url = fixture_server.url("/downloads/punjab-labor-act-2026.pdf")
    statute_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    instrument_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert statute_row is not None
    assert instrument_row is None
    assert statute_row.query_json["route"]["source_section"] == "acts"


async def test_punjablaws_bills_detail_listing_without_html_fans_out_documents(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/bills/list-2026.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>VII</td>
                <td><a href="/bills/punjab-green-bill-2026">Punjab Green Bill, 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/bills/punjab-green-bill-2026",
        """
        <html><body>
          <h1>Punjab Green Bill, 2026</h1>
          <a href="/downloads/punjab-green-bill-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/punjab-green-bill-2026.pdf", text_pdf_bytes("Punjab Green Bill, 2026"), content_type="application/pdf")

    source = await _punjab_assembly_source(db, fixture_server, ["/bills/list-2026.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/bills/punjab-green-bill-2026" in fixture_server.hits
    assert "/downloads/punjab-green-bill-2026.pdf" in fixture_server.hits

    detail_url = fixture_server.url("/bills/punjab-green-bill-2026")
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["target_kind"] == "instrument"
    assert detail_row.query_json["meta"]["source_section"] == "bills"

    document_url = fixture_server.url("/downloads/punjab-green-bill-2026.pdf")
    doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert doc_row is not None
    assert doc_row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert doc_row.query_json["route"]["source_section"] == "bills"


async def test_punjablaws_rules_seed_fans_out_instrument_document(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/rules/list-2026.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>XI</td>
                <td><a href="/rules/punjab-sample-rules-2026">Punjab Sample Rules, 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/rules/punjab-sample-rules-2026",
        """
        <html><body>
          <h1>Punjab Sample Rules, 2026</h1>
          <a href="/downloads/punjab-sample-rules-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/punjab-sample-rules-2026.pdf", text_pdf_bytes("Punjab Sample Rules, 2026"), content_type="application/pdf")

    source = await _punjab_assembly_source(db, fixture_server, ["/rules/list-2026.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    assert "/rules/punjab-sample-rules-2026" in fixture_server.hits
    assert "/downloads/punjab-sample-rules-2026.pdf" in fixture_server.hits

    detail_url = fixture_server.url("/rules/punjab-sample-rules-2026")
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.query_json["target_kind"] == "instrument"
    assert detail_row.query_json["meta"]["source_section"] == "rules"

    document_url = fixture_server.url("/downloads/punjab-sample-rules-2026.pdf")
    doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    statute_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert doc_row is not None
    assert statute_row is None
    assert doc_row.query_json["route"]["detail_fetch"] == "detail_documents"
    assert doc_row.query_json["route"]["source_section"] == "rules"


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


async def test_punjablaws_ordinance_seed_non_pdf_candidate_fails_closed_as_instrument(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/ordinances/list-2026.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>V</td>
                <td><a href="/downloads/fake-punjab-ordinance.pdf">Punjab Sample Ordinance, 2026</a></td>
                <td>2026</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add("/downloads/fake-punjab-ordinance.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _punjab_assembly_source(db, fixture_server, ["/ordinances/list-2026.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
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

    doc_url = fixture_server.url("/downloads/fake-punjab-ordinance.pdf")
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PunjabAssembly",
                CrawlFrontier.query_key == f"instrument:{doc_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


async def test_punjablaws_ordinance_seed_rerun_remains_idempotent_with_instrument_key(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/ordinances/list-2025.html",
        """
        <html><body>
          <table>
            <thead><tr><th>Law No.</th><th>Title</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td>IX</td>
                <td><a href="/downloads/punjab-energy-ordinance-2025.pdf">Punjab Energy Ordinance, 2025</a></td>
                <td>2025</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/downloads/punjab-energy-ordinance-2025.pdf",
        text_pdf_bytes("Punjab Energy Ordinance, 2025"),
        content_type="application/pdf",
    )

    source = await _punjab_assembly_source(db, fixture_server, ["/ordinances/list-2025.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    keys_before = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "PunjabAssembly",
            )
        )
    ).scalars().all()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    keys_after = (
        await db.execute(
            select(CrawlFrontier.query_key).where(
                CrawlFrontier.source_name == "PunjabAssembly",
            )
        )
    ).scalars().all()

    assert len(keys_before) == len(keys_after)
    assert set(keys_before) == set(keys_after)
    doc_url = fixture_server.url("/downloads/punjab-energy-ordinance-2025.pdf")
    assert f"instrument:{doc_url}" in set(keys_after)
    assert f"statute:{doc_url}" not in set(keys_after)
