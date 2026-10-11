"""Pakistani courts now give each judgment a neutral citation of their own ("2025 SHC KHI 608", "2025 PHC 1",
"2012 LHC 869"). The extractor knew only the law reports, so every judgment fetched from a court website was held
as "no citation supported by source" (5,576 Sindh High Court, 1,378 Islamabad, 397 Gilgit-Baltistan, 303 Peshawar
and 259 AJK on 10 Oct 2026), and the Sindh High Court citation in each result row was dropped on the way."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.extractors.deterministic import extract_judgment_deterministic
from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, QuarantineQueue, ScraperSource, ScraperStaging
from scraper.parsers.citation_extractor import extract_citations, normalise_citation
from scraper.tasks.sindh_high_court import scrape_sindh_high_court
from tests.fixtures import text_pdf_bytes


@pytest.mark.parametrize(
    "text, normalized, reporter, court, year",
    [
        ("2025 SHC KHI 608", "2025 SHC KHI 608", "SHC", "Sindh", 2025),
        ("2021 shc khi 1329", "2021 SHC KHI 1329", "SHC", "Sindh", 2021),
        ("2024 SHC HYD 45", "2024 SHC HYD 45", "SHC", "Sindh", 2024),
        ("2024 SHC SUK 12", "2024 SHC SUK 12", "SHC", "Sindh", 2024),
        ("2025 PHC 1", "2025 PHC 1", "PHC", "Peshawar", 2025),
        ("2012 LHC 869", "2012 LHC 869", "LHC", "Lahore", 2012),
        ("2024 IHC 123", "2024 IHC 123", "IHC", "Islamabad", 2024),
        ("2023 SCP 245", "2023 SCP 245", "SCP", "SC", 2023),
    ],
)
def test_neutral_citations_are_recognised(text, normalized, reporter, court, year):
    hits = extract_citations(f"see {text} at para 4")
    assert len(hits) == 1
    hit = hits[0]
    assert hit["normalized"] == normalized
    assert hit["reporter"] == reporter
    assert hit["court"] == court
    assert hit["year"] == year
    assert normalise_citation(text) == normalized


@pytest.mark.parametrize("text", ["PLD 2023 IHC 5", "P.L.D. 2023 IHC 5", "P. L. D. 2023 IHC 5", "P L D 2023 IHC 5"])
def test_pld_with_a_court_token_is_not_also_read_as_a_neutral_citation(text):
    assert [h["normalized"] for h in extract_citations(text)] == ["PLD 2023 Islamabad 5"]


def test_row_neutral_citation_decides_the_court_over_a_cited_case():
    text = "Reliance is placed on PLD 2020 SC 1 and 2019 SCMR 5.\nThe petition is dismissed."
    out = extract_judgment_deterministic(html=None, text=text, source_meta={"citation": "2025 SHC KHI 608"})
    assert out["citations"][0] == "2025 SHC KHI 608"
    assert out["court"] == "Sindh"


def test_law_report_citations_unchanged():
    assert [h["normalized"] for h in extract_citations("2024 SCMR 1 and 2023 CLC 45")] == ["2024 SCMR 1", "2023 CLC 45"]


def test_result_row_neutral_citation_gives_citation_court_and_year():
    text = "HIGH COURT OF SINDH AT KARACHI\nConst. P. 3257 of 2010\nOrder dated 21-01-2025\nPetition allowed."
    out = extract_judgment_deterministic(html=None, text=text, source_meta={"citation": "2025 SHC KHI 608"})
    assert out["citations"][0] == "2025 SHC KHI 608"
    assert out["year"] == 2025
    assert out["court"]


async def _shc_source(db, fixture_server, listings):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SindhHighCourt"))).scalars().first()
    source.allow_list = ["127.0.0.1", "localhost"]
    source.respect_robots = True
    source.crawl_max_depth = 3
    source.extraction_mode = "deterministic"
    source.config_json = {
        "listings": [fixture_server.url(path) for path in listings],
        "detail_result_page_size": 10,
        "detail_result_max_pages": 1,
    }
    await db.commit()
    return source


def _grid(rows):
    body = "".join(
        f"<tr><td>{i}</td><td>{cit}</td><td><!-- <a href=\"view-file/{tok}\">t</a> -->"
        f"<a href=\"download-file.php?doc={tok}&citation={cit.replace(' ', '+')}\">Const. P. {i}/2024</a></td></tr>"
        for i, (tok, cit) in enumerate(rows, 1)
    )
    return f"<html><body><table><tbody>{body}</tbody></table></body></html>"


_PDF_TEXT = (
    "IN THE HIGH COURT OF SINDH AT KARACHI\nConst. P. No. D-{n} of 2024\n"
    "Before: Mr. Justice Muhammad Karim Khan Agha and Mr. Justice Adnan-ul-Karim Memon\n"
    "Hafiz Muhammad Tariq ........ Petitioner\nVersus\nFederation of Pakistan and others ........ Respondents\n"
    "Date of hearing: 21.01.2025\nJUDGMENT\n"
    + "The petitioner challenges the order of the respondents under Article 199 of the Constitution.\n" * 8
    + "The petition is allowed in the above terms."
)


async def test_shc_judgment_takes_its_citation_from_the_result_row(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/caselaw/public/reported-judgements-detail-all/844/-1", _grid([("TOK-1", "2025 SHC KHI 608")]))
    fixture_server.add("/caselaw/view-file/TOK-1", text_pdf_bytes(_PDF_TEXT.format(n=1)), content_type="application/pdf")
    source = await _shc_source(db, fixture_server, ["/caselaw/public/reported-judgements-detail-all/844/-1"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.source_name == "SindhHighCourt"))).scalars().one()
    assert st.extracted_citation == "2025 SHC KHI 608"
    assert st.quarantine_reason != "no citation supported by source"


async def test_held_shc_judgment_is_requalified_when_its_grid_row_is_seen_again(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/caselaw/view-file/TOK-9", text_pdf_bytes(_PDF_TEXT.format(n=9)), content_type="application/pdf")
    # first pass: the row carries no citation (as every row looked to the old code), so the judgment is held
    fixture_server.add("/caselaw/public/reported-judgements-detail-all/900/-1", _grid([("TOK-9", "Nil")]))
    source = await _shc_source(db, fixture_server, ["/caselaw/public/reported-judgements-detail-all/900/-1"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()
    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.source_name == "SindhHighCourt"))).scalars().one()
    assert st.status == "quarantined"
    assert st.quarantine_reason == "no citation supported by source"
    db.add(QuarantineQueue(staging_id=st.id, reason=st.quarantine_reason, source_name="SindhHighCourt"))
    await db.commit()

    # second pass: the grid row names the citation; the held judgment is re-checked with it
    fixture_server.add("/caselaw/public/reported-judgements-detail-all/900/-1", _grid([("TOK-9", "2025 SHC KHI 77")]))
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()

    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.id == st.id))).scalars().one()
    assert st.extracted_citation == "2025 SHC KHI 77"
    assert st.status in ("extracted", "promoted"), (st.quarantine_reason, st.validation_errors, st.confidence_score, st.extracted_court)
    item = (await db.execute(select(QuarantineQueue).where(QuarantineQueue.staging_id == st.id))).scalars().one()
    assert item.reviewed is True
    assert item.resolution == "requalified"
    fr = (
        await db.execute(select(CrawlFrontier).where(CrawlFrontier.source_name == "SindhHighCourt", CrawlFrontier.query_key.like("judgment:%")))
    ).scalars().one()
    assert fr.query_json["meta"]["citation"] == "2025 SHC KHI 77"


async def test_done_detail_grids_are_read_again_once_to_pick_up_citations(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/caselaw/public/rpt-afr", '<html><body><a href="/caselaw/public/reported-judgements-detail-all/901/-1">1</a></body></html>')
    fixture_server.add("/caselaw/public/reported-judgements-detail-all/901/-1", _grid([("TOK-5", "Nil")]))
    fixture_server.add("/caselaw/view-file/TOK-5", text_pdf_bytes(_PDF_TEXT.format(n=5)), content_type="application/pdf")
    source = await _shc_source(db, fixture_server, ["/caselaw/public/rpt-afr"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()
    hits_before = fixture_server.hits.count("/caselaw/public/reported-judgements-detail-all/901/-1")

    fixture_server.add("/caselaw/public/reported-judgements-detail-all/901/-1", _grid([("TOK-5", "2025 SHC KHI 5")]))
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()
    assert fixture_server.hits.count("/caselaw/public/reported-judgements-detail-all/901/-1") == hits_before + 1
    st = (await db.execute(select(ScraperStaging).where(ScraperStaging.source_name == "SindhHighCourt"))).scalars().one()
    assert st.extracted_citation == "2025 SHC KHI 5"

    # the re-read happens once, not on every run
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        await scrape_sindh_high_court(source, db, fetcher=fetcher, limit=20)
    await db.commit()
    assert fixture_server.hits.count("/caselaw/public/reported-judgements-detail-all/901/-1") == hits_before + 1
