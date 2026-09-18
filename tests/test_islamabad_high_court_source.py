from __future__ import annotations

import json

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.islamabad_high_court import normalize_ihc_public_url, scrape_islamabad_high_court
from tests.fixtures import text_pdf_bytes


async def _ihc_source(db, fixture_server, listings, *, search_posts=None, latest_posts=None):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "IslamabadHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {
        "listings": [fixture_server.url(path) for path in listings],
        "search_result_page_size": 2,
        "search_result_max_pages": 2,
    }
    if search_posts is not None:
        cfg["search_posts"] = search_posts
    if latest_posts is not None:
        cfg["latest_posts"] = latest_posts
    source.config_json = cfg
    await db.commit()
    return source


def test_normalize_ihc_public_url_unwraps_wayback_and_attachment_relative():
    wayback = "https://web.archive.org/web/20241005132304/https://mis.ihc.gov.pk/attachments/judgements/101/1/Test File.pdf"
    out = normalize_ihc_public_url(wayback, base_url="https://mis.ihc.gov.pk/frmJgmnt.aspx?jgs=1")
    assert out == "https://mis.ihc.gov.pk/attachments/judgements/101/1/Test%20File.pdf"

    rel = normalize_ihc_public_url("attachments/judgements/101/1/Legacy Order.pdf", base_url="https://mis.ihc.gov.pk/frmJgmnt.aspx?jgs=1")
    assert rel == "https://mis.ihc.gov.pk/attachments/judgements/101/1/Legacy%20Order.pdf"


async def test_ihc_ajax_harvest_discovers_pdf_judgments_with_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    listing_html = """
        <html><body>
          <a href="/attachments/judgements/300/1/Anchor Evidence.pdf">Anchor</a>
          <div data-doc-url="/attachments/judgements/301/1/Data Evidence.pdf">data path</div>
          <script>
            const scriptDoc = "/attachments/judgements/302/1/Script Order.pdf";
            var webMethod = "ihc.asmx/GetLatestJgmntsNew";
            var webMethod2 = "ihc.asmx/srchDecisionClms";
          </script>
        </body></html>
    """
    fixture_server.add("/frmJgmnt.aspx?jgs=1", listing_html)
    fixture_server.add("/frmJgmnt.aspx?jgs=0", listing_html)

    latest_rows = [
        {
            "PARTIES": "Alpha vs Beta",
            "DDATE": "10-SEP-2026",
            "ATTACHMENTS": "/attachments/judgements/200/1/Latest One.pdf",
            "O_CITATION": "2026 IHC 1",
            "CASENO": "Writ Petition-1-2026",
            "BENCHNAME": "Bench One",
        },
        {
            "PARTIES": "Gamma vs Delta",
            "DDATE": "11-SEP-2026",
            "ATTACHMENTS": "/attachments/judgements/201/1/Latest Two.pdf",
            "O_CITATION": "2026 IHC 2",
            "CASENO": "Writ Petition-2-2026",
            "BENCHNAME": "Bench Two",
        },
    ]
    search_rows = [
        {
            "PARTIES": "Search Case One",
            "DDATE": "12-SEP-2026",
            "ATTACHMENTS": "/attachments/judgements/210/1/Search One.pdf",
            "CASENO": "Civil Appeal-5-2026",
            "BENCHNAME": "Bench Three",
            "O_CITATION": "2026 IHC 3",
        },
        {
            "PARTIES": "Search Case Two",
            "DDATE": "13-SEP-2026",
            "ATTACHMENTS": "/attachments/judgements/211/1/Search Two.pdf",
            "CASENO": "Civil Appeal-6-2026",
            "BENCHNAME": "Bench Four",
            "O_CITATION": "2026 IHC 4",
        },
        {
            "PARTIES": "Search Case Three",
            "DDATE": "14-SEP-2026",
            "ATTACHMENTS": "/attachments/judgements/212/1/Search Three.pdf",
            "CASENO": "Civil Appeal-7-2026",
            "BENCHNAME": "Bench Five",
            "O_CITATION": "2026 IHC 5",
        },
    ]
    fixture_server.add_post("/ihc.asmx/GetLatestJgmntsNew", json.dumps({"d": json.dumps(latest_rows)}), content_type="application/json")
    fixture_server.add_post("/ihc.asmx/srchDecisionClms", json.dumps({"d": json.dumps(search_rows)}), content_type="application/json")

    for idx, path in enumerate(
        (
            "/attachments/judgements/200/1/Latest%20One.pdf",
            "/attachments/judgements/201/1/Latest%20Two.pdf",
            "/attachments/judgements/210/1/Search%20One.pdf",
            "/attachments/judgements/211/1/Search%20Two.pdf",
            "/attachments/judgements/212/1/Search%20Three.pdf",
            "/attachments/judgements/300/1/Anchor%20Evidence.pdf",
            "/attachments/judgements/301/1/Data%20Evidence.pdf",
            "/attachments/judgements/302/1/Script%20Order.pdf",
        ),
        start=1,
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                f"PLD 2024 IHC {idx}\nISLAMABAD HIGH COURT\nWrit Petition No. {idx} of 2024\nDecided on 1st January 2024\nPetition dismissed."
            ),
            content_type="application/pdf",
        )

    source = await _ihc_source(
        db,
        fixture_server,
        ["/frmJgmnt.aspx?jgs=1", "/frmJgmnt.aspx?jgs=0"],
        search_posts=[{"PCASENO": "0", "PJUG": "0", "PADV": "0", "PYEAR": "0", "pPrty": "", "PDDATE": "01/01/1900", "PLANDMARK": "1", "PAFR": "0"}],
        latest_posts=[{}],
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_islamabad_high_court(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 8
    assert "POST /ihc.asmx/GetLatestJgmntsNew" in fixture_server.hits
    assert "POST /ihc.asmx/srchDecisionClms" in fixture_server.hits
    assert "/attachments/judgements/210/1/Search%20One.pdf" in fixture_server.hits
    assert "/attachments/judgements/302/1/Script%20Order.pdf" in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "IslamabadHighCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/attachments/judgements/200/1/Latest%%20One.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/attachments/judgements/210/1/Search%%20One.pdf" % fixture_server.port in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "IslamabadHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/attachments/judgements/210/1/Search%20One.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "post_json"
    assert row.query_json["route"]["search_source"] == "search"
    assert row.query_json["route"]["search_result_page"] == 1
    assert row.query_json["route"]["search_endpoint"].endswith("/ihc.asmx/srchDecisionClms")


async def test_ihc_pdf_signature_gate_retires_non_pdf_ajax_document_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/frmJgmnt.aspx?jgs=1",
        """
        <html><body>
          <script>
            var webMethod = "ihc.asmx/GetLatestJgmntsNew";
          </script>
        </body></html>
        """,
    )
    fixture_server.add_post(
        "/ihc.asmx/GetLatestJgmntsNew",
        json.dumps(
            {
                "d": json.dumps(
                    [
                        {
                            "PARTIES": "Broken PDF",
                            "ATTACHMENTS": "/attachments/judgements/999/1/Bad.pdf",
                            "CASENO": "Writ Petition-99-2026",
                        }
                    ]
                )
            }
        ),
        content_type="application/json",
    )
    fixture_server.add(
        "/attachments/judgements/999/1/Bad.pdf",
        "<html><body>not pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _ihc_source(db, fixture_server, ["/frmJgmnt.aspx?jgs=1"], latest_posts=[{}], search_posts=[])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_islamabad_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count()).select_from(ScraperStaging).where(ScraperStaging.source_name == "IslamabadHighCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "IslamabadHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/attachments/judgements/999/1/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
