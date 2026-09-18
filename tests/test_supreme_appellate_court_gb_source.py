from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.supreme_appellate_court_gb import (
    normalize_sacgb_public_url,
    scrape_supreme_appellate_court_gb,
)
from tests.fixtures import text_pdf_bytes


async def _sacgb_source(db, fixture_server, listings, *, config_extra=None):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "SupremeAppellateCourtGB")
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {"listings": [fixture_server.url(path) for path in listings]}
    if config_extra:
        cfg.update(config_extra)
    source.config_json = cfg
    await db.commit()
    return source


def test_normalize_sacgb_public_url_unwraps_wayback_and_host():
    wayback = "https://web.archive.org/web/20241005132304/https://www.sacgb.gov.pk/Judgments/judgements-2021/Test File.pdf"
    out = normalize_sacgb_public_url(wayback, base_url="https://sacgb.gov.pk/Judgments.html")
    assert out == "https://sacgb.gov.pk/Judgments/judgements-2021/Test%20File.pdf"

    rel = normalize_sacgb_public_url(
        "Judgments/latest_judgements/Order 2024.pdf",
        base_url="https://sacgb.gov.pk/Latest%20Judgements.html",
    )
    assert rel == "https://sacgb.gov.pk/Judgments/latest_judgements/Order%202024.pdf"


async def test_sacgb_discovery_from_anchor_data_script_and_wayback(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    wayback_pdf = (
        "https://web.archive.org/web/20241005132304/"
        f"{fixture_server.url('/Judgments/judgements-2021/Wayback%20Appeal.pdf')}"
    )
    fixture_server.add(
        "/Judgments.html",
        f"""
        <html><body>
          <a href="/Latest Judgements.html">Latest Judgements</a>
          <a href="Judgments/judgements2022-2026/Anchor Appeal.pdf">Anchor file</a>
          <div data-doc-url="Judgments/latest_judgements/Data Evidence.pdf">data-path</div>
          <script>
            const doc = "Judgments/judgements-2021/Script Order.pdf";
            const latest = "Latest%20Judgements.html";
          </script>
          <a href="{wayback_pdf}">Wayback PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Latest%20Judgements.html",
        '<html><body><a href="Judgments/latest_judgements/Latest Case.pdf">latest</a></body></html>',
    )
    fixture_server.add(
        "/Latest Judgements.html",
        '<html><body><a href="Judgments/latest_judgements/Latest Case.pdf">latest</a></body></html>',
    )

    for idx, path in enumerate(
        (
            "/Judgments/judgements2022-2026/Anchor%20Appeal.pdf",
            "/Judgments/latest_judgements/Data%20Evidence.pdf",
            "/Judgments/judgements-2021/Script%20Order.pdf",
            "/Judgments/judgements-2021/Wayback%20Appeal.pdf",
            "/Judgments/latest_judgements/Latest%20Case.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 SACGB {idx}\nSUPREME APPELLATE COURT GILGIT-BALTISTAN\nCivil Appeal No. {idx} of 2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _sacgb_source(db, fixture_server, ["/Judgments.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_appellate_court_gb(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["discovered"] >= 5
    hits = set(fixture_server.hits)
    assert "/Latest%20Judgements.html" in hits
    assert "/Judgments/judgements-2021/Script%20Order.pdf" in hits
    assert "/Judgments/judgements-2021/Wayback%20Appeal.pdf" in hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "SupremeAppellateCourtGB",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/Judgments/judgements-2021/Script%%20Order.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/Judgments/latest_judgements/Latest%%20Case.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeAppellateCourtGB",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/judgements-2021/Script%20Order.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["meta"]["discovery_channel"] == "inline-script"
    assert row.query_json["route"]["listing"] == f"http://127.0.0.1:{fixture_server.port}/Judgments.html"


async def test_sacgb_pdf_signature_gate_retires_non_pdf_judgment_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/Judgments.html", '<html><body><a href="Judgments/Bad.pdf">bad</a></body></html>')
    fixture_server.add("/Judgments/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _sacgb_source(db, fixture_server, ["/Judgments.html"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_appellate_court_gb(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "SupremeAppellateCourtGB")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeAppellateCourtGB",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")


async def test_sacgb_judgments_table_window_adds_result_metadata_with_bounded_fanout(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Judgments.html",
        """
        <html><body>
          <table id="myTable">
            <tr><th>Case Title</th></tr>
            <tr><td><a href="Judgments/judgements2022-2026/Case-1.pdf">Cr.Appeal No. 1/2024 Alpha v. State</a></td></tr>
            <tr><td><a href="Judgments/judgements2022-2026/Case-2.pdf">Cr.Appeal No. 2/2024 Bravo v. State</a></td></tr>
            <tr><td><a href="Judgments/judgements2022-2026/Case-3.pdf">Cr.Appeal No. 3/2024 Charlie v. State</a></td></tr>
            <tr><td><a href="Judgments/judgements2022-2026/Case-4.pdf">Cr.Appeal No. 4/2024 Delta v. State</a></td></tr>
            <tr><td><a href="Judgments/judgements2022-2026/Case-5.pdf">Cr.Appeal No. 5/2024 Echo v. State</a></td></tr>
          </table>
        </body></html>
        """,
    )
    for idx in range(1, 6):
        fixture_server.add(
            f"/Judgments/judgements2022-2026/Case-{idx}.pdf",
            text_pdf_bytes(
                f"PLD 2024 SACGB {idx}\nSUPREME APPELLATE COURT GILGIT-BALTISTAN\nCr.Appeal No. {idx}/2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _sacgb_source(
        db,
        fixture_server,
        ["/Judgments.html"],
        config_extra={"result_page_size": 2, "result_max_pages": 2},
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_appellate_court_gb(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 4
    assert "/Judgments/judgements2022-2026/Case-1.pdf" in fixture_server.hits
    assert "/Judgments/judgements2022-2026/Case-4.pdf" in fixture_server.hits
    assert "/Judgments/judgements2022-2026/Case-5.pdf" not in fixture_server.hits

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeAppellateCourtGB",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/judgements2022-2026/Case-3.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "result_table"
    assert row.query_json["route"]["result_table_kind"] == "judgments"
    assert row.query_json["route"]["result_window_page"] == 2
    assert row.query_json["route"]["result_window_index"] == 0
    assert row.query_json["route"]["result_title"] == "Cr.Appeal No. 3/2024 Charlie v. State"


async def test_sacgb_latest_judgements_table_non_pdf_is_retired_with_result_route_metadata(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Latest%20Judgements.html",
        """
        <html><body>
          <table class="table table-hover table-bordered w-auto">
            <tr>
              <th>Sr.No.</th><th>Case Subject</th><th>Case No.</th><th>Case Title</th>
              <th>Author Judge</th><th>Judgment Date</th><th>Upload Date</th><th>Download</th>
            </tr>
            <tr>
              <td>1.</td><td>Bail</td><td>Cr. Misc No. 14/2022</td><td>Shoaib Ahmed vs State</td>
              <td>Mr. Justice Sample Judge</td><td>29-09-2022</td><td>03-11-2022</td>
              <td><a href="Judgments/latest_judgements/Bad-Order.pdf">Download</a></td>
            </tr>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/Judgments/latest_judgements/Bad-Order.pdf",
        "<html><body>not a pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _sacgb_source(
        db,
        fixture_server,
        ["/Latest%20Judgements.html"],
        config_extra={"result_page_size": 10, "result_max_pages": 1},
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_appellate_court_gb(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "SupremeAppellateCourtGB")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeAppellateCourtGB",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/Judgments/latest_judgements/Bad-Order.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert row.query_json["route"]["listing_fetch"] == "result_table"
    assert row.query_json["route"]["result_table_kind"] == "latest_judgements"
    assert row.query_json["route"]["result_row_serial"] == "1."
    assert row.query_json["route"]["result_case_no"] == "Cr. Misc No. 14/2022"
    assert row.query_json["route"]["result_title"] == "Shoaib Ahmed vs State"
    assert row.query_json["route"]["result_judgment_date"] == "29-09-2022"
    assert "missing %PDF signature" in (row.last_error or "")
