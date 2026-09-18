from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.ajk_supreme_court import normalize_ajk_supreme_public_url, scrape_ajk_supreme_court
from tests.fixtures import text_pdf_bytes


async def _ajk_sc_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "AJKSupremeCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {"listings": [fixture_server.url(path) for path in listings]}
    await db.commit()
    return source


def test_normalize_ajk_supreme_public_url_unwraps_wayback_and_host():
    wayback = "https://web.archive.org/web/20241005132304/https://www.ajksupremecourt.gok.pk/wp-content/uploads/2022/07/Test File.pdf"
    out = normalize_ajk_supreme_public_url(wayback, base_url="https://ajksupremecourt.gok.pk/category/judgments/")
    assert out == "https://ajksupremecourt.gok.pk/wp-content/uploads/2022/07/Test%20File.pdf"

    rel = normalize_ajk_supreme_public_url(
        "category/judgments/page/2/",
        base_url="https://ajksupremecourt.gok.pk/category/judgments/",
    )
    assert rel == "https://ajksupremecourt.gok.pk/category/judgments/page/2/"


async def test_ajk_supreme_discovery_from_listing_post_data_script_and_wayback(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    wayback_pdf = (
        "https://web.archive.org/web/20241005132304/"
        f"{fixture_server.url('/wp-content/uploads/2022/07/Wayback%20Appeal.pdf')}"
    )
    fixture_server.add(
        "/category/judgments/",
        f"""
        <html><body>
          <a href="/category/judgments/page/2/">Page 2</a>
          <a href="/fareeda-rafique-vs-azad-govt-others/">Fareeda Rafique vs. Azad Govt.</a>
          <div data-doc-url="wp-content/uploads/2022/07/Data Evidence.pdf">data-path</div>
          <script>
            const doc = "wp-content/uploads/2022/07/Script Order.pdf";
            const p3 = "/category/judgments/page/3/";
          </script>
          <a href="{wayback_pdf}">Wayback PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/category/judgments/page/2/",
        """
        <html><body>
          <a href="/malik-zaffar-vs-rashid-hussain-shah-others/">Malik Zaffar vs. Rashid Hussain Shah</a>
          <a href="/wp-content/uploads/2022/07/Page Two Case.pdf">Page 2 direct PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/category/judgments/page/3/",
        '<html><body><a href="/wp-content/uploads/2022/07/Page Three Order.pdf">page3</a></body></html>',
    )
    fixture_server.add(
        "/fareeda-rafique-vs-azad-govt-others/",
        '<html><body><a href="/wp-content/uploads/2022/07/Fareeda Rafique Judgment.pdf">Download PDF</a></body></html>',
    )
    fixture_server.add(
        "/malik-zaffar-vs-rashid-hussain-shah-others/",
        '<html><body><a href="/wp-content/uploads/2022/07/Malik Zaffar Order.pdf">Download Order</a></body></html>',
    )

    for idx, path in enumerate(
        (
            "/wp-content/uploads/2022/07/Data%20Evidence.pdf",
            "/wp-content/uploads/2022/07/Script%20Order.pdf",
            "/wp-content/uploads/2022/07/Wayback%20Appeal.pdf",
            "/wp-content/uploads/2022/07/Page%20Two%20Case.pdf",
            "/wp-content/uploads/2022/07/Page%20Three%20Order.pdf",
            "/wp-content/uploads/2022/07/Fareeda%20Rafique%20Judgment.pdf",
            "/wp-content/uploads/2022/07/Malik%20Zaffar%20Order.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 AJKSC {idx}\nSUPREME COURT OF AZAD JAMMU AND KASHMIR\nCivil Appeal No. {idx} of 2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _ajk_sc_source(db, fixture_server, ["/category/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_ajk_supreme_court(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["discovered"] >= 7
    hits = set(fixture_server.hits)
    assert "/category/judgments/page/2/" in hits
    assert "/category/judgments/page/3/" in hits
    assert "/wp-content/uploads/2022/07/Script%20Order.pdf" in hits
    assert "/wp-content/uploads/2022/07/Wayback%20Appeal.pdf" in hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "AJKSupremeCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/wp-content/uploads/2022/07/Script%%20Order.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/wp-content/uploads/2022/07/Fareeda%%20Rafique%%20Judgment.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKSupremeCourt",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/fareeda-rafique-vs-azad-govt-others/",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["meta"]["discovery_channel"] == "anchor"
    assert row.query_json["route"]["listing"] == f"http://127.0.0.1:{fixture_server.port}/category/judgments/"


async def test_ajk_supreme_pdf_signature_gate_retires_non_pdf_judgment_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/category/judgments/",
        '<html><body><a href="/wp-content/uploads/2022/07/Bad.pdf">bad</a></body></html>',
    )
    fixture_server.add(
        "/wp-content/uploads/2022/07/Bad.pdf",
        "<html><body>not pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _ajk_sc_source(db, fixture_server, ["/category/judgments/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_ajk_supreme_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "AJKSupremeCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKSupremeCourt",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2022/07/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
