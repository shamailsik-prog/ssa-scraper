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


def test_health_and_dashboard(client, admin_headers):
    public = client.get("/health").json()
    assert public["status"] == "ok" and public["db_connected"] and public["redis_connected"]
    assert "not_configured" not in public
    assert "sources" not in public
    assert "read_only_role" not in public
    h = client.get("/health", headers=admin_headers).json()
    assert h["read_only_role"]["status"] == "CONFIGURED" and h["read_only_role"]["exists"]
    assert "PLS_SUBSCRIBED_REPORTERS" in h["not_configured"]
    d = client.get("/dashboard")
    assert d.status_code == 200 and "Overview" in d.text and "Corpus viewer" in d.text and "Browse and search" in d.text and "Record detail" in d.text
    assert "Human login" in d.text and "Review queue" in d.text and "Sources" in d.text
    for secret in ("test-admin-key", settings.ENCRYPTION_KEY, "readerpw", "writerpw"):
        assert secret not in json.dumps(public) and secret not in json.dumps(h) and secret not in d.text


def test_admin_routes_require_key(client, admin_headers):
    for path in (
        "/admin/sources",
        "/admin/sources/PakistanLawSite/status",
        "/admin/coverage",
        "/admin/review",
        "/admin/archive",
        "/admin/scrapegraph/status",
        "/admin/scrapegraph/usage",
        "/admin/sessions/PakistanLawSite",
        "/admin/jobs",
        "/admin/notifications",
        "/admin/errors",
        "/admin/corpus/summary",
        "/export/judgments",
        "/export/statutes",
    ):
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=admin_headers).status_code == 200, path
    assert client.get("/admin/sources", headers={"X-API-Key": "wrong"}).status_code == 401


def test_sources_view_has_no_credential_card_and_shows_slots(client, admin_headers):
    rows = client.get("/admin/sources", headers=admin_headers).json()
    assert len(rows) == 22
    pls = next(r for r in rows if r["source_name"] == "PakistanLawSite")
    assert pls["access_method"] == "login_session" and len(pls["slots"]) == 2 and pls["state"] == "PAUSED"
    assert "credential" not in json.dumps(pls).lower()
    assert "password" not in json.dumps(rows).lower()
    assert {
        "LahoreHighCourt",
        "PeshawarHighCourt",
        "BalochistanHighCourt",
        "IslamabadHighCourt",
        "AJKHighCourt",
        "AJKSupremeCourt",
        "SupremeAppellateCourtGB",
        "FederalShariatCourt",
        "GazetteOfPakistan",
        "PunjabAssembly",
        "AJKAssembly",
        "GBAssembly",
    } <= {r["source_name"] for r in rows}


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
    assert client.get("/export/judgments?format=jsonl&include_full_text=true").status_code == 401
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true", headers=admin_headers).text.splitlines()]
    by = {r["canonical_citation"]: r for r in rows}
    assert "The appellant" in by["PLD 2024 SC 701"]["full_text"]
    assert by["PLD 2024 SC 702"]["full_text"].startswith("[EXCLUDED")
    # admin key alone is not enough; confirmation header and deployment flag are required
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true", headers=admin_headers).text.splitlines()]
    assert {r["canonical_citation"]: r for r in rows}["PLD 2024 SC 702"]["full_text"].startswith("[EXCLUDED")
    monkeypatch.setattr(settings, "EXPORT_LOGIN_SESSION_FULL_TEXT", True)
    rows = [json.loads(l) for l in client.get("/export/judgments?format=jsonl&include_full_text=true", headers={**admin_headers, "X-Admin-Confirm": "export-login-session-full-text"}).text.splitlines()]
    assert "The appellant" in {r["canonical_citation"]: r for r in rows}["PLD 2024 SC 702"]["full_text"]
    csv_text = client.get("/export/judgments?format=csv", headers=admin_headers).text
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



def test_status_page_and_numbers_need_no_key_and_carry_no_secrets(client, admin_headers):
    """/status and /status.json are read-only and key-free (numbers, states, archive targets,
    Google Drive configured or not); nothing in them is a secret, a URL or judgment text."""
    page = client.get("/status")
    assert page.status_code == 200 and "Corpus status" in page.text and "/manifest.webmanifest" in page.text
    data = client.get("/status.json").json()
    assert set(data["totals"]) >= {"judgments", "citations", "statutes", "statute_sections", "instruments", "review_queue_open"}
    assert "judgments_promoted_today" in data["movement"]
    pls = next(s for s in data["sources"] if s["source"] == "PakistanLawSite")
    assert pls["access"] == "login_session" and len(pls["slots"]) == 2 and pls["pacing_today"]["limit_per_hour"] == 300
    assert data["archive"]["google_drive"]["configured"] is False and "Connect Google Drive" in data["archive"]["google_drive"]["note"]
    blob = json.dumps(data).lower()
    assert "password" not in blob and "storage_state" not in blob and "config_encrypted" not in blob
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200 and manifest.json()["start_url"] == "/status"
    assert client.get("/static/icon.svg").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/dashboard").text.count('rel="manifest"') == 1


def test_connect_google_drive_flow_stores_encrypted_refresh_token_and_creates_folder(client, admin_headers, monkeypatch):
    """Connect returns Google's consent URL bound to a single-use state; the callback (no admin key,
    the state is the credential) exchanges the code, creates the Drive folder and stores the client
    secret and refresh token Fernet-encrypted in a google_drive target that /status reports."""
    from urllib.parse import parse_qs, urlsplit

    from scraper.routers import archive as archive_router

    r = client.post("/admin/archive/google-drive/connect", json={"client_id": "1234567890-abc.apps.googleusercontent.com", "client_secret": "GOCSPX-supersecret", "mirror_login_session_rows": True}, headers=admin_headers)
    assert r.status_code == 200, r.text
    url = r.json()["authorization_url"]
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["1234567890-abc.apps.googleusercontent.com"] and q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["scope"] == ["https://www.googleapis.com/auth/drive.file"] and q["redirect_uri"][0].endswith("/oauth/google-drive/callback")
    state = q["state"][0]
    assert client.post("/admin/archive/google-drive/connect", json={"client_id": "x", "client_secret": "y"}).status_code == 401
    assert client.get("/oauth/google-drive/callback?state=unknown&code=abc").status_code == 400

    seen = {}

    async def fake_exchange(*, code, client_id, client_secret, redirect_uri):
        seen.update(code=code, client_id=client_id, client_secret=client_secret, redirect_uri=redirect_uri)
        return {"access_token": "ya29.access", "refresh_token": "1//refresh-token-value", "expires_in": 3599}

    async def fake_folder(access_token):
        seen["access_token"] = access_token
        return "folder-id-123"

    monkeypatch.setattr(archive_router, "_exchange_code", fake_exchange)
    monkeypatch.setattr(archive_router, "_create_root_folder", fake_folder)
    done = client.get(f"/oauth/google-drive/callback?state={state}&code=4/0AbCd")
    assert done.status_code == 200 and "Google Drive connected" in done.text
    assert seen["code"] == "4/0AbCd" and seen["client_secret"] == "GOCSPX-supersecret" and seen["access_token"] == "ya29.access"
    assert client.get(f"/oauth/google-drive/callback?state={state}&code=again").status_code == 400  # single use
    listing = client.get("/admin/archive", headers=admin_headers)
    body = listing.text
    target = next(t for t in listing.json()["targets"] if t["name"] == "google_drive")
    assert target["type"] == "google_drive" and target["enabled"] is True
    assert "refresh-token-value" not in body and "GOCSPX" not in body
    public = client.get("/status.json").json()
    assert public["archive"]["google_drive"]["configured"] is True
    assert "refresh-token-value" not in json.dumps(public) and "GOCSPX" not in json.dumps(public)
    from scraper.models import ArchiveTarget
    from scraper.storage.archive import target_config
    from scraper.storage.adapters import google_drive_credentials
    from sqlalchemy import select
    from scraper.database import SessionLocal, run_async

    async def _cfg():
        async with SessionLocal() as db:
            t = (await db.execute(select(ArchiveTarget).where(ArchiveTarget.name == "google_drive"))).scalars().first()
            return target_config(t)

    cfg = run_async(_cfg())
    assert cfg["refresh_token"] == "1//refresh-token-value" and cfg["folder_id"] == "folder-id-123"
    creds = google_drive_credentials(cfg)
    assert creds.refresh_token == "1//refresh-token-value" and creds.client_id == "1234567890-abc.apps.googleusercontent.com"
