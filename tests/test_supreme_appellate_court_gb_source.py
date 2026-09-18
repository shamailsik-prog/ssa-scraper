from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.supreme_appellate_court_gb import (
    normalize_sacgb_public_url,
    scrape_supreme_appellate_court_gb,
)
from tests.fixtures import text_pdf_bytes


async def _sacgb_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(ScraperSource.source_name == "SupremeAppellateCourtGB")
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {"listings": [fixture_server.url(path) for path in listings]}
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
