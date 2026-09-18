from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, ScraperStaging, SourceProvenance
from scraper.tasks.lahore_high_court import normalize_lhc_public_url, scrape_lahore_high_court
from tests.fixtures import text_pdf_bytes


async def _lhc_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "LahoreHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
    }
    await db.commit()
    return source


def test_normalize_lhc_public_url_handles_wayback_relative_and_sys_paths():
    wayback = "https://web.archive.org/web/20241005132304/https://opc.lhc.gov.pk/pdf/2024CLD917.pdf"
    out = normalize_lhc_public_url(wayback, base_url="https://opc.lhc.gov.pk/Relevant_Laws.aspx")
    assert out == "https://opc.lhc.gov.pk/pdf/2024CLD917.pdf"

    rel = normalize_lhc_public_url("pdf/2021CLC392.pdf", base_url="https://opc.lhc.gov.pk/Relevant_Laws.aspx")
    assert rel == "https://opc.lhc.gov.pk/pdf/2021CLC392.pdf"

    sys_rel = normalize_lhc_public_url("appjudgments/Reported", base_url="http://sys.lhc.gov.pk/appjudgments/")
    assert sys_rel == "http://sys.lhc.gov.pk/appjudgments/Reported"


async def test_lhc_result_listing_discovers_judgment_pdfs_with_route_and_provenance(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Relevant_Laws.aspx",
        f"""
        <html><body>
          <h3>LAWS</h3>
          <ol>
            <li>The Constitution of Islamic Republic of Pakistan, 1973 <a href="pdf/The_Constitution_of_Islamic_Republic_of_Pakistan_1973.pdf">Download</a></li>
          </ol>
          <h3>Supreme Court of Pakistan</h3>
          <ol>
            <li>2024 C L D 917, China Harbour vs ZZ Enterprises <a href="pdf/2024CLD917.pdf">Download</a></li>
            <li>2021 C L C 392, Muhammad Yaqoob Vs Commissioner Lahore Division <a href="/pdf/2021CLC392.pdf">Download</a></li>
            <li data-pdf="pdf/1_WP_50962-19_Overseas_Pakistanies.pdf">2020 PLD Lahore 49, Tariq Mehmood Vs Commission</li>
          </ol>
          <script>
            const hiddenJudgment = "http://127.0.0.1:{fixture_server.port}/pdf/73598_21_Rabeah_Hussain_vs_Nusrat_Aftab.pdf";
          </script>
        </body></html>
        """,
    )

    for path in (
        "/pdf/2024CLD917.pdf",
        "/pdf/2021CLC392.pdf",
        "/pdf/1_WP_50962-19_Overseas_Pakistanies.pdf",
        "/pdf/73598_21_Rabeah_Hussain_vs_Nusrat_Aftab.pdf",
    ):
        fixture_server.add(
            path,
            text_pdf_bytes(
                "PLD 2024 LHC 1\nLAHORE HIGH COURT, LAHORE\nWrit Petition No. 1 of 2024\nDecided on 1st January 2024\nPetition dismissed."
            ),
            content_type="application/pdf",
        )
    fixture_server.add(
        "/pdf/The_Constitution_of_Islamic_Republic_of_Pakistan_1973.pdf",
        text_pdf_bytes("Constitution"),
        content_type="application/pdf",
    )

    source = await _lhc_source(db, fixture_server, ["/Relevant_Laws.aspx"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_lahore_high_court(source, db, fetcher=fetcher, limit=60)
    await db.commit()

    assert stats["discovered"] >= 4
    assert "/pdf/2024CLD917.pdf" in fixture_server.hits
    assert "/pdf/2021CLC392.pdf" in fixture_server.hits
    assert "/pdf/1_WP_50962-19_Overseas_Pakistanies.pdf" in fixture_server.hits
    assert "/pdf/The_Constitution_of_Islamic_Republic_of_Pakistan_1973.pdf" not in fixture_server.hits

    pdf_rows = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "LahoreHighCourt",
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().all()
    urls = {p.source_url for p in pdf_rows}
    assert "http://127.0.0.1:%s/pdf/2024CLD917.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/pdf/1_WP_50962-19_Overseas_Pakistanies.pdf" % fixture_server.port in urls
    assert "http://127.0.0.1:%s/pdf/The_Constitution_of_Islamic_Republic_of_Pakistan_1973.pdf" % fixture_server.port not in urls

    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "LahoreHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/pdf/2024CLD917.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.query_json["route"]["listing_fetch"] == "result_list"
    assert row.query_json["route"]["result_section"] == "Supreme Court of Pakistan"
    assert isinstance(row.query_json["route"]["result_index"], int)


async def test_lhc_pdf_signature_gate_retires_non_pdf_document_url(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/Relevant_Laws.aspx",
        """
        <html><body>
          <h3>Judgements</h3>
          <ol>
            <li>2024 PLD Lahore 421, Case Vs State <a href="pdf/Bad.pdf">Download</a></li>
          </ol>
        </body></html>
        """,
    )
    fixture_server.add("/pdf/Bad.pdf", "<html><body>not pdf</body></html>", content_type="application/pdf")

    source = await _lhc_source(db, fixture_server, ["/Relevant_Laws.aspx"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_lahore_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(ScraperStaging)
            .where(ScraperStaging.source_name == "LahoreHighCourt")
        )
    ).scalar()
    assert staged == 0
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "LahoreHighCourt",
                CrawlFrontier.query_key == f"judgment:http://127.0.0.1:{fixture_server.port}/pdf/Bad.pdf",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature" in (row.last_error or "")
