from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.federal_shariat_court import normalize_fsc_public_url, scrape_federal_shariat_court
from tests.fixtures import text_pdf_bytes


async def _fsc_source(db, fixture_server, listings, *, config_extra=None):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "FederalShariatCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {"listings": [fixture_server.url(path) for path in listings]}
    if config_extra:
        cfg.update(config_extra)
    source.config_json = cfg
    await db.commit()
    return source


def test_normalize_fsc_public_url_unwraps_and_normalizes():
    wayback = "https://web.archive.org/web/20241005132304/https://www.federalshariatcourt.gov.pk/Judments/Test File.pdf"
    out = normalize_fsc_public_url(wayback, base_url="https://www.federalshariatcourt.gov.pk/en/judgments/")
    assert out == "https://www.federalshariatcourt.gov.pk/Judgments/Test%20File.pdf"

    rel = normalize_fsc_public_url("Judgements/Order 2024.pdf", base_url="https://www.federalshariatcourt.gov.pk/alljud.php")
    assert rel == "https://www.federalshariatcourt.gov.pk/Judgments/Order%202024.pdf"


async def test_fsc_discovery_from_anchor_data_and_inline_script(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/judgments/",
        """
        <html><body>
          <a href="/judnew1.html">Reported Judgements</a>
          <a href="Judgments/Anchor Appeal.pdf">Anchor file</a>
          <div data-doc-url="Judments/Data Evidence.pdf">data-path</div>
          <script>
            const doc = "Judgements/Script Order.pdf";
            const nextListing = "alljud.php?page=2";
          </script>
        </body></html>
        """,
    )
    fixture_server.add("/judnew1.html", '<html><body><a href="alljud.php">All</a></body></html>')
    fixture_server.add("/alljud.php", '<html><body><a href="Judgments/Legacy Petition.pdf">legacy</a></body></html>')
    fixture_server.add("/alljud.php?page=2", '<html><body><a href="Judgments/Page Two Case.pdf">page2</a></body></html>')

    for idx, path in enumerate(
        (
        "/Judgments/Anchor%20Appeal.pdf",
        "/Judgments/Data%20Evidence.pdf",
        "/Judgments/Script%20Order.pdf",
        "/Judgments/Legacy%20Petition.pdf",
        "/Judgments/Page%20Two%20Case.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 FSC {idx}\nFEDERAL SHARIAT COURT\nShariat Petition No. {idx} of 2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _fsc_source(db, fixture_server, ["/en/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["discovered"] >= 5
    hits = set(fixture_server.hits)
    assert "/alljud.php?page=2" in hits
    assert "/Judgments/Data%20Evidence.pdf" in hits
    assert "/Judgments/Script%20Order.pdf" in hits

    pdf_rows = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.source_name == "FederalShariatCourt", SourceProvenance.content_kind == "pdf"))
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/Judgments/Data%%20Evidence.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/Judgments/Script%%20Order.pdf" % fixture_server.port in urls


async def test_pdf_signature_gate_retires_non_pdf_judgment_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/en/judgments/", '<html><body><a href="Judgments/Bad.pdf">bad</a></body></html>')
    fixture_server.add("/Judgments/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _fsc_source(db, fixture_server, ["/en/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "FederalShariatCourt"))).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")


async def test_fsc_alljud_table_window_adds_result_metadata_with_bounded_fanout(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/alljud.php",
        """
        <html><body>
          <table border="1">
            <tr><td>Sr. No</td><td>Case No</td><td>Title</td><td>Download</td><td>Year of Decision</td></tr>
            <tr><td>1</td><td>Shariat Petition No. 1-I of 1980</td><td>Alpha v. Federation</td><td><a href="Judgments/Case-1.pdf">PDF</a></td><td>1980</td></tr>
            <tr><td>2</td><td>Shariat Petition No. 2-I of 1981</td><td>Bravo v. Federation</td><td><a href="Judgments/Case-2.pdf">PDF</a></td><td>1981</td></tr>
            <tr><td>3</td><td>Shariat Petition No. 3-I of 1982</td><td>Charlie v. Federation</td><td><a href="Judgments/Case-3.pdf">PDF</a></td><td>1982</td></tr>
            <tr><td>4</td><td>Shariat Petition No. 4-I of 1983</td><td>Delta v. Federation</td><td><a href="Judgments/Case-4.pdf">PDF</a></td><td>1983</td></tr>
            <tr><td>5</td><td>Shariat Petition No. 5-I of 1984</td><td>Echo v. Federation</td><td><a href="Judgments/Case-5.pdf">PDF</a></td><td>1984</td></tr>
          </table>
        </body></html>
        """,
    )
    for idx in range(1, 6):
        fixture_server.add(
            f"/Judgments/Case-{idx}.pdf",
            text_pdf_bytes(
                f"PLD 198{idx} FSC {idx}\nFEDERAL SHARIAT COURT\nShariat Petition No. {idx} of 198{idx}\nDecided on 1st January 198{idx}\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _fsc_source(
        db,
        fixture_server,
        ["/alljud.php"],
        config_extra={"result_page_size": 2, "result_max_pages": 2},
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 4
    assert "/Judgments/Case-1.pdf" in fixture_server.hits
    assert "/Judgments/Case-4.pdf" in fixture_server.hits
    assert "/Judgments/Case-5.pdf" not in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "FederalShariatCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/Judgments/Case-4.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/Judgments/Case-5.pdf" % fixture_server.port not in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Case-3.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "result_table"
    assert row.query_json["route"]["result_table_kind"] == "alljud"
    assert row.query_json["route"]["result_window_page"] == 2
    assert row.query_json["route"]["result_window_index"] == 0
    assert row.query_json["route"]["result_case_no"] == "Shariat Petition No. 3-I of 1982"
    assert row.query_json["route"]["result_decision_year"] == "1982"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "judgments-dir"


async def test_fsc_orders_table_non_pdf_is_retired_with_route_metadata(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/en/orders/",
        """
        <html><body>
          <table>
            <tr><td><strong>S.No.</strong></td><td><strong>Date</strong></td><td><strong>Orders</strong></td></tr>
            <tr>
              <td>1.</td>
              <td>13.06.2020</td>
              <td><a href="/wp-content/uploads/2020/03/Orders/Bad-Order.pdf">Broken order</a></td>
            </tr>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/wp-content/uploads/2020/03/Orders/Bad-Order.pdf",
        "<html><body>not a pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _fsc_source(db, fixture_server, ["/en/orders/"], config_extra={"result_page_size": 10, "result_max_pages": 1})
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(ScraperStaging)
            .where(ScraperStaging.source_name == "FederalShariatCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2020/03/Orders/Bad-Order.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert row.query_json["route"]["listing_fetch"] == "result_table"
    assert row.query_json["route"]["result_table_kind"] == "orders"
    assert row.query_json["route"]["result_order_date"] == "13.06.2020"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "orders-upload"
    assert "missing %PDF signature" in (row.last_error or "")


async def test_fsc_alljud_rerun_is_idempotent_without_duplicate_frontier_keys(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/alljud.php",
        """
        <html><body>
          <table>
            <tr><td>Sr. No</td><td>Case No</td><td>Title</td><td>Download</td><td>Year of Decision</td></tr>
            <tr><td>1</td><td>Shariat Petition No. 11-I of 1988</td><td>Idempotent v. Federation</td><td><a href="Judgments/Idempotent-1.pdf">PDF</a></td><td>1988</td></tr>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Judgments/Idempotent-1.pdf",
        text_pdf_bytes(
            "PLD 1988 FSC 11\nFEDERAL SHARIAT COURT\nShariat Petition No. 11-I of 1988\nDecided on 1st January 1988\nAppeal dismissed."
        ),
        content_type="application/pdf",
    )

    source = await _fsc_source(db, fixture_server, ["/alljud.php"], config_extra={"result_page_size": 10, "result_max_pages": 1})

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats_first = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats_second = await scrape_federal_shariat_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats_first["discovered"] >= 1
    assert stats_second["discovered"] == 0
    key = f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Idempotent-1.pdf"
    frontier_count = (
        await db.execute(
            select(func.count()).select_from(CrawlFrontier).where(
                CrawlFrontier.source_name == "FederalShariatCourt",
                CrawlFrontier.query_key == key,
            )
        )
    ).scalar()
    assert frontier_count == 1
    assert fixture_server.hits.count("/Judgments/Idempotent-1.pdf") == 1
