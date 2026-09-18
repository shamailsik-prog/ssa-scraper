from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import DEFAULT_LISTINGS, normalize_ajk_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _ajk_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "AJKAssembly",
            )
        )
    ).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 2
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "target_kind": "statute",
    }
    await db.commit()
    return source


def test_normalize_ajk_public_url_canonicalizes_law_host():
    rel = normalize_ajk_public_url(
        "//www.law.gok.pk/wp-content/uploads/2026/01/sample-act.pdf",
        base_url="https://law.gok.pk/revised-volume/",
    )
    assert rel == "https://law.gok.pk/wp-content/uploads/2026/01/sample-act.pdf"


def test_ajk_assembly_default_listings_include_law_department_seeds():
    urls = [entry["url"] for entry in DEFAULT_LISTINGS["AJKAssembly"]]
    expected = {
        "https://law.gok.pk/revised-volume/",
        "https://law.gok.pk/acts/",
        "https://law.gok.pk/ordinance/",
    }
    assert expected.issubset(set(urls))


async def test_ajk_revised_volume_fans_out_detail_then_document_with_target_kinds(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/revised-volume/",
        """
        <html><body>
          <table>
            <thead><tr><th>Title</th><th>Act No</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td><a href="/acts/the-ajk-public-service-act-2024/">The AJK Public Service Act, 2024</a></td>
                <td>IX</td>
                <td>2024</td>
              </tr>
              <tr>
                <td><a href="/ordinance/the-ajk-tax-ordinance-2024/">The AJK Tax Ordinance, 2024</a></td>
                <td>III</td>
                <td>2024</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/the-ajk-public-service-act-2024/",
        """
        <html><body>
          <h2>The AJK Public Service Act, 2024</h2>
          <a href="/wp-content/uploads/2024/09/ajk-public-service-act-2024.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/ordinance/the-ajk-tax-ordinance-2024/",
        """
        <html><body>
          <h2>The AJK Tax Ordinance, 2024</h2>
          <a href="/wp-content/uploads/2024/09/ajk-tax-ordinance-2024.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/wp-content/uploads/2024/09/ajk-public-service-act-2024.pdf",
        text_pdf_bytes("The AJK Public Service Act, 2024"),
        content_type="application/pdf",
    )
    fixture_server.add(
        "/wp-content/uploads/2024/09/ajk-tax-ordinance-2024.pdf",
        text_pdf_bytes("The AJK Tax Ordinance, 2024"),
        content_type="application/pdf",
    )

    source = await _ajk_assembly_source(db, fixture_server, ["/revised-volume/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=80)
    await db.commit()

    assert stats["halted"] is False
    assert "/acts/the-ajk-public-service-act-2024/" in fixture_server.hits
    assert "/ordinance/the-ajk-tax-ordinance-2024/" in fixture_server.hits

    act_detail_url = f"http://127.0.0.1:{fixture_server.port}/acts/the-ajk-public-service-act-2024/"
    ordinance_detail_url = f"http://127.0.0.1:{fixture_server.port}/ordinance/the-ajk-tax-ordinance-2024/"
    act_detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"listing:{act_detail_url}",
            )
        )
    ).scalars().first()
    ordinance_detail_row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"listing:{ordinance_detail_url}",
            )
        )
    ).scalars().first()
    assert act_detail_row is not None
    assert ordinance_detail_row is not None
    assert act_detail_row.query_json["target_kind"] == "statute"
    assert ordinance_detail_row.query_json["target_kind"] == "instrument"
    assert ordinance_detail_row.query_json["meta"]["act_no"] == "III"

    act_doc_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2024/09/ajk-public-service-act-2024.pdf"
    ordinance_doc_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2024/09/ajk-tax-ordinance-2024.pdf"
    act_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"statute:{act_doc_url}",
            )
        )
    ).scalars().first()
    ordinance_doc = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"instrument:{ordinance_doc_url}",
            )
        )
    ).scalars().first()
    assert act_doc is not None
    assert ordinance_doc is not None
    assert act_doc.query_json["expect_pdf"] is True
    assert ordinance_doc.query_json["expect_pdf"] is True
    assert ordinance_doc.query_json["route"]["detail_fetch"] == "detail_file_link"
    assert ordinance_doc.query_json["route"]["detail_url"] == ordinance_detail_url

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "AJKAssembly",
                SourceProvenance.source_url == ordinance_doc_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["source_section"] == "ordinance"
    assert prov.route_json["act_title"] == "The AJK Tax Ordinance, 2024"


async def test_ajk_assembly_pdf_signature_gate_retires_non_pdf_download(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/revised-volume/",
        """
        <html><body>
          <table>
            <thead><tr><th>Title</th><th>Act No</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td><a href="/ordinance/fake-ordinance-2024/">Fake Ordinance 2024</a></td>
                <td>VII</td>
                <td>2024</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/ordinance/fake-ordinance-2024/",
        """
        <html><body>
          <h2>Fake Ordinance 2024</h2>
          <a href="/wp-content/uploads/2024/09/fake-ordinance-2024.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/wp-content/uploads/2024/09/fake-ordinance-2024.pdf",
        "<html>not-a-pdf</html>",
        content_type="application/pdf",
    )

    source = await _ajk_assembly_source(db, fixture_server, ["/revised-volume/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=50)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "AJKAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2024/09/fake-ordinance-2024.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"instrument:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for instrument document URL" in (row.last_error or "")


async def test_ajk_assembly_detail_flow_is_idempotent_on_rerun(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/revised-volume/",
        """
        <html><body>
          <table>
            <thead><tr><th>Title</th><th>Act No</th><th>Year</th></tr></thead>
            <tbody>
              <tr>
                <td><a href="/acts/the-ajk-idempotence-act-2021/">The AJK Idempotence Act, 2021</a></td>
                <td>XI</td>
                <td>2021</td>
              </tr>
            </tbody>
          </table>
        </body></html>
        """,
    )
    fixture_server.add(
        "/acts/the-ajk-idempotence-act-2021/",
        """
        <html><body>
          <h2>The AJK Idempotence Act, 2021</h2>
          <a href="/wp-content/uploads/2021/01/ajk-idempotence-act-2021.pdf">Download PDF</a>
        </body></html>
        """,
    )
    fixture_server.add(
        "/wp-content/uploads/2021/01/ajk-idempotence-act-2021.pdf",
        text_pdf_bytes("The AJK Idempotence Act, 2021"),
        content_type="application/pdf",
    )

    source = await _ajk_assembly_source(db, fixture_server, ["/revised-volume/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        first = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()
    assert first["halted"] is False

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        second = await scrape_legislature(source, db, fetcher=fetcher, limit=60)
    await db.commit()
    assert second["halted"] is False
    assert second["discovered"] == 0

    detail_url = f"http://127.0.0.1:{fixture_server.port}/acts/the-ajk-idempotence-act-2021/"
    detail_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"listing:{detail_url}",
            )
        )
    ).scalar()
    assert detail_count == 1

    document_url = f"http://127.0.0.1:{fixture_server.port}/wp-content/uploads/2021/01/ajk-idempotence-act-2021.pdf"
    document_count = (
        await db.execute(
            select(func.count())
            .select_from(CrawlFrontier)
            .where(
                CrawlFrontier.source_name == "AJKAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalar()
    assert document_count == 1
