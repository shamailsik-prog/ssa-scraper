# PakistanCode frontier priority: docs before listings under ASC pending order.

from scraper.tasks import pakistancode as pc


def test_pakistan_code_document_priority_beats_listing_under_asc() -> None:
    assert pc.PC_DOCUMENT_PRIORITY == 10
    assert pc.PC_LISTING_PRIORITY == 80
    assert pc.PC_DOCUMENT_PRIORITY < pc.PC_LISTING_PRIORITY
    assert pc.PC_CRAWL_MAX_PAGES == 200
