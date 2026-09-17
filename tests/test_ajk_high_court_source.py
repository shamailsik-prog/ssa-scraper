from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.ajk_high_court import normalize_ajk_public_url, scrape_ajk_high_court
from tests.fixtures import text_pdf_bytes


async def _ajk_source(db, fixture_server, listings, search_posts=None):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "AJKHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {"listings": [fixture_server.url(path) for path in listings]}
    if search_posts is not None:
        cfg["search_posts"] = search_posts
    source.config_json = cfg
    await db.commit()
    return source


def test_normalize_ajk_public_url_unwraps_wayback_and_host():
    wayback = "https://web.archive.org/web/20241005132304/https://www.ajkhighcourt.gok.pk/judgment_files/Test File.pdf"
    out = normalize_ajk_public_url(wayback, base_url="https://ajkhighcourt.gok.pk/important-judgments")
    assert out == "https://ajkhighcourt.gok.pk/judgment_files/Test%20File.pdf"

    rel = normalize_ajk_public_url("judgment_files/Legacy Order.pdf", base_url="https://ajkhighcourt.gok.pk/important-judgments")
    assert rel == "https://ajkhighcourt.gok.pk/judgment_files/Legacy%20Order.pdf"


async def test_ajk_discovery_from_anchor_data_script_and_search_posts(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    listing_html = """
        <html><body>
          <form id="FmCs" method="post" action="">
            <select name="cmbYear"><option value="ALL">ALL</option></select>
            <select name="cmbBench"><option value="ALL">ALL</option></select>
            <select name="cmbCategory"><option value="ALL">ALL</option></select>
          </form>
          <a href="/important-judgments?judgment_tab=previous">Previous tab</a>
          <a href="judgment_files/Anchor Appeal.pdf">Anchor file</a>
          <div data-doc-url="judgment_files/Data Evidence.pdf">data-path</div>
          <script>
            const doc = "judgment_files/Script Order.pdf";
            const old = "important-judgments?judgment_tab=previous";
          </script>
        </body></html>
        """
    listing_previous_html = listing_html.replace(
        "</body></html>",
        '<a href="judgment_files/Prev Anchor.pdf">Previous anchor</a></body></html>',
    )
    fixture_server.add(
        "/important-judgments?seed=judgment",
        listing_html,
    )
    fixture_server.add(
        "/important-judgments?seed=previous",
        listing_previous_html,
    )
    fixture_server.add_post(
        "/important-judgments?seed=judgment",
        '<html><body><a href="judgment_files/Post Current.pdf">post current</a></body></html>',
    )
    fixture_server.add_post(
        "/important-judgments?seed=previous",
        '<html><body><a href="judgment_files/Post Previous.pdf">post previous</a></body></html>',
    )
    for idx, path in enumerate(
        (
            "/judgment_files/Anchor%20Appeal.pdf",
            "/judgment_files/Data%20Evidence.pdf",
            "/judgment_files/Script%20Order.pdf",
            "/judgment_files/Prev%20Anchor.pdf",
            "/judgment_files/Post%20Current.pdf",
            "/judgment_files/Post%20Previous.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 AJK {idx}\nHIGH COURT OF AZAD JAMMU AND KASHMIR\nWrit Petition No. {idx} of 2024\nDecided on 1st January 2024\nPetition dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _ajk_source(
        db,
        fixture_server,
        ["/important-judgments?seed=judgment", "/important-judgments?seed=previous"],
        search_posts=[
            {"judgment_tab": "judgment", "cmbYear": "ALL", "cmbBench": "ALL", "cmbCategory": "ALL", "btnSearchJudgment": "Search"},
            {"judgment_tab": "previous", "cmbYear": "ALL", "cmbBench": "ALL", "cmbCategory": "ALL", "btnSearchJudgment": "Search"},
        ],
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_ajk_high_court(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["discovered"] >= 6
    assert "POST /important-judgments?seed=judgment" in fixture_server.hits
    assert "POST /important-judgments?seed=previous" in fixture_server.hits
    assert "/judgment_files/Post%20Current.pdf" in fixture_server.hits
    assert "/judgment_files/Post%20Previous.pdf" in fixture_server.hits

    pdf_rows = (
        await db.execute(select(SourceProvenance).where(SourceProvenance.source_name == "AJKHighCourt", SourceProvenance.content_kind == "pdf"))
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/judgment_files/Post%%20Current.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/judgment_files/Post%%20Previous.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/judgment_files/Post%20Current.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "post_form"
    assert row.query_json["route"]["search"]["judgment_tab"] == "judgment"


async def test_ajk_pdf_signature_gate_retires_non_pdf_judgment_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/important-judgments", '<html><body><a href="judgment_files/Bad.pdf">bad</a></body></html>')
    fixture_server.add("/judgment_files/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _ajk_source(db, fixture_server, ["/important-judgments"], search_posts=[])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_ajk_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (await db.execute(select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "AJKHighCourt"))).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/judgment_files/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
