from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.peshawar_high_court import normalize_phc_public_url, scrape_peshawar_high_court
from tests.fixtures import text_pdf_bytes


async def _phc_source(db, fixture_server, listings, *, search_posts=None):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PeshawarHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {
        "listings": [fixture_server.url(path) for path in listings],
    }
    if search_posts is not None:
        cfg["search_posts"] = search_posts
    source.config_json = cfg
    await db.commit()
    return source


def test_normalize_phc_public_url_unwraps_wayback_and_cleans_double_slashes():
    wayback = "https://web.archive.org/web/20241005132304/https://www.peshawarhighcourt.gov.pk/PHCCMS//judgments/WP Test.pdf"
    out = normalize_phc_public_url(wayback, base_url="https://www.peshawarhighcourt.gov.pk/PHCCMS/reportedJudgments.php")
    assert out == "https://www.peshawarhighcourt.gov.pk/PHCCMS/judgments/WP%20Test.pdf"

    rel = normalize_phc_public_url("../../../../PHCCMS/reportedJudgments.php?action=search", base_url="https://www.peshawarhighcourt.gov.pk/app/site/47/c/All_the_Referred,Reported_Judgments.html")
    assert rel == "https://www.peshawarhighcourt.gov.pk/PHCCMS/reportedJudgments.php?action=search"


async def test_phc_search_harvest_discovers_pdf_judgments_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/app/site/47/c/All_the_Referred,Reported_Judgments.html",
        """
        <html><body>
          <a href="../../../../PHCCMS/reportedJudgments.php">All the Referred, Reported Judgments</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/PHCCMS/reportedJudgments.php",
        """
        <html><body>
          <form action="/PHCCMS/reportedJudgments.php?action=search" method="post">
            <select name="year"><option value="0">All</option><option value="2026">2026</option></select>
            <select name="judge"><option value="0">All</option></select>
            <select name="category"><option value="0">All</option></select>
            <input name="txtsearchbyremarks" value="">
            <input name="submit" value="search">
          </form>
          <a href="/PHCCMS//judgments/Anchor-Top.pdf">Top anchor</a>
        </body></html>
        """,
    )
    fixture_server.add_post(
        "/PHCCMS/reportedJudgments.php?action=search",
        """
        <html><body>
          <table><tbody>
            <tr>
              <td>2026 PHC 1</td>
              <td>Criminal</td>
              <td><a target='_blank' href=/PHCCMS//judgments/Case-One.pdf><img src=/PHCCMS//images/PDF5.gif width=30></a></td>
            </tr>
            <tr>
              <td>2026 PHC 2</td>
              <td>Civil</td>
              <td><a target='_blank' href="/PHCCMS//judgments/Case-Two.pdf"><img src="/PHCCMS//images/PDF5.gif" width=30></a></td>
            </tr>
          </tbody></table>
          <script>const hiddenPdf = "https://127.0.0.1:1/not-allowed.pdf";</script>
        </body></html>
        """,
    )

    for path in ("/PHCCMS/judgments/Anchor-Top.pdf", "/PHCCMS/judgments/Case-One.pdf", "/PHCCMS/judgments/Case-Two.pdf"):
        fixture_server.add(
            path,
            text_pdf_bytes(
                "PLD 2024 PHC 1\nPESHAWAR HIGH COURT\nWrit Petition No. 1 of 2024\nDecided on 1st January 2024\nPetition dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _phc_source(
        db,
        fixture_server,
        ["/app/site/47/c/All_the_Referred,Reported_Judgments.html", "/PHCCMS/reportedJudgments.php"],
        search_posts=[{"year": "2026", "judge": "0", "category": "0", "txtsearchbyremarks": "", "submit": "search"}],
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_peshawar_high_court(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["discovered"] >= 3
    assert "POST /PHCCMS/reportedJudgments.php?action=search" in fixture_server.hits
    assert "/PHCCMS/judgments/Case-One.pdf" in fixture_server.hits
    assert "/PHCCMS/judgments/Anchor-Top.pdf" in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "PeshawarHighCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/PHCCMS/judgments/Case-One.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/PHCCMS/judgments/Case-Two.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PeshawarHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/PHCCMS/judgments/Case-One.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "post_form"
    assert row.query_json["route"]["search"] == {"year": "2026", "judge": "0", "category": "0"}
    assert row.query_json["route"]["search_endpoint"].endswith("/PHCCMS/reportedJudgments.php?action=search")


async def test_phc_pdf_signature_gate_retires_non_pdf_document_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/PHCCMS/reportedJudgments.php",
        """
        <html><body>
          <form action="/PHCCMS/reportedJudgments.php?action=search" method="post">
            <select name="year"><option value="2026">2026</option></select>
            <select name="judge"><option value="0">All</option></select>
            <select name="category"><option value="0">All</option></select>
            <input name="submit" value="search">
          </form>
        </body></html>
        """,
    )
    fixture_server.add_post(
        "/PHCCMS/reportedJudgments.php?action=search",
        """
        <html><body>
          <table><tbody>
            <tr>
              <td>bad</td>
              <td><a target='_blank' href=/PHCCMS//judgments/Bad.pdf><img src=/PHCCMS//images/PDF5.gif width=30></a></td>
            </tr>
          </tbody></table>
        </body></html>
        """,
    )
    fixture_server.add("/PHCCMS/judgments/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _phc_source(
        db,
        fixture_server,
        ["/PHCCMS/reportedJudgments.php"],
        search_posts=[{"year": "2026", "judge": "0", "category": "0", "submit": "search"}],
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_peshawar_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(ScraperStaging)
            .where(ScraperStaging.source_name == "PeshawarHighCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PeshawarHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/PHCCMS/judgments/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
