"""Spot checks (operator request, 24 September 2026): a few promoted records re-fetched from their
source and compared with what the corpus holds; results as spot_check rows and on /status."""

from __future__ import annotations

import hashlib

import redis.asyncio as aioredis
from sqlalchemy import select

from scraper.auth.session_manager import PageResult
from scraper.config import settings
from scraper.extractors.judgment_guards import strip_leading_judgment_chrome
from scraper.fetchers import canonical_text_hash
from scraper.models import BrowserSessionSlot, Judgment, ScraperSource, SourceProvenance, SpotCheck, Statute, StatuteSection, StatuteSectionVersion
from scraper.parsers.text_cleaner import clean_html
from scraper.tasks.spot_check import check_judgments, check_statute_sections
from tests.fixtures import LOGIN_PAGE, BrowserScript, judgment_html
from tests.test_auth_playwright import _activate, _nosleep


def _stored_text_for(html: str) -> str:
    return strip_leading_judgment_chrome(clean_html(html))


async def _judgment(db, citation: str, url: str, full_text: str) -> Judgment:
    j = Judgment(canonical_citation=citation, case_title=f"{citation} title", source_name="PakistanLawSite", source_url=url, full_text=full_text, full_text_hash=canonical_text_hash(full_text), reporter="PLD", year=2024)
    db.add(j)
    await db.flush()
    return j


async def test_judgment_spot_check_matches_and_flags_a_changed_page(db, login_source):
    await _activate(db, login_source)
    same_url = "https://www.pakistanlawsite.com/case/9001"
    changed_url = "https://www.pakistanlawsite.com/case/9002"
    same_html = judgment_html("PLD 2024 SC 9001", title="Same Text Case")
    changed_html = judgment_html("PLD 2024 SC 9002", title="Changed Text Case")
    await _judgment(db, "PLD 2024 SC 9001", same_url, _stored_text_for(same_html))
    await _judgment(db, "PLD 2024 SC 9002", changed_url, _stored_text_for(changed_html) + " an extra paragraph the site no longer shows")
    await db.commit()
    sc = BrowserScript()
    sc.page(("goto", same_url), same_html)
    sc.page(("goto", changed_url), changed_html)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    results = await check_judgments(db, sample=5, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await db.commit()
    await r.aclose()
    by_label = {x["label"]: x for x in results if "label" in x}
    assert by_label["PLD 2024 SC 9001"]["result"] == "match"
    assert by_label["PLD 2024 SC 9002"]["result"] == "differs" and 0 < by_label["PLD 2024 SC 9002"]["similarity"] < 1
    rows = (await db.execute(select(SpotCheck).where(SpotCheck.kind == "judgment"))).scalars().all()
    assert {r.label: r.result for r in rows} == {"PLD 2024 SC 9001": "match", "PLD 2024 SC 9002": "differs"}
    assert (login_source.config_json or {}).get("pacing", {}).get("day_pages") == 2  # charged like any other page


async def test_judgment_spot_check_login_page_ends_the_run_and_marks_the_slot(db, login_source):
    await _activate(db, login_source)
    url = "https://www.pakistanlawsite.com/case/9003"
    await _judgment(db, "PLD 2024 SC 9003", url, "some stored text")
    await db.commit()
    sc = BrowserScript()
    sc.routes[("goto", url)] = lambda b: PageResult(url="https://www.pakistanlawsite.com/Login/MainPage", html=LOGIN_PAGE, status=200)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    results = await check_judgments(db, sample=3, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await db.commit()
    await r.aclose()
    assert results and results[0]["result"] == "login_required"
    slot = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.slot_number == 1))).scalars().first()
    assert slot.state == "NEEDS_HUMAN_LOGIN"
    row = (await db.execute(select(SpotCheck))).scalars().first()
    assert row.result == "login_required" and "LoginRequired" in row.detail


async def test_judgment_spot_check_skips_when_the_source_is_not_active(db, login_source):
    login_source.state = "PAUSED"
    await db.commit()
    assert (await check_judgments(db, sample=3))[0]["skipped"].startswith("PakistanLawSite is PAUSED")


async def test_statute_section_spot_check_finds_or_misses_the_stored_text(db, fixture_server):
    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    source.allow_list = list(source.allow_list or []) + ["127.0.0.1", "localhost"]
    settings_backup = settings.SCRAPER_RESPECT_ROBOTS
    stored_text = "Whoever, with intent to cause, or knowing that he is likely to cause, wrongful loss or damage to the public or to any person, causes the destruction of any property."
    page = f"<html><body><a href='/x'>Menu</a><h1>Pakistan Penal Code</h1><h2>425. Mischief</h2><p>{stored_text}</p><h2>426. Punishment</h2><p>Whoever commits mischief shall be punished.</p></body></html>"
    url_ok = fixture_server.add("/ppc/425", page)
    url_changed = fixture_server.add("/ppc/426", page.replace("shall be punished", "shall be fined only"))
    fixture_server.add("/robots.txt", "User-agent: *\nAllow: /\n")
    st = Statute(name="Pakistan Penal Code", short_name="PPC", source_name="PakistanCode")
    db.add(st)
    await db.flush()
    rows = []
    for num, title, text, url in (("425", "Mischief", stored_text, url_ok), ("426", "Punishment for mischief", "Whoever commits mischief shall be punished.", url_changed)):
        prov = SourceProvenance(source_name="PakistanCode", access_method="public", source_url=url, content_hash=hashlib.sha256(url.encode()).hexdigest(), content_kind="html")
        db.add(prov)
        await db.flush()
        sec = StatuteSection(statute_id=st.id, section_number=num, section_title=title)
        db.add(sec)
        await db.flush()
        ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text=text, text_hash=canonical_text_hash(text), source_provenance_id=prov.id)
        db.add(ver)
        await db.flush()
        sec.current_version_id = ver.id
        rows.append(sec)
    await db.commit()
    results = await check_statute_sections(db, sample=5, allow_private_for_tests=True)
    await db.commit()
    by_label = {x["label"]: x["result"] for x in results if "label" in x}
    assert by_label == {"PPC s. 425": "match", "PPC s. 426": "differs"}
    checks = (await db.execute(select(SpotCheck).where(SpotCheck.kind == "statute_section"))).scalars().all()
    assert {c.label: c.result for c in checks} == by_label
    assert all(c.source_url for c in checks)
    assert settings.SCRAPER_RESPECT_ROBOTS == settings_backup


def test_status_page_reports_spot_checks(client, admin_headers):
    data = client.get("/status.json").json()
    sp = data["spot_checks"]
    assert sp["every_seconds"] == settings.SPOT_CHECK_SCHEDULE_SECONDS and sp["per_run"] == {"judgments": settings.SPOT_CHECK_JUDGMENTS, "statute_sections": settings.SPOT_CHECK_STATUTES}
    assert "recent" in sp and "last_24h" in sp
    page = client.get("/status").text
    assert "Spot checks" in page
