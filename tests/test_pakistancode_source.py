from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.pakistancode import (
    REJECTED_URL_SAMPLE_LIMIT,
    PakistanCodePipeline,
    _classify_discovered_url,
    _effective_crawl_limit,
    document_query_key,
    normalize_pakistancode_public_url,
    scrape_pakistancode,
)
from tests.fixtures import text_pdf_bytes

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "pakistancode"
DEFENSE_ACT_PATH = "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj"
ORDINANCE_PATH = "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaQ%3D%3D-sg-jjjjjjjjjjjjj"


async def _pakistancode_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "PakistanCode",
            )
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 2
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
    }
    await db.commit()
    return source


def test_normalize_pakistancode_public_url_handles_alpha_alias_routes():
    rel = normalize_pakistancode_public_url(
        "https://www.pakistancode.gov.pk/alpha/LGu0xAD?alp=A&page=1&action=inactive",
        base_url="https://pakistancode.gov.pk/english/index.php",
    )
    assert rel == "https://pakistancode.gov.pk/english/LGu0xAD?alp=A&page=1&action=inactive"


async def test_pakistancode_listing_and_detail_route_provenance_for_statutes_and_ordinances(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/english/LGu0xBD.php",
        """
        <html><body>
          <div class="tab-pane fade show active" id="primary-legislation">
            <div class="accordion">
              <div class="accordion-section">
                <div class='accordion-section-title' data-tab='#accordion-p-1' id='accordionsss'>
                  <a href="UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj"><strong>11.</strong> Defense Forces of Pakistan Act, 2026</a>
                </div>
                <div class='accordion-section-content' id='accordion-p-1'>
                  General Laws | <font size="3">XLVII of 2026</font> | <font size="3">Promulgation Date: August 20 2026.</font>
                </div>
              </div>
            </div>
          </div>

          <div class="tab-pane fade" id="ordinance">
            <div class="accordion">
              <div class="accordion-section">
                <div class='accordion-section-title' data-tab='#accordion-p-2' id='accordionsss'>
                  <a href="UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj"><strong>12.</strong> Defence Industrial Production and Regulatory Authority of Pakistan Ordinance, 2026</a>
                </div>
                <div class='accordion-section-content' id='accordion-p-2'>
                  General Laws | <font size="3">IV of 2026</font> | <font size="3">Promulgation Date: June 03 2026.</font>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj",
        """
        <html><body>
          <h2>Defense Forces of Pakistan Act, 2026</h2>
          <div class="tab-pane fade" id="download">
            <a href="/pdffiles/administrator-act-2026.pdf">Download PDF</a>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj",
        """
        <html><body>
          <h2>Defence Industrial Production and Regulatory Authority of Pakistan Ordinance, 2026</h2>
          <div class="tab-pane fade show active" id="pdf">
            <iframe src="/ViewerJS/#../pdffiles/administrator-ord-2026.pdf" title="Full Law"></iframe>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/pdffiles/administrator-act-2026.pdf",
        text_pdf_bytes("Defense Forces of Pakistan Act, 2026"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/pdffiles/administrator-ord-2026.pdf",
        text_pdf_bytes("Defence Industrial Production and Regulatory Authority of Pakistan Ordinance, 2026"),
        content_type="application/pdf",
    )

    source = await _pakistancode_source(db, fixture_server, ["/english/LGu0xBD.php"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_pakistancode(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["halted"] is False
    assert "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj" in fixture_server.hits
    assert "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj" in fixture_server.hits
    assert "/pdffiles/administrator-act-2026.pdf" in fixture_server.hits
    assert "/pdffiles/administrator-ord-2026.pdf" in fixture_server.hits

    statute_detail_url = f"http://127.0.0.1:{fixture_server.port}/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj"
    detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == f"listing:{statute_detail_url}",
            )
        )
    ).scalars().first()
    assert detail_row is not None
    assert detail_row.priority == 80
    assert detail_row.query_json["meta"]["listing_fetch"] == "chronological_accordion"
    assert detail_row.query_json["meta"]["source_section"] == "federal_laws"
    assert detail_row.query_json["meta"]["act_no"] == "XLVII of 2026"
    assert detail_row.query_json["meta"]["act_year"] == "2026"
    assert detail_row.query_json["target_kind"] == "statute"

    statute_pdf_url = f"http://127.0.0.1:{fixture_server.port}/pdffiles/administrator-act-2026.pdf"
    statute_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == document_query_key(
                    "statute",
                    statute_pdf_url,
                    {"detail_url": statute_detail_url},
                ),
            )
        )
    ).scalars().first()
    assert statute_doc_row is not None
    assert statute_doc_row.priority == 10
    assert statute_doc_row.query_json["expect_pdf"] is True
    assert statute_doc_row.query_json["route"]["pdf_endpoint_kind"] == "pdffiles-direct"
    assert statute_doc_row.query_json["route"]["detail_fetch"] == "download_tab_link"
    assert statute_doc_row.query_json["route"]["detail_url"].endswith("tqaw%3D%3D-sg-jjjjjjjjjjjjj")

    ord_pdf_url = f"http://127.0.0.1:{fixture_server.port}/pdffiles/administrator-ord-2026.pdf"
    ord_detail_url = f"http://127.0.0.1:{fixture_server.port}/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj"
    ord_doc_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == document_query_key(
                    "instrument",
                    ord_pdf_url,
                    {"detail_url": ord_detail_url},
                ),
            )
        )
    ).scalars().first()
    assert ord_doc_row is not None
    assert ord_doc_row.priority == 10
    assert ord_doc_row.query_json["expect_pdf"] is True
    assert ord_doc_row.query_json["route"]["source_section"] == "ordinances"
    assert ord_doc_row.query_json["route"]["pdf_endpoint_kind"] == "viewerjs-pdf-embed"
    assert ord_doc_row.query_json["route"]["detail_fetch"] == "viewer_iframe"
    assert ord_doc_row.query_json["route"]["act_type"] == "ordinance"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "PakistanCode",
                SourceProvenance.source_url == ord_pdf_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["detail_url"].endswith("tpbw%3D%3D-sg-jjjjjjjjjjjjj")


async def test_pakistancode_pdf_signature_gate_retires_non_pdf_candidate(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/english/LGu0xBD.php",
        """
        <html><body>
          <div class="tab-pane fade show active" id="ordinance">
            <div class="accordion">
              <div class="accordion-section">
                <div class='accordion-section-title' data-tab='#accordion-p-2' id='accordionsss'>
                  <a href="UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj">Defence Industrial Production and Regulatory Authority of Pakistan Ordinance, 2026</a>
                </div>
                <div class='accordion-section-content' id='accordion-p-2'>
                  General Laws | <font size="3">IV of 2026</font> | <font size="3">Promulgation Date: June 03 2026.</font>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj",
        """
        <html><body>
          <h2>Defence Industrial Production and Regulatory Authority of Pakistan Ordinance, 2026</h2>
          <a href="/pdffiles/fake-ord-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add("/pdffiles/fake-ord-2026.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _pakistancode_source(db, fixture_server, ["/english/LGu0xBD.php"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_pakistancode(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "PakistanCode")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/pdffiles/fake-ord-2026.pdf"
    document_detail_url = f"http://127.0.0.1:{fixture_server.port}/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tpbw%3D%3D-sg-jjjjjjjjjjjjj"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == document_query_key(
                    "instrument",
                    document_url,
                    {"detail_url": document_detail_url},
                ),
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


async def test_pakistancode_effective_limit_enforces_floor_and_respects_explicit_limit(db, fixture_server):
    source = await _pakistancode_source(db, fixture_server, ["/english/LGu0xBD.php"])
    source.crawl_max_pages = 120
    assert _effective_crawl_limit(source, None) == 200

    source.crawl_max_pages = 260
    assert _effective_crawl_limit(source, None) == 260

    source.config_json = {**(source.config_json or {}), "crawl_max_pages": 310}
    assert _effective_crawl_limit(source, None) == 310
    assert _effective_crawl_limit(source, 75) == 75


def test_document_query_key_keeps_shared_pdfs_distinct():
    pdf = "https://pakistancode.gov.pk/pdffiles/shared.pdf"
    alpha = document_query_key("statute", pdf, {"detail_url": "https://pakistancode.gov.pk/english/alpha"})
    beta = document_query_key("statute", pdf, {"detail_url": "https://pakistancode.gov.pk/english/beta"})
    assert alpha != beta
    assert alpha.startswith("statute:https://pakistancode.gov.pk/pdffiles/shared.pdf|")
    assert document_query_key("statute", pdf, {}) == f"statute:{pdf}"


async def test_pakistancode_shared_pdf_keeps_distinct_act_frontier_and_staging(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/english/LGu0xBD.php",
        """
        <html><body>
          <div class="tab-pane fade show active" id="primary-legislation">
            <div class="accordion">
              <div class="accordion-section">
                <div class='accordion-section-title'><a href="UY2FqaJw1-alpha-detail"><strong>1.</strong> Alpha Fisheries Act, 2026</a></div>
                <div class='accordion-section-content'>General Laws | <font size="3">I of 2026</font></div>
              </div>
              <div class="accordion-section">
                <div class='accordion-section-title'><a href="UY2FqaJw1-beta-detail"><strong>2.</strong> Beta Forestry Act, 2026</a></div>
                <div class='accordion-section-content'>General Laws | <font size="3">II of 2026</font></div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/english/UY2FqaJw1-alpha-detail",
        """
        <html><body>
          <h2>Alpha Fisheries Act, 2026</h2>
          <a href="/pdffiles/shared-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/english/UY2FqaJw1-beta-detail",
        """
        <html><body>
          <h2>Beta Forestry Act, 2026</h2>
          <a href="/pdffiles/shared-2026.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/pdffiles/shared-2026.pdf",
        text_pdf_bytes(
            "Alpha Fisheries Act, 2026\n"
            "Beta Forestry Act, 2026\n"
            "1. Short title.- This Act shall be called the Shared Compilation Act and extends throughout Pakistan.\n"
            "2. Definitions.- In this Act, unless the context otherwise requires, terms have assigned meanings.\n"
        ),
        content_type="application/pdf",
    )

    source = await _pakistancode_source(db, fixture_server, ["/english/LGu0xBD.php"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_pakistancode(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["halted"] is False
    pdf_url = f"http://127.0.0.1:{fixture_server.port}/pdffiles/shared-2026.pdf"
    alpha_detail = f"http://127.0.0.1:{fixture_server.port}/english/UY2FqaJw1-alpha-detail"
    beta_detail = f"http://127.0.0.1:{fixture_server.port}/english/UY2FqaJw1-beta-detail"

    alpha_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == document_query_key("statute", pdf_url, {"detail_url": alpha_detail}),
            )
        )
    ).scalars().first()
    beta_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == document_query_key("statute", pdf_url, {"detail_url": beta_detail}),
            )
        )
    ).scalars().first()
    assert alpha_row is not None
    assert beta_row is not None
    assert alpha_row.query_key != beta_row.query_key

    staged = (
        await db.execute(
            select(StatutesStaging).where(StatutesStaging.source_name == "PakistanCode")
        )
    ).scalars().all()
    names = {
        (row.reconciled_json or {}).get("statute_name")
        for row in staged
        if row.reconciled_json
    }
    assert len(staged) == 2
    assert "Alpha Fisheries Act, 2026" in names
    assert "Beta Forestry Act, 2026" in names


def test_federal_catalog_paths_are_listings():
    assert _classify_discovered_url("https://pakistancode.gov.pk/english/sHyuRiF.php") == "listing"
    assert _classify_discovered_url("https://pakistancode.gov.pk/english/KmTY5e.php") == "listing"


async def test_live_catalog_fixture_discovers_acts_and_records_rejected_urls(db, fixture_server):
    """Homepage chrome is off-allow-list; Act rows live on sHyuRiF.php.

    A retired Act detail is retried. A detail retired for HTTP 404 stays retired.
    """
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/english/index.php", (FIXTURE_DIR / "listing_index_excerpt.html").read_text(encoding="utf-8"))
    fixture_server.add("/english/sHyuRiF.php", (FIXTURE_DIR / "federal_laws_excerpt.html").read_text(encoding="utf-8"))
    fixture_server.add("/english/UY2FqaJw1-apaUY2Fqa-apaUY2Npa5tqaw%3D%3D-sg-jjjjjjjjjjjjj", (FIXTURE_DIR / "act_detail_excerpt.html").read_text(encoding="utf-8"))
    fixture_server.add(
        "/pdffiles/defense-forces-act-2026.pdf",
        text_pdf_bytes(
            "Defense Forces of Pakistan Act, 2026\n"
            "1. Short title.- This Act may be called the Defense Forces of Pakistan Act, 2026.\n"
            "2. Definitions.- In this Act, unless the context otherwise requires, terms have assigned meanings.\n"
        ),
        content_type="application/pdf",
    )

    index_url = fixture_server.url("/english/index.php")
    defense_url = normalize_pakistancode_public_url(DEFENSE_ACT_PATH, base_url=index_url)
    ordinance_url = normalize_pakistancode_public_url(ORDINANCE_PATH, base_url=index_url)
    source = await _pakistancode_source(db, fixture_server, ["/english/index.php"])
    db.add(
        CrawlFrontier(
            source_name="PakistanCode",
            tier=0,
            query_key=f"listing:{defense_url}",
            query_json={"kind": "listing", "url": defense_url, "target_kind": "statute", "depth": 2},
            cursor_json={},
            status="retired",
            priority=80,
            attempts=3,
            last_error="[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate",
        )
    )
    db.add(
        CrawlFrontier(
            source_name="PakistanCode",
            tier=0,
            query_key=f"listing:{ordinance_url}",
            query_json={"kind": "listing", "url": ordinance_url, "target_kind": "instrument", "depth": 2},
            cursor_json={},
            status="retired",
            priority=80,
            attempts=1,
            last_error="HTTP 404",
        )
    )
    await db.commit()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_pakistancode(source, db, fetcher=fetcher, limit=4)
    await db.commit()

    assert stats["discovered"] > 0
    assert stats["halted"] is False
    assert "/english/sHyuRiF.php" in fixture_server.hits
    assert DEFENSE_ACT_PATH in fixture_server.hits
    assert "/pdffiles/defense-forces-act-2026.pdf" in fixture_server.hits
    assert ORDINANCE_PATH not in fixture_server.hits

    defense_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == f"listing:{defense_url}",
            )
        )
    ).scalars().first()
    ordinance_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "PakistanCode",
                CrawlFrontier.query_key == f"listing:{ordinance_url}",
            )
        )
    ).scalars().first()
    assert defense_row is not None and defense_row.status == "done"
    assert ordinance_row is not None and ordinance_row.status == "retired"
    assert ordinance_row.last_error == "HTTP 404"

    sample = stats["rejected_url_sample"]
    assert stats["rejected_urls"] >= 3
    assert len(sample) == stats["rejected_urls"]
    assert len(sample) <= REJECTED_URL_SAMPLE_LIMIT
    found_hosts = [urlsplit(item["url"]).hostname for item in sample]
    assert any(host == "drs.molaw.gov.pk" for host in found_hosts)
    assert any(host == "play.google.com" for host in found_hosts)
    assert any(host == "apps.apple.com" for host in found_hosts)
    assert all(item["reason"] for item in sample)


async def test_rejected_url_sample_keeps_the_first_twenty(db, fixture_server):
    source = await _pakistancode_source(db, fixture_server, ["/english/index.php"])
    pipeline = PakistanCodePipeline(db, source)
    base = fixture_server.url("/english/index.php")
    for i in range(REJECTED_URL_SAMPLE_LIMIT + 5):
        pipeline._capture_candidate(
            raw=f"https://off.example/chrome/{i}",
            hint="store",
            base_url=base,
            docs={},
            listings={},
            route_meta={},
        )
    sample = pipeline.stats["rejected_url_sample"]
    assert pipeline.stats["rejected_urls"] == REJECTED_URL_SAMPLE_LIMIT + 5
    assert len(sample) == REJECTED_URL_SAMPLE_LIMIT
    assert sample[0]["url"] == "https://off.example/chrome/0"
    assert sample[-1]["url"] == f"https://off.example/chrome/{REJECTED_URL_SAMPLE_LIMIT - 1}"
    assert "allow-list" in sample[0]["reason"]

    secret_pipeline = PakistanCodePipeline(db, source)
    secret_pipeline._capture_candidate(
        raw="https://user:secret-token@off.example/hidden?token=abcdef",
        hint="",
        base_url=base,
        docs={},
        listings={},
        route_meta={},
    )
    hidden = secret_pipeline.stats["rejected_url_sample"][0]["url"]
    assert hidden.startswith("https://off.example/hidden")
    assert "secret-token" not in hidden
    assert "abcdef" not in hidden
    assert "@" not in hidden

    secret_pipeline._capture_candidate(
        raw="http://alice:hunter2@/hidden",
        hint="",
        base_url=base,
        docs={},
        listings={},
        route_meta={},
    )
    hostless = secret_pipeline.stats["rejected_url_sample"][-1]["url"]
    assert "hunter2" not in hostless
    assert "alice" not in hostless

    secret_pipeline._capture_candidate(
        raw="https://off.example:bad/chrome",
        hint="",
        base_url=base,
        docs={},
        listings={},
        route_meta={},
    )
    bad_port = secret_pipeline.stats["rejected_url_sample"][-1]["url"]
    assert urlsplit(bad_port).hostname == "off.example"
    assert "bad" not in bad_port


def test_worker_death_retirement_is_not_reopened():
    row = type("Row", (), {})()
    row.status = "retired"
    row.last_error = "retired: the worker died 3 times while processing this item (out of memory, killed or timed out)"
    assert PakistanCodePipeline._retryable_retired(row) is False
