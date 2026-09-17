"""API / dashboard smoke tests (Section 14 originals): health, auth, guards, export, review, state machine."""

from __future__ import annotations

import pytest

import json

from sqlalchemy import select

from scraper.config import settings
from scraper.database import run_async
from scraper.extractors.hybrid_extractor import HybridExtractor
from scraper.fetchers import record_provenance, stage_judgment
from scraper.models import Judgment, QuarantineQueue, ScraperStaging
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks.promotion import promote_judgment_staging, promote_staging_records
from tests.fixtures import judgment_html


def test_health_and_dashboard(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["db_connected"] and h["redis_connected"]
    assert h["read_only_role"]["status"] == "CONFIGURED" and h["read_only_role"]["exists"]
    assert "PLS_SUBSCRIBED_REPORTERS" in h["not_configured"]
    d = client.get("/dashboard")
    assert d.status_code == 200 and "Human login" in d.text and "Check viewer" in d.text and "Review queue" in d.text and "Archive storage" in d.text
    for secret in ("test-admin-key", settings.ENCRYPTION_KEY, "readerpw", "writerpw"):
        assert secret not in json.dumps(h) and secret not in d.text


def test_admin_routes_require_key(client, admin_headers):
    for path in ("/admin/sources", "/admin/coverage", "/admin/review", "/admin/archive", "/admin/scrapegraph/status", "/admin/scrapegraph/usage", "/admin/sessions/PakistanLawSite", "/admin/jobs", "/admin/notifications", "/admin/errors"):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=admin_headers).status_code == 200, path
    assert client.get("/admin/sources", headers={"X-API-Key": "wrong"}).status_code == 401


def test_sources_view_has_no_credential_card_and_shows_slots(client, admin_headers):
    rows = client.get("/admin/sources", headers=admin_headers).json()
    assert len(rows) == 18
    pls = next(r for r in rows if r["source_name"] == "PakistanLawSite")
    assert pls["access_method"] == "login_session" and len(pls["slots"]) == 2 and pls["state"] == "PAUSED"
    assert "credential" not in json.dumps(pls).lower()
    assert "password" not in json.dumps(rows).lower()
    assert {"LahoreHighCourt", "PeshawarHighCourt", "BalochistanHighCourt", "IslamabadHighCourt", "AJKHighCourt", "FederalShariatCourt", "GazetteOfPakistan", "PunjabAssembly"} <= {r["source_name"] for r in rows}


def test_extraction_settings_and_guards(client, admin_headers):
    r = client.post("/admin/sources/PakistanLawSite/extraction-settings", json={"extraction_mode": "scrapegraph_managed"}, headers=admin_headers)
    assert r.status_code == 422
    r = client.post("/admin/sources/PakistanLawSite/extraction-settings", json={"crawl_allowed": True}, headers=admin_headers)
    assert r.status_code == 422
    r = client.post("/admin/sources/SupremeCourt/extraction-settings", json={"extraction_mode": "scrapegraph_local", "extraction_min_confidence": 0.9, "ai_extract_enabled": False}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["extraction"]["mode"] == "scrapegraph_local" and r.json()["extraction"]["effective_engine"] == "deterministic"


def test_halted_source_needs_admin_review_to_re_enable(client, admin_headers):
    client.post("/admin/sources/SupremeCourt/state", json={"action": "pause"}, headers=admin_headers)
    assert client.get("/admin/sources/SupremeCourt/status", headers=admin_headers).json()["state"] == "PAUSED"
    client.post("/admin/sources/SupremeCourt/state", json={"action": "resume"}, headers=admin_headers)
    # simulate a block
    from scraper.database import SessionLocal
    import asyncio

    async def halt():
        async with SessionLocal() as db:
            from scraper.models import ScraperSource

            s = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SupremeCourt"))).scalars().first()
            s.state, s.requires_admin_review = "HALTED", True
            await db.commit()

    run_async(halt())
    assert client.post("/admin/sources/SupremeCourt/trigger", headers=admin_headers).status_code == 409
    assert client.post("/admin/sources/SupremeCourt/state", json={"action": "resume"}, headers=admin_headers).status_code == 409
    assert client.post("/admin/sources/SupremeCourt/state", json={"action": "re_enable"}, headers=admin_headers).status_code == 422
    r = client.post("/admin/sources/SupremeCourt/state", json={"action": "re_enable", "reviewed_by": "Advocate", "reason": "false positive"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["state"] == "ACTIVE"


def test_login_trigger_requires_chambers_and_active_slot(client, admin_headers, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "cloud")
    assert client.post("/admin/sources/PakistanLawSite/trigger", headers=admin_headers).status_code == 409
    s = client.get("/admin/sessions/PakistanLawSite", headers=admin_headers).json()
    assert s["login_scraping_permitted"] is False and [x["state"] for x in s["slots"]] == ["EMPTY", "EMPTY"]
    assert client.post("/admin/sessions/PakistanLawSite/slots/1/resume", headers=admin_headers).status_code == 409


async def _seed_judgments(access_method="public"):
    from scraper.database import SessionLocal
    from scraper.models import ScraperSource

    async with SessionLocal() as db:
        src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == ("PakistanLawSite" if access_method == "login_session" else "SupremeCourt")))).scalars().first()
        html = judgment_html("PLD 2024 SC 701" if access_method == "public" else "PLD 2024 SC 702")
        prov = await record_provenance(db, source=src, url=f"http://127.0.0.1/{access_method}", content=html.encode(), content_kind="html")
        st = await stage_judgment(db, source=src, prov=prov, raw_html=html, raw_text=clean_html(html), url=prov.source_url)
        o = await HybridExtractor(db, src).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
        st.reconciled_json, st.status, st.confidence_score = o.data, "extracted", o.confidence
        await promote_judgment_staging(db, st)
        await db.commit()


def test_export_excludes_login_session_full_text_without_confirmation(client, admin_headers, monkeypatch):
    import asyncio

    run_async(_seed_judgments("public"))
    run_async(_seed_judgments("login_session"))
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true").text.splitlines()]
    by = {r["canonical_citation"]: r for r in rows}
    assert "The appellant" in by["PLD 2024 SC 701"]["full_text"]
    assert by["PLD 2024 SC 702"]["full_text"].startswith("[EXCLUDED")
    # admin key alone is not enough; confirmation header and deployment flag are required
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true", headers=admin_headers).text.splitlines()]
    assert {r["canonical_citation"]: r for r in rows}["PLD 2024 SC 702"]["full_text"].startswith("[EXCLUDED")
    monkeypatch.setattr(settings, "EXPORT_LOGIN_SESSION_FULL_TEXT", True)
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true", headers={**admin_headers, "X-Admin-Confirm": "export-login-session-full-text"}).text.splitlines()]
    assert "The appellant" in {r["canonical_citation"]: r for r in rows}["PLD 2024 SC 702"]["full_text"]
    csv_text = client.get("/export/judgments?format=csv").text
    assert "PLD 2024 SC 701" in csv_text and "The appellant" not in csv_text


def test_review_queue_resolve_and_check_viewer(client, admin_headers):
    import asyncio

    async def seed():
        from scraper.database import SessionLocal
        from scraper.models import ScraperSource

        async with SessionLocal() as db:
            src = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "SupremeCourt"))).scalars().first()
            html = "<html><body><p>No citation here, just a page about nothing in particular with enough words.</p></body></html>"
            prov = await record_provenance(db, source=src, url="http://127.0.0.1/q", content=html.encode(), content_kind="html")
            st = await stage_judgment(db, source=src, prov=prov, raw_html=html, raw_text=clean_html(html), url=prov.source_url)
            o = await HybridExtractor(db, src, provenance_id=prov.id, staging_id=st.id).extract_judgment(html=html, text=st.raw_text, content_hash=prov.content_hash)
            st.reconciled_json, st.deterministic_json, st.confidence_score = o.data, o.deterministic_json, o.confidence
            st.status = "quarantined" if o.quarantine else "extracted"
            st.quarantine_reason = o.quarantine_reason
            await db.commit()
            await promote_staging_records()
            return str(st.id)

    staging_id = run_async(seed())
    q = client.get("/admin/review", headers=admin_headers).json()
    assert len(q) == 1 and q[0]["staging_id"] == staging_id and "citation" in q[0]["reason"]
    chk = client.get(f"/admin/check/{staging_id}", headers=admin_headers).json()
    assert chk["status"] == "quarantined" and chk["raw_text"] and chk["deterministic"] and chk["audit"]
    r = client.post(f"/admin/review/{q[0]['id']}/resolve", json={"resolution": "rejected", "reviewer": "Advocate", "notes": "not a judgment"}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["result"] == "rejected"
    assert client.get("/admin/review", headers=admin_headers).json() == []
    assert client.get("/admin/review?reviewed=true", headers=admin_headers).json()[0]["resolution"] == "rejected"


def test_scrapegraph_status_and_test_endpoint_guards(client, admin_headers):
    s = client.get("/admin/scrapegraph/status", headers=admin_headers).json()
    assert s["managed"]["api_key"] == "NOT CONFIGURED" and s["managed"]["stealth_allowed"] is False and s["local"]["status"] == "NOT CONFIGURED"
    assert s["public_test_url"] == "NOT CONFIGURED"
    assert client.post("/admin/scrapegraph/test-public", headers=admin_headers).status_code == 409
    assert client.get("/admin/coverage", headers=admin_headers).json()["volume_end_gap"] == 40


def test_archive_target_config_is_encrypted_and_never_echoed(client, admin_headers):
    r = client.post("/admin/archive/targets", json={"name": "s3", "target_type": "s3_compatible", "config": {"bucket": "b", "access_key": "AKIAAAAAAAAAAAAAAAAA", "secret_key": "verysecretvalue12345"}}, headers=admin_headers)
    assert r.status_code == 200 and r.json()["config"] == "CONFIGURED"
    body = client.get("/admin/archive", headers=admin_headers).text
    assert "verysecretvalue" not in body and "AKIAAAAA" not in body
    assert client.post("/admin/archive/targets", json={"name": "x", "target_type": "ftp"}, headers=admin_headers).status_code == 422


def test_login_stream_websocket_requires_key_and_open_session(client, admin_headers):
    """The human-login stream is a WebSocket: a wrong key is refused before accept, a right key with
    no login session open closes with 4404, and neither path may raise inside the server."""
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises(WebSocketDisconnect) as bad:
        with client.websocket_connect("/admin/sessions/PakistanLawSite/login/stream?key=wrong"):
            pass
    assert bad.value.code == 4401
    key = admin_headers["X-API-Key"]
    with pytest.raises(WebSocketDisconnect) as none_open:
        with client.websocket_connect(f"/admin/sessions/PakistanLawSite/login/stream?key={key}"):
            pass
    assert none_open.value.code == 4404
    with pytest.raises(WebSocketDisconnect) as via_header:
        with client.websocket_connect("/admin/sessions/PakistanLawSite/login/stream", headers=admin_headers):
            pass
    assert via_header.value.code == 4404

