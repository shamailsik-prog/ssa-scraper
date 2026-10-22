"""F2: /admin/corpus lists hide document_quarantine rows; detail exposes is_quarantined."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from scraper.models import Citation, DocumentQuarantine, Judgment, Statute


@pytest.mark.asyncio
async def test_admin_corpus_judgment_list_excludes_quarantined(db, client, admin_headers):
    visible = Judgment(
        canonical_citation="PLD 2022 SC 1",
        case_title="Visible Admin",
        access_method="public",
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
    )
    hidden = Judgment(
        canonical_citation="PLD 2022 SC 9",
        case_title="Hidden Admin",
        access_method="public",
        full_text="The court hears the matter at length and disposes of the appeal with detailed reasons on every issue raised.",
    )
    db.add_all([visible, hidden])
    await db.flush()
    db.add(DocumentQuarantine(doc_type="judgment", doc_id=hidden.id, reason="test", run_id="r1"))
    await db.commit()

    items = client.get("/admin/corpus/judgments", params={"q": "Admin"}, headers=admin_headers).json()["items"]
    citations = {i["citation"] for i in items}
    assert "PLD 2022 SC 1" in citations
    assert "PLD 2022 SC 9" not in citations

    detail = client.get(f"/admin/corpus/judgments/{hidden.id}", headers=admin_headers).json()
    assert detail["is_quarantined"] is True
    assert detail["citation"] == "PLD 2022 SC 9"


@pytest.mark.asyncio
async def test_admin_corpus_statute_list_excludes_quarantined(db, client, admin_headers):
    st = Statute(name="Admin Hidden Statute", short_name="AHS", year_enacted=2022)
    db.add(st)
    await db.flush()
    db.add(DocumentQuarantine(doc_type="statute", doc_id=st.id, reason="test", run_id="r1"))
    await db.commit()

    items = client.get("/admin/corpus/statutes", params={"q": "Hidden"}, headers=admin_headers).json()["items"]
    assert all(i["name"] != "Admin Hidden Statute" for i in items)
    detail = client.get(f"/admin/corpus/statutes/{st.id}", headers=admin_headers).json()
    assert detail["is_quarantined"] is True
