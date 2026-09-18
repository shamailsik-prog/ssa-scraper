from __future__ import annotations

import json

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.balochistan_high_court import normalize_bhc_public_url, scrape_balochistan_high_court
from tests.fixtures import text_pdf_bytes


async def _bhc_source(db, fixture_server, listings, *, config_extra=None):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "BalochistanHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    cfg = {
        "listings": [fixture_server.url(path) for path in listings],
    }
    if config_extra:
        cfg.update(config_extra)
    source.config_json = cfg
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

    rel_portal_download = normalize_bhc_public_url(
        "/v2/downloadpdf/2024/sample_file.doc",
        base_url="https://api.bhc.gov.pk/v2/judgments",
    )
    assert rel_portal_download == "https://api.bhc.gov.pk/v2/downloadpdf/2024/sample_file.doc"


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


async def test_bhc_portal_api_fanout_discovers_downloadpdf_judgments_with_route(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/judgments",
        """
        <html><body>
          <div id="__nuxt"></div>
          <link rel="preload" href="/_nuxt/app.abc123-def.js" as="script">
        </body></html>
        """,
    )
    fixture_server.add(
        "/_nuxt/app.abc123-def.js",
        'window.__STORE__={guestAuthData:{email:"guest@bhc.gov.pk",password:"public-guest-password"}};',
        content_type="application/javascript",
    )
    fixture_server.add_post(
        "/login",
        json.dumps({"access_token": "token-123"}),
        content_type="application/json",
    )
    fixture_server.add_post(
        "/v2/judges",
        json.dumps(
            [
                {"JUDGE_ID": 1001, "JUDGE_NAME": "Judge One", "STATUS": 1, "TOTAL_ORDERS": 120},
                {"JUDGE_ID": 1002, "JUDGE_NAME": "Judge Two", "STATUS": 1, "TOTAL_ORDERS": 80},
            ]
        ),
        content_type="application/json",
    )
    fixture_server.add_post(
        "/v2/judgments",
        json.dumps(
            [
                {
                    "FILE_FOLDER": "2024",
                    "FILE_NAME": "1001_case_file",
                    "FILE_EXT": "doc",
                    "CASE_ID": 1001001,
                    "REGISTER_NUMBER": "CP-1/2024",
                    "ORDER_DATE": "14/09/2024",
                    "AUTHOR_JUDGE": "Judge One",
                    "TYPE_NAME": "Final Judgment",
                }
            ]
        ),
        content_type="application/json",
    )
    fixture_server.add(
        "/v2/downloadpdf/2024/1001_case_file.doc",
        text_pdf_bytes(
            "PLD 2024 Balochistan 101\nBalochistan High Court\nConstitution Petition No. 1 of 2024\nDecided on 14th September 2024\nPetition dismissed."
        ),
        content_type="application/pdf",
    )

    source = await _bhc_source(
        db,
        fixture_server,
        ["/judgments"],
        config_extra={
            "portal_login_endpoint": fixture_server.url("/login"),
            "portal_judges_endpoint": fixture_server.url("/v2/judges"),
            "portal_judgments_endpoint": fixture_server.url("/v2/judgments"),
            "portal_judge_max": 2,
            "portal_years": [2024, 2023],
            "portal_result_page_size": 10,
            "portal_result_max_pages": 2,
        },
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["discovered"] >= 1
    assert fixture_server.hits.count("POST /v2/judgments") == 4
    assert "/v2/downloadpdf/2024/1001_case_file.doc" in fixture_server.hits

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/v2/downloadpdf/2024/1001_case_file.doc",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "portal_post_json"
    assert row.query_json["route"]["search_source"] == "portal_judge_year"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "portal-downloadpdf"
    assert row.query_json["route"]["search_result_page"] == 1


async def test_bhc_portal_downloadpdf_signature_gate_retires_non_pdf_content(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/judgments",
        """
        <html><body>
          <div id="__nuxt"></div>
          <script src="/_nuxt/app.js"></script>
        </body></html>
        """,
    )
    fixture_server.add(
        "/_nuxt/app.js",
        'window.__STORE__={guestAuthData:{email:"guest@bhc.gov.pk",password:"public-guest-password"}};',
        content_type="application/javascript",
    )
    fixture_server.add_post("/login", json.dumps({"access_token": "token-xyz"}), content_type="application/json")
    fixture_server.add_post(
        "/v2/judges",
        json.dumps([{"JUDGE_ID": 1001, "JUDGE_NAME": "Judge One", "STATUS": 1, "TOTAL_ORDERS": 120}]),
        content_type="application/json",
    )
    fixture_server.add_post(
        "/v2/judgments",
        json.dumps(
            [
                {
                    "FILE_FOLDER": "2024",
                    "FILE_NAME": "bad_case_file",
                    "FILE_EXT": "doc",
                    "CASE_ID": 1001002,
                    "REGISTER_NUMBER": "CP-2/2024",
                    "ORDER_DATE": "15/09/2024",
                }
            ]
        ),
        content_type="application/json",
    )
    fixture_server.add(
        "/v2/downloadpdf/2024/bad_case_file.doc",
        "<html><body>not a pdf</body></html>",
        content_type="application/pdf",
    )

    source = await _bhc_source(
        db,
        fixture_server,
        ["/judgments"],
        config_extra={
            "portal_login_endpoint": fixture_server.url("/login"),
            "portal_judges_endpoint": fixture_server.url("/v2/judges"),
            "portal_judgments_endpoint": fixture_server.url("/v2/judgments"),
            "portal_judge_max": 1,
            "portal_years": [2024],
            "portal_result_page_size": 10,
            "portal_result_max_pages": 1,
        },
    )
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=40)
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
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/v2/downloadpdf/2024/bad_case_file.doc",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert row.query_json["route"]["pdf_endpoint_kind"] == "portal-downloadpdf"
    assert "missing %PDF signature" in (row.last_error or "")


async def test_bhc_portal_rerun_is_idempotent_without_duplicate_frontier_keys(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/judgments",
        """
        <html><body>
          <div id="__nuxt"></div>
          <script src="/_nuxt/app.js"></script>
        </body></html>
        """,
    )
    fixture_server.add(
        "/_nuxt/app.js",
        'window.__STORE__={guestAuthData:{email:"guest@bhc.gov.pk",password:"public-guest-password"}};',
        content_type="application/javascript",
    )
    fixture_server.add_post("/login", json.dumps({"access_token": "token-rerun"}), content_type="application/json")
    fixture_server.add_post(
        "/v2/judges",
        json.dumps([{"JUDGE_ID": 1001, "JUDGE_NAME": "Judge One", "STATUS": 1, "TOTAL_ORDERS": 120}]),
        content_type="application/json",
    )
    fixture_server.add_post(
        "/v2/judgments",
        json.dumps(
            [
                {
                    "FILE_FOLDER": "2024",
                    "FILE_NAME": "idempotent_case_file",
                    "FILE_EXT": "doc",
                    "CASE_ID": 1001003,
                    "REGISTER_NUMBER": "CP-3/2024",
                    "ORDER_DATE": "16/09/2024",
                }
            ]
        ),
        content_type="application/json",
    )
    fixture_server.add(
        "/v2/downloadpdf/2024/idempotent_case_file.doc",
        text_pdf_bytes(
            "PLD 2024 Balochistan 102\nBalochistan High Court\nConstitution Petition No. 3 of 2024\nDecided on 16th September 2024\nPetition dismissed."
        ),
        content_type="application/pdf",
    )

    source = await _bhc_source(
        db,
        fixture_server,
        ["/judgments"],
        config_extra={
            "portal_login_endpoint": fixture_server.url("/login"),
            "portal_judges_endpoint": fixture_server.url("/v2/judges"),
            "portal_judgments_endpoint": fixture_server.url("/v2/judgments"),
            "portal_judge_max": 1,
            "portal_years": [2024],
            "portal_result_page_size": 10,
            "portal_result_max_pages": 1,
        },
    )

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats_first = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats_second = await scrape_balochistan_high_court(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats_first["discovered"] >= 1
    assert stats_second["discovered"] == 0
    key = f"judgment:http://127.0.0.1:{fixture_server.port}/v2/downloadpdf/2024/idempotent_case_file.doc"
    frontier_count = (
        await db.execute(
            select(func.count()).select_from(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanHighCourt",
                CrawlFrontier.query_key == key,
            )
        )
    ).scalar()
    assert frontier_count == 1
    assert fixture_server.hits.count("/v2/downloadpdf/2024/idempotent_case_file.doc") == 1
