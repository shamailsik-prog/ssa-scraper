from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.balochistan_high_court import normalize_bhc_public_url, scrape_balochistan_high_court
from tests.fixtures import text_pdf_bytes


async def _bhc_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "BalochistanHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
    }
    await db.commit()
    return source


def test_normalize_bhc_public_url_unwraps_wayback_and_relative_paths():
    wayback = "https://web.archive.org/web/20241005132304/https://bhc.gov.pk/media/judgments/419.pdf"
    out = normalize_bhc_public_url(wayback, base_url="https://bhc.gov.pk/resources/judgments")
    assert out == "https://bhc.gov.pk/media/judgments/419.pdf"

    rel_pdf = normalize_bhc_public_url("media/judgments/7.pdf", base_url="https://bhc.gov.pk/resources/judgments")
    assert rel_pdf == "https://bhc.gov.pk/media/judgments/7.pdf"

    rel_listing = normalize_bhc_public_url(
        "resources/judgments/justice-qazi-faez-isa/reported-judgments",
        base_url="https://bhc.gov.pk/judgments",
    )
    assert rel_listing == "https://bhc.gov.pk/resources/judgments/justice-qazi-faez-isa/reported-judgments"


async def test_bhc_result_boxes_discover_pdf_judgments_with_route_and_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/resources/judgments",
        """
        <html><body>
          <a href="/resources/judgments/justice-qazi-faez-isa/reported-judgments">Justice Qazi Faez Isa — reported judgments</a>
          <a href="https://portal.bhc.gov.pk/judgments">Portal</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/resources/judgments/justice-qazi-faez-isa/reported-judgments",
        """
        <html><body>
          <div class="judgmentbox margin-bottom-md forceltr">
            <div class="serial">01</div>
            <div class="description">
              <div class="title"><strong>Muhammad Umair v. Government of Balochistan</strong></div>
              <div class="note margin-top-sm"><i>PLD 2013 Balochistan 75</i></div>
            </div>
            <div class="filelink"><a class="popup" data-iframe="true" data-src="https://127.0.0.1:1/not-allowed.pdf">bad</a></div>
            <div class="filelink"><a class="popup" data-iframe="true" data-src="/media/judgments/419.pdf">View Judgment</a></div>
          </div>
          <div class="judgmentbox margin-bottom-md forceltr">
            <div class="serial">02</div>
            <div class="description">
              <div class="title"><strong>Asmatullah Khan v. Government of Balochistan</strong></div>
              <div class="note margin-top-sm"><i>PLD 2013 Balochistan 12</i></div>
            </div>
            <div class="filelink"><a class="popup" data-iframe="true" data-src="media/judgments/4.pdf">View Judgment</a></div>
          </div>
          <script>
            const hidden = "https://127.0.0.1:2/not-allowed.pdf";
          </script>
        </body></html>
        """,
    )

    fixture_server.add(
        "/media/judgments/419.pdf",
        text_pdf_bytes(
            "PLD 2013 Balochistan 75\nBalochistan High Court\nConstitution Petition No. 1 of 2013\nDecided on 12th March 2013\nPetition allowed."
        ),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/media/judgments/4.pdf",
        text_pdf_bytes(
            "PLD 2013 Balochistan 12\nBalochistan High Court\nConstitution Petition No. 2 of 2013\nDecided on 14th March 2013\nPetition dismissed."
        ),
        content_type="application/pdf",
    )

    source = await _bhc_source(db, fixture_server, ["/resources/judgments"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 2
    assert "/media/judgments/419.pdf" in fixture_server.hits
    assert "/media/judgments/4.pdf" in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "BalochistanHighCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/media/judgments/419.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/media/judgments/4.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/media/judgments/419.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "result_box"
    assert row.query_json["route"]["result_serial"] == "01"
    assert row.query_json["route"]["result_year"] == "2013"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "media-judgments"


async def test_bhc_pdf_signature_gate_retires_non_pdf_document_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/resources/judgments/justice-qazi-faez-isa/reported-judgments",
        """
        <html><body>
          <div class="judgmentbox">
            <div class="serial">01</div>
            <div class="description">
              <div class="title"><strong>Bad PDF</strong></div>
              <div class="note margin-top-sm"><i>PLD 2015 Balochistan 10</i></div>
            </div>
            <div class="filelink"><a data-src="/media/judgments/Bad.pdf">View Judgment</a></div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add("/media/judgments/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _bhc_source(db, fixture_server, ["/resources/judgments/justice-qazi-faez-isa/reported-judgments"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(ScraperStaging)
            .where(ScraperStaging.source_name == "BalochistanHighCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/media/judgments/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
