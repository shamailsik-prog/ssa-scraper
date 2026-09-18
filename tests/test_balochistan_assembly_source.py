from __future__ import annotations

from sqlalchemy import func, select

from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, ScraperSource, SourceProvenance, StatutesStaging
from scraper.tasks.legislatures import normalize_pab_public_url, scrape_legislature
from tests.fixtures import text_pdf_bytes


async def _balochistan_assembly_source(db, fixture_server, listings):
    source = (
        await db.execute(
            select(ScraperSource).where(
                ScraperSource.source_name == "BalochistanAssembly",
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


def test_normalize_pab_public_url_handles_relative_storage_path():
    rel = normalize_pab_public_url(
        "/storage/9339/act-2026.pdf",
        base_url="https://www.pabalochistan.gov.pk/acts",
    )
    assert rel == "https://pabalochistan.gov.pk/storage/9339/act-2026.pdf"


async def test_balochistan_assembly_structured_rows_enqueue_pdf_with_provenance_route_meta(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <div id="tenureAccordion">
            <div class="accordion-item">
              <h2 class="accordion-header"><button>2024-2029</button></h2>
              <div class="accordion-body">
                <div class="accordion-item">
                  <h2 class="accordion-header"><button>2026</button></h2>
                  <div class="accordion-body">
                    <table class="table table-bordered table-striped">
                      <thead><tr><th>Act No</th><th>Act Title</th><th>Passed on</th><th>Assented on</th><th>Type</th></tr></thead>
                      <tbody>
                        <tr>
                          <td>15</td>
                          <td><a href="/storage/9339/the-balochistan-prisons-act-2026.pdf">THE BALOCHISTAN PRISONS ACT, 2026, ACT NO. XV OF 2026.</a></td>
                          <td>14/05/2026</td>
                          <td>19/05/2026</td>
                          <td>Government</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add(
        "/storage/9339/the-balochistan-prisons-act-2026.pdf",
        text_pdf_bytes(
            "THE BALOCHISTAN PRISONS ACT, 2026\nACT NO. XV OF 2026\nProvincial Assembly of Balochistan"
        ),
        content_type="application/pdf",
    )

    source = await _balochistan_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=30)
    await db.commit()

    assert stats["halted"] is False
    assert stats["discovered"] >= 1
    assert "/storage/9339/the-balochistan-prisons-act-2026.pdf" in fixture_server.hits

    document_url = f"http://127.0.0.1:{fixture_server.port}/storage/9339/the-balochistan-prisons-act-2026.pdf"
    frontier = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert frontier is not None
    assert frontier.query_json["expect_pdf"] is True
    assert frontier.query_json["route"]["listing_fetch"] == "acts_table"
    assert frontier.query_json["route"]["act_year"] == "2026"
    assert frontier.query_json["route"]["act_no"] == "15"
    assert frontier.query_json["route"]["pdf_endpoint_kind"] == "storage-file"

    prov = (
        await db.execute(
            select(SourceProvenance).where(
                SourceProvenance.source_name == "BalochistanAssembly",
                SourceProvenance.source_url == document_url,
                SourceProvenance.content_kind == "pdf",
            )
        )
    ).scalars().first()
    assert prov is not None
    assert prov.route_json["act_title"].startswith("THE BALOCHISTAN PRISONS ACT")
    assert prov.route_json["document_format"] == "pdf"
    assert prov.route_json["tenure"] == "2024-2029"


async def test_balochistan_assembly_pdf_signature_gate_retires_non_pdf_statute_link(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add(
        "/acts",
        """
        <html><body>
          <div id="tenureAccordion">
            <div class="accordion-item">
              <div class="accordion-body">
                <div class="accordion-item">
                  <div class="accordion-body">
                    <table class="table table-bordered table-striped">
                      <thead><tr><th>Act No</th><th>Act Title</th></tr></thead>
                      <tbody>
                        <tr>
                          <td>1</td>
                          <td><a href="/storage/1000/fake-act.pdf">Fake Act</a></td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </body></html>
        """,
    )
    fixture_server.add("/storage/1000/fake-act.pdf", "<html>not-a-pdf</html>", content_type="application/pdf")

    source = await _balochistan_assembly_source(db, fixture_server, ["/acts"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    assert stats["halted"] is False
    staged = (
        await db.execute(
            select(func.count())
            .select_from(StatutesStaging)
            .where(StatutesStaging.source_name == "BalochistanAssembly")
        )
    ).scalar()
    assert staged == 0

    document_url = f"http://127.0.0.1:{fixture_server.port}/storage/1000/fake-act.pdf"
    row = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "BalochistanAssembly",
                CrawlFrontier.query_key == f"statute:{document_url}",
            )
        )
    ).scalars().first()
    assert row is not None
    assert row.status == "retired"
    assert "missing %PDF signature for statute document URL" in (row.last_error or "")
