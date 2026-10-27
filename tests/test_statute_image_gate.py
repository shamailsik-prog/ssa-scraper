"""WhatsApp JPEG / JFIF statute sources are rejected before a statute row is written."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from scraper.document_validation import validate_statute_reconciled_json
from scraper.fetchers import HttpFetcher
from scraper.models import CrawlFrontier, DocumentQuarantine, Statute, StatutesStaging
from scraper.tasks.legislatures import SindhAssemblyPipeline
from scraper.tasks.promotion import promote_statute_staging
from tests.test_b3_statute_promotion import _make_statute_staging
from tests.test_sindh_assembly_source import _sindh_assembly_source

AUDITOR_JPEG_URL = (
    "https://sindhlaws.gov.pk/GalleryImages/"
    "20379-WhatsApp%20Image%202026-07-18%20at%204.48.17%20PM%20(1).jpeg"
)


def whatsapp_jfif_bytes() -> bytes:
    """JPEG SOI (ÿØÿà) + JFIF + ICC_PROFILE, then a non-text payload."""
    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xe2\x00\x1cICC_PROFILE\x00\x01\x01"
        + bytes(i % 256 for i in range(480))
    )


def _jfif_text() -> str:
    return whatsapp_jfif_bytes().decode("latin-1")


def test_image_gate_rejects_gallery_jpeg_url_content_type_and_magic():
    from scraper.statute_image_gate import statute_source_is_image, statute_url_is_image

    body = whatsapp_jfif_bytes()
    assert statute_url_is_image(AUDITOR_JPEG_URL)
    assert statute_url_is_image("https://sindhlaws.gov.pk/GalleryImages/scan.JPG")
    assert statute_url_is_image("https://example.test/a.PNG")
    assert statute_url_is_image("https://example.test/a.gif")
    assert statute_url_is_image("https://example.test/a.webp")
    assert statute_url_is_image("https://example.test/a.bmp")
    assert not statute_url_is_image("https://sindhlaws.gov.pk/setup/publications/act.pdf")
    assert not statute_url_is_image("https://sindhlaws.gov.pk/GazetteDetail.aspx?X=ACT&Year=2026")
    assert statute_source_is_image(url="", content_type="image/jpeg; charset=binary", content=b"")
    assert statute_source_is_image(url="https://example.test/act.html", content_type="text/html", content=body)
    assert statute_source_is_image(url="", content_type="", content=b"\x89PNG\r\n\x1a\n" + b"IHDR")
    assert not statute_source_is_image(
        url="https://example.test/act.pdf",
        content_type="application/pdf",
        content=b"%PDF-1.4\nICC_PROFILE later is still a pdf",
    )
    assert not statute_source_is_image(
        url="https://example.test/act.html",
        content_type="text/html",
        content=b"<html><body>The Sales Tax Act, 1990.</body></html>",
    )


def test_binary_garbage_text_catches_jfif_name_and_spares_prose():
    from scraper.statute_image_gate import is_binary_garbage_text

    raw = _jfif_text()
    assert is_binary_garbage_text(raw[:120])
    assert is_binary_garbage_text(raw)
    assert not is_binary_garbage_text("Sales Tax Act, 1990")
    assert not is_binary_garbage_text(
        "1. This Act may be called the Sales Tax Act, 1990 and it extends to the whole of Pakistan."
    )
    assert not is_binary_garbage_text("دفعہ ۱۔ یہ ایکٹ سندھ میں نافذ ہوگا اور حکومت قواعد بنا سکے گی۔")
    schedule = "1 0101.21 Horses 5% 2 0101.29 Other 10% " * 4
    assert not is_binary_garbage_text(schedule)
    assert not is_binary_garbage_text("The annex mentions a JFIF marker only as a technical label in this Act.")


def test_validator_rejects_jfif_name_and_binary_sections():
    raw = _jfif_text()
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": raw[:180],
            "sections": [
                {"section_number": "0", "section_text": raw[:200]},
                {"section_number": "1", "section_text": raw[200:400]},
                {"section_number": "2", "section_text": raw[400:]},
            ],
        },
        raw_text=raw,
    )
    assert not ok
    assert reason == "binary_statute_text"


def test_validator_rejects_clean_name_when_section_text_is_binary():
    raw = _jfif_text()
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "Sindh Laws Gallery Capture, 2026",
            "year_enacted": 2026,
            "sections": [
                {"section_number": "1", "section_text": raw},
            ],
        },
        raw_text="Sindh Laws Gallery Capture, 2026",
    )
    assert not ok
    assert reason == "binary_statute_text"


def _latin1_nonprose_junk() -> str:
    """High bytes with no JPEG SOI, JFIF, or ICC_PROFILE marker."""
    return bytes(range(1, 256)).decode("latin-1") * 2


def _urdu_with_inpage_private_use() -> tuple[str, str]:
    """Urdu prose that also carries InPage BMP private-use letterforms.

    ``sprinkled`` is still mostly Arabic, but more than 20% of its characters
    are U+E000–U+F8FF, so ``str.isprintable()`` used to call it binary.
    ``inpage_body`` is the legacy shape: the letterforms themselves are
    private-use, with a short Urdu frame, so the allowed-character ratio
    used to fall under one half.
    """
    urdu = "دفعہ ۱۔ یہ ایکٹ سندھ میں نافذ ہوگا اور حکومت قواعد بنا سکے گی۔ "
    # Four copies of the private-use run: about a quarter of the string, so the
    # allowed-character ratio stays above one half and the non-printable check
    # is what rejects it. Endpoints U+E000 and U+F8FF are both present.
    pua = "\uE000\uE001\uE0A0\uF000\uF8FF"
    sprinkled = urdu + (pua * 4)
    inpage_body = "سندھ " + "".join(chr(0xE000 + i) for i in range(40)) + "\uF8FF ایکٹ "
    return sprinkled, inpage_body


def test_validator_rejects_latin1_junk_without_jfif_marker():
    raw = _latin1_nonprose_junk()
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "Not A Statute",
            "sections": [{"section_number": "1", "section_text": raw}],
        },
        raw_text=raw,
    )
    assert not ok
    assert reason == "binary_statute_text"


def test_validator_accepts_urdu_text_with_inpage_private_use():
    from scraper.statute_image_gate import is_binary_garbage_text

    sprinkled, inpage_body = _urdu_with_inpage_private_use()
    assert "\uE000" in sprinkled and "\uF8FF" in sprinkled
    assert "\uE000" in inpage_body and "\uF8FF" in inpage_body
    assert not is_binary_garbage_text(sprinkled)
    assert not is_binary_garbage_text(inpage_body)

    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "سندھ لوکل گورنمنٹ (ترمیمی) ایکٹ، 2023",
            "year_enacted": 2023,
            "sections": [
                {"section_number": "1", "section_text": sprinkled},
                {"section_number": "2", "section_text": inpage_body},
            ],
        },
        raw_text=sprinkled + "\n" + inpage_body,
    )
    assert ok
    assert reason == ""


def test_validator_still_accepts_a_normal_statute():
    ok, reason = validate_statute_reconciled_json(
        {
            "statute_name": "Sales Tax Act, 1990",
            "sections": [
                {
                    "section_number": "1",
                    "section_text": "This Act may be called the Sales Tax Act, 1990.",
                }
            ],
        },
        raw_text="This Act may be called the Sales Tax Act, 1990.",
    )
    assert ok
    assert reason == ""


@pytest.mark.asyncio
async def test_promotion_writes_no_statute_for_binary_section_text(db):
    raw = _jfif_text()
    st = await _make_statute_staging(
        db,
        {
            "statute_name": "Sindh Laws Gallery Capture, 2026",
            "year_enacted": 2026,
            "jurisdiction": "Sindh",
            "sections": [
                {"section_number": "0", "section_text": raw[:220]},
                {"section_number": "1", "section_text": raw[220:440]},
                {"section_number": "2", "section_text": raw[440:]},
            ],
        },
    )
    assert await promote_statute_staging(db, st) == "quarantined"
    assert (st.quarantine_reason or "").startswith("document_validation:binary_statute_text")
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_sindhlaws_gallery_jpeg_is_not_enqueued(db, fixture_server):
    from scraper.tasks.legislatures import scrape_legislature

    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    fixture_server.add("/", "<html><body><a href='/Gazette.aspx?pg=ACT'>Acts</a></body></html>")
    fixture_server.add(
        "/Gazette.aspx?pg=ACT",
        "<html><body><a href='/GazetteDetail.aspx?X=ACT&Year=2026'>2026</a></body></html>",
    )
    jpeg_path = "/GalleryImages/20379-WhatsApp-Image-2026-07-18.jpeg"
    fixture_server.add(
        "/GazetteDetail.aspx?X=ACT&Year=2026",
        f"""
        <html><body>
          <table><tbody>
            <tr><td>I</td><td>WHATSAPP GAZETTE, 2026</td>
            <td><a href="{jpeg_path}">Download</a></td><td>Jul 18, 2026</td></tr>
          </tbody></table>
        </body></html>
        """,
    )
    fixture_server.add(jpeg_path, whatsapp_jfif_bytes(), content_type="image/jpeg")

    source = await _sindh_assembly_source(db, fixture_server, ["/"])
    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        stats = await scrape_legislature(source, db, fetcher=fetcher, limit=40)
    await db.commit()

    assert stats["rejected_urls"] >= 1
    staged = (
        await db.execute(
            select(func.count()).select_from(StatutesStaging).where(StatutesStaging.source_name == "SindhAssembly")
        )
    ).scalar()
    assert staged == 0
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0
    assert not any(jpeg_path in hit for hit in fixture_server.hits)
    queued = (
        await db.execute(
            select(CrawlFrontier).where(
                CrawlFrontier.source_name == "SindhAssembly",
                CrawlFrontier.query_key.contains("GalleryImages"),
            )
        )
    ).scalars().first()
    assert queued is None


@pytest.mark.asyncio
async def test_queued_gallery_jpeg_is_retired_without_statute_write(db, fixture_server):
    fixture_server.add("/robots.txt", "", status=404, content_type="text/plain")
    jpeg_path = "/GalleryImages/20379-WhatsApp-Image-2026-07-18.jpeg"
    fixture_server.add(jpeg_path, whatsapp_jfif_bytes(), content_type="image/jpeg")
    source = await _sindh_assembly_source(db, fixture_server, ["/"])
    url = fixture_server.url(jpeg_path)
    row = CrawlFrontier(
        source_name=source.source_name,
        tier=0,
        query_key=f"statute:{url}",
        query_json={"kind": "statute", "url": url, "route": {}, "meta": {"act_title": "WHATSAPP GAZETTE, 2026"}},
        cursor_json={},
        priority=50,
    )
    db.add(row)
    await db.flush()

    async with HttpFetcher(source, allow_private_for_tests=True) as fetcher:
        pipeline = SindhAssemblyPipeline(db, source, fetcher=fetcher)
        assert await pipeline._drain_one(row) == "ok"

    assert row.status == "retired"
    assert "image statute source rejected" in (row.last_error or "")
    assert (await db.execute(select(func.count()).select_from(StatutesStaging))).scalar() == 0
    assert (await db.execute(select(func.count()).select_from(Statute))).scalar() == 0


@pytest.mark.asyncio
async def test_null_year_inventory_is_read_only(db):
    from scraper.tasks.statute_null_year import REASON_NULL_YEAR_TITLE_REVIEW, visible_null_year_inventory

    visible = Statute(
        name="OCR Garbled Senate Title Without Year",
        jurisdiction="Federal",
        year_enacted=None,
        source_name="Senate",
        source_url="https://senate.gov.pk/uploads/documents/ocr.pdf",
    )
    dated = Statute(
        name="Sales Tax Act, 1990",
        jurisdiction="Federal",
        year_enacted=1990,
        source_name="Senate",
    )
    hidden = Statute(
        name="Hidden Null Year Act",
        jurisdiction="Federal",
        year_enacted=None,
        source_name="Senate",
    )
    db.add_all([visible, dated, hidden])
    await db.flush()
    db.add(
        DocumentQuarantine(
            doc_type="statute",
            doc_id=hidden.id,
            reason="already quarantined",
            run_id="test-null-year",
        )
    )
    await db.flush()

    report = await visible_null_year_inventory(db)
    ids = {item["id"] for item in report["statutes"]}
    assert report["dry_run"] is True
    assert report["writes"] == 0
    assert report["reason_code"] == REASON_NULL_YEAR_TITLE_REVIEW
    assert str(visible.id) in ids
    assert str(dated.id) not in ids
    assert str(hidden.id) not in ids
    assert all(item["reason_code"] == REASON_NULL_YEAR_TITLE_REVIEW for item in report["statutes"])

    await db.refresh(visible)
    assert visible.year_enacted is None
    quarantine_rows = (await db.execute(select(func.count()).select_from(DocumentQuarantine))).scalar()
    assert quarantine_rows == 1


def test_null_year_cli_has_no_apply_switch():
    from scraper.tasks.statute_null_year import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["--apply"])
