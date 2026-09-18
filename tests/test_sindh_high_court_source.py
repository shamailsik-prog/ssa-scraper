from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.sindh_high_court import normalize_shc_public_url, scrape_sindh_high_court
from tests.fixtures import text_pdf_bytes


async def _shc_source(
    db,
    fixture_server,
    listings,
    *,
    detail_result_page_size=2,
    detail_result_max_pages=2,
    report_result_page_size=2,
    report_result_max_pages=1,
):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SindhHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "detail_result_page_size": detail_result_page_size,
        "detail_result_max_pages": detail_result_max_pages,
        "report_result_page_size": report_result_page_size,
        "report_result_max_pages": report_result_max_pages,
    }
    await db.commit()
    return source


def test_normalize_shc_public_url_view_and_download_forms():
    wayback = "https://web.archive.org/web/20241005132304/https://caselaw.shc.gov.pk/caselaw/view-file/T0tFTj0="
    out = normalize_shc_public_url(wayback, base_url="https://caselaw.shc.gov.pk/caselaw/public/home")
    assert out == "https://caselaw.shc.gov.pk/caselaw/view-file/T0tFTj0="

    rel = normalize_shc_public_url(
        "download-file.php?doc=T0tFTj0%3D&citation=2026+SHC+KHI+1",
        base_url="https://caselaw.shc.gov.pk/caselaw/public/reported-judgements-detail-all/844/-1",
    )
    assert rel == "https://caselaw.shc.gov.pk/caselaw/download-file.php?doc=T0tFTj0%3D&citation=2026%20SHC%20KHI%201"


async def test_shc_result_grids_extract_view_file_pdfs_with_window_and_route(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/caselaw/public/rpt-afr",
        """
        <html><body>
          <a href="public/reported-judgements-detail-all/844/-1">3704</a>
          <a href="/caselaw/public/reported-judgements-detail-all/1321/-1">34</a>
          <a href="/caselaw/public/reported-judgements-detail-all/1550/-1">11</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/caselaw/public/reported-judgements-detail-all/844/-1",
        """
        <html><body>
          <table><tbody>
            <tr>
              <td>1</td>
              <td>2026 SHC KHI 1001</td>
              <td>
                <!-- <a href="view-file/TOKEN-1">token</a> -->
                <a href="download-file.php?doc=TOKEN-1&citation=2026+SHC+KHI+1001">Case one</a>
              </td>
            </tr>
            <tr>
              <td>2</td>
              <td>2026 SHC KHI 1002</td>
              <td>
                <!-- <a href="view-file/TOKEN-2">token</a> -->
                <a href="download-file.php?doc=TOKEN-2&citation=2026+SHC+KHI+1002">Case two</a>
              </td>
            </tr>
            <tr>
              <td>3</td>
              <td>2026 SHC KHI 1003</td>
              <td>
                <!-- <a href="view-file/TOKEN-3">token</a> -->
                <a href="download-file.php?doc=TOKEN-3&citation=2026+SHC+KHI+1003">Case three</a>
              </td>
            </tr>
            <tr>
              <td>4</td>
              <td>2026 SHC KHI 1004</td>
              <td>
                <!-- <a href="view-file/TOKEN-4">token</a> -->
                <a href="download-file.php?doc=TOKEN-4&citation=2026+SHC+KHI+1004">Case four</a>
              </td>
            </tr>
            <tr>
              <td>5</td>
              <td>2026 SHC KHI 1005</td>
              <td>
                <!-- <a href="view-file/TOKEN-5">token</a> -->
                <a href="download-file.php?doc=TOKEN-5&citation=2026+SHC+KHI+1005">Case five</a>
              </td>
            </tr>
          </tbody></table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/caselaw/public/reported-judgements-detail-all/1321/-1",
        """
        <html><body>
          <table><tbody>
            <tr>
              <td>1</td>
              <td>Nil</td>
              <td><!-- <a href="view-file/TOKEN-A">token</a> --><a href="download-file.php?doc=TOKEN-A&citation=Nil">A</a></td>
            </tr>
          </tbody></table>
        </body></html>
        """,
    )

    for token in ("TOKEN-1", "TOKEN-2", "TOKEN-3", "TOKEN-4", "TOKEN-5", "TOKEN-A"):
        fixture_server.add(
            f"/caselaw/view-file/{token}",
            text_pdf_bytes(
                f"2026 SHC KHI {token}\nHIGH COURT OF SINDH\nConst. P. 1/2026\nDecided on 1st January 2026\nOrder maintained."
            ),
            content_type="application/pdf",
        )

    source = await _shc_source(db, fixture_server, ["/caselaw/public/rpt-afr"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 5
    assert "/caselaw/public/reported-judgements-detail-all/844/-1" in fixture_server.hits
    assert "/caselaw/public/reported-judgements-detail-all/1321/-1" in fixture_server.hits
    assert "/caselaw/public/reported-judgements-detail-all/1550/-1" not in fixture_server.hits
    assert "/caselaw/view-file/TOKEN-1" in fixture_server.hits
    assert "/caselaw/view-file/TOKEN-4" in fixture_server.hits
    assert "/caselaw/view-file/TOKEN-5" not in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "SindhHighCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/caselaw/view-file/TOKEN-1" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/caselaw/view-file/TOKEN-4" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/caselaw/view-file/TOKEN-5" % fixture_server.port not in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/caselaw/view-file/TOKEN-3",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "result_grid"
    assert row.query_json["route"]["result_grid_kind"] == "reported-judgements-detail-all"
    assert row.query_json["route"]["result_window_page"] == 2
    assert row.query_json["route"]["pdf_endpoint_kind"] == "view-file"


async def test_shc_pdf_signature_gate_retires_non_pdf_file_view_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/caselaw/public/reported-judgements-detail-all/844/-1",
        """
        <html><body>
          <table><tbody>
            <tr>
              <td>1</td>
              <td>Nil</td>
              <td><!-- <a href="view-file/BAD-TOKEN">token</a> --><a href="download-file.php?doc=BAD-TOKEN&citation=Nil">Bad</a></td>
            </tr>
          </tbody></table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/caselaw/view-file/BAD-TOKEN",
        "<html><body>not pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _shc_source(
        db,
        fixture_server,
        ["/caselaw/public/reported-judgements-detail-all/844/-1"],
        detail_result_page_size=10,
        detail_result_max_pages=1,
        report_result_page_size=1,
        report_result_max_pages=1,
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(ScraperStaging)
            .where(ScraperStaging.source_name == "SindhHighCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/caselaw/view-file/BAD-TOKEN",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
