from __future__ import annotations

import json

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.supreme_court import normalize_supreme_court_public_url, scrape_supreme_court
from tests.fixtures import text_pdf_bytes


async def _supreme_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SupremeCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "search_posts": [{"case_type": "C.A.", "case_year": "2025", "reported": "yes"}],
        "search_page_fields": ["page"],
        "search_pages": [1, 2],
    }
    await db.commit()
    return source


def test_normalize_supreme_court_public_url_unwraps_and_normalizes():
    wayback = "https://web.archive.org/web/20241005132304/https://supremecourt.gov.pk/downloads_judgements/Test File.pdf"
    out = normalize_supreme_court_public_url(wayback, base_url="https://www.supremecourt.gov.pk/judgement-search/")
    assert out == "https://www.supremecourt.gov.pk/downloads_judgements/Test%20File.pdf"

    file_only = normalize_supreme_court_public_url("Another File.pdf", base_url="https://www.supremecourt.gov.pk/judgement-search/")
    assert file_only == "https://www.supremecourt.gov.pk/downloads_judgements/Another%20File.pdf"


async def test_supreme_court_post_json_discovery_and_pagination_harvest(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/judgement-search/",
        """
        <html><body>
          <a href="/judgements/page/2/">Older judgments</a>
          <div data-doc-url="downloads_judgements/Data Evidence.pdf">data-url</div>
          <script>
            function getRecord() {
              jQuery.ajax({url:'/wp-content/plugins/my-plugin/online_judgments.php',type:'POST'});
            }
            const scriptDoc = "/downloads_judgements/Script Order.pdf";
          </script>
        </body></html>
        """,
    )
    fixture_server.add(
        "/judgements/page/2/",
        '<html><body><a href="/downloads_judgements/Page Two.pdf">page2</a></body></html>',
    )

    post_json = [
        {"caseFileName": "Posted One.pdf", "fileSizeInBytes": 123},
        {"download_url": "downloads_judgements/Posted Two.pdf"},
        {"nested": {"url": fixture_server.url("/downloads_judgements/Posted%20Three.pdf")}},
        {"listing": "/judgements/page/2/"},
    ]
    fixture_server.add_post(
        "/wp-content/plugins/my-plugin/online_judgments.php",
        json.dumps(post_json),
        content_type="application/json",
    )

    for idx, path in enumerate(
        (
            "/downloads_judgements/Data%20Evidence.pdf",
            "/downloads_judgements/Script%20Order.pdf",
            "/downloads_judgements/Posted%20One.pdf",
            "/downloads_judgements/Posted%20Two.pdf",
            "/downloads_judgements/Posted%20Three.pdf",
            "/downloads_judgements/Page%20Two.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 SC {idx}\nSUPREME COURT OF PAKISTAN\nCivil Appeal No. {idx} of 2024\nDecided on 1st January 2024\nAppeal dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _supreme_source(db, fixture_server, ["/judgement-search/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_court(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["discovered"] >= 6
    assert sum(1 for h in fixture_server.hits if h == "POST /wp-content/plugins/my-plugin/online_judgments.php") >= 2
    hits = set(fixture_server.hits)
    assert "/judgements/page/2/" in hits
    assert "/downloads_judgements/Posted%20One.pdf" in hits
    assert "/downloads_judgements/Page%20Two.pdf" in hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "SupremeCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/downloads_judgements/Posted%%20One.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/downloads_judgements/Page%%20Two.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeCourt",
                CrawlFrontier.query_key
                == f"judgment:http://127.0.0.1:{fixture_server.port}/downloads_judgements/Posted%20One.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["meta"]["discovery_channel"] == "post-json"
    assert row.query_json["route"]["listing_fetch"] == "post_form"
    assert row.query_json["route"]["search_page"] == 1
    assert row.query_json["route"]["search_endpoint"].endswith("/wp-content/plugins/my-plugin/online_judgments.php")


async def test_supreme_court_pdf_signature_gate_retires_non_pdf_post_result(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/judgement-search/",
        """
        <html><body>
          <script>
            function getRecord() {
              jQuery.ajax({url:'/wp-content/plugins/my-plugin/online_judgments.php',type:'POST'});
            }
          </script>
        </body></html>
        """,
    )
    fixture_server.add_post(
        "/wp-content/plugins/my-plugin/online_judgments.php",
        json.dumps([{"caseFileName": "Bad.pdf", "fileSizeInBytes": 123}]),
        content_type="application/json",
    )
    fixture_server.add(
        "/downloads_judgements/Bad.pdf",
        "<html><body>not pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _supreme_source(db, fixture_server, ["/judgement-search/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_supreme_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "SupremeCourt"))).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SupremeCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/downloads_judgements/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
