"""Spot checks (operator request, 24 September 2026): a few promoted records re-fetched from their
source and compared with what the corpus holds; results as spot_check rows and on /status."""

from __future__ import annotations

import hashlib
import json

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
    # A page whose section text is written by its own script: a bare download never carries it,
    # the browser's rendered page does (the operator's reason for comparing on the browser).
    scripted_text = "No person shall be punished twice for the same offence under this Code."
    url_scripted = fixture_server.add(
        "/ppc/403",
        "<html><body><h2>403. Double jeopardy</h2><div id='body'></div><script>document.getElementById('body').textContent = "
        + repr(scripted_text)
        + ";</script></body></html>",
    )
    from tests.fixtures import text_pdf_bytes

    pdf_text = "428. Mischief by killing or maiming animal. Whoever commits mischief by killing an animal shall be punished."
    url_pdf = fixture_server.add("/ppc/428.pdf", text_pdf_bytes(pdf_text), content_type="application/pdf")
    fixture_server.add("/robots.txt", "User-agent: *\nAllow: /\n")
    st = Statute(name="Pakistan Penal Code", short_name="PPC", source_name="PakistanCode")
    db.add(st)
    await db.flush()
    rows = []
    for num, title, text, url in (
        ("425", "Mischief", stored_text, url_ok),
        ("426", "Punishment for mischief", "Whoever commits mischief shall be punished.", url_changed),
        ("403", "Double jeopardy", scripted_text, url_scripted),
        ("428", "Mischief by killing", "Whoever commits mischief by killing an animal shall be punished.", url_pdf),
    ):
        prov = SourceProvenance(source_name="PakistanCode", access_method="public", source_url=url, content_hash=hashlib.sha256(url.encode()).hexdigest(), content_kind="pdf" if url.endswith(".pdf") else "html")
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
    async def no_sleep(_seconds):
        return None

    results = await check_statute_sections(db, sample=5, allow_private_for_tests=True, sleep=no_sleep)
    await db.commit()
    by_label = {x["label"]: x["result"] for x in results if "label" in x}
    assert by_label == {"PPC s. 425": "match", "PPC s. 426": "differs", "PPC s. 403": "match", "PPC s. 428": "match"}
    checks = (await db.execute(select(SpotCheck).where(SpotCheck.kind == "statute_section"))).scalars().all()
    assert {c.label: c.result for c in checks} == by_label
    assert all(c.source_url for c in checks)
    details = {c.label: c.detail for c in checks}
    assert "rendered page" in details["PPC s. 403"] and "PDF fetched by the browser" in details["PPC s. 428"]
    assert settings.SCRAPER_RESPECT_ROBOTS == settings_backup


def test_status_page_reports_spot_checks(client, admin_headers):
    data = client.get("/status.json").json()
    sp = data["spot_checks"]
    assert sp["every_seconds"] == settings.SPOT_CHECK_SCHEDULE_SECONDS and sp["per_run"] == {"judgments": settings.SPOT_CHECK_JUDGMENTS, "statute_sections": settings.SPOT_CHECK_STATUTES}
    assert "recent" in sp and "last_24h" in sp
    page = client.get("/status").text
    assert "Spot checks" in page


async def test_judgment_spot_check_never_switches_to_the_alternate_slot(db, login_source):
    """A bounce during a spot check marks the slot it used and stops; the alternate slot is
    neither opened nor touched (no continuity ladder for checks)."""
    await _activate(db, login_source, slots=(1, 2))
    await _judgment(db, "PLD 2024 SC 9004", "https://www.pakistanlawsite.com/case/9004", "text a")
    await _judgment(db, "PLD 2024 SC 9005", "https://www.pakistanlawsite.com/case/9005", "text b")
    await db.commit()
    sc = BrowserScript()
    for u in ("https://www.pakistanlawsite.com/case/9004", "https://www.pakistanlawsite.com/case/9005"):
        sc.routes[("goto", u)] = lambda b: PageResult(url="https://www.pakistanlawsite.com/Login/MainPage", html=LOGIN_PAGE, status=200)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    results = await check_judgments(db, sample=3, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await db.commit()
    await r.aclose()
    assert [x["result"] for x in results] == ["login_required"]
    slots = {s.slot_number: s.state for s in (await db.execute(select(BrowserSessionSlot))).scalars().all()}
    assert slots == {1: "NEEDS_HUMAN_LOGIN", 2: "ACTIVE"}
    assert all(entry[1] == 1 for entry in sc.log)  # every page was asked of slot 1 only
    assert (login_source.config_json or {}).get("current_slot") == 1  # not switched
    assert login_source.state == "ACTIVE"


async def test_judgment_match_requires_the_citation_on_the_page(db, login_source):
    await _activate(db, login_source)
    url = "https://www.pakistanlawsite.com/case/9006"
    html = judgment_html("PLD 2024 SC 9006", title="Identity Case")
    j = await _judgment(db, "PLD 2024 SC 9006", url, _stored_text_for(html))
    j.canonical_citation = "PLD 2024 SC 9999"  # the stored citation is not on the page
    await db.commit()
    sc = BrowserScript()
    sc.page(("goto", url), html)
    r = aioredis.from_url(settings.REDIS_URL)
    await r.delete("corpus:login_session_lock:PakistanLawSite")
    results = await check_judgments(db, sample=1, browser_factory=sc.factory(), redis_client=r, sleep=_nosleep)
    await db.commit()
    await r.aclose()
    assert results[0]["result"] == "differs"
    row = (await db.execute(select(SpotCheck))).scalars().first()
    assert "citation is not on the page" in row.detail


async def test_statute_spot_check_block_halts_the_source_and_partial_text_differs(db, fixture_server):
    from scraper.models import Notification

    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    source.allow_list = list(source.allow_list or []) + ["127.0.0.1", "localhost"]
    fixture_server.add("/robots.txt", "User-agent: *\nAllow: /\n")
    long_text = "Section text " + "word " * 300 + "END OF SECTION"
    url_partial = fixture_server.add("/ppc/500", f"<html><body><h2>500. Long</h2><p>{long_text[:600]} something else entirely</p></body></html>")
    url_blocked = fixture_server.add("/ppc/501", "<html><body><h1>Access denied</h1><p>Your account has been suspended for automated access.</p></body></html>", status=403)
    st = Statute(name="Test Code", short_name="TC", source_name="PakistanCode")
    db.add(st)
    await db.flush()

    async def add_section(num, text, url):
        prov = SourceProvenance(source_name="PakistanCode", access_method="public", source_url=url, content_hash=hashlib.sha256(url.encode()).hexdigest(), content_kind="html")
        db.add(prov)
        await db.flush()
        sec = StatuteSection(statute_id=st.id, section_number=num, section_title="t")
        db.add(sec)
        await db.flush()
        ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text=text, text_hash=canonical_text_hash(text), source_provenance_id=prov.id)
        db.add(ver)
        await db.flush()
        sec.current_version_id = ver.id

    # 1. Only the first part of a long section is on the page: not a match.
    await add_section("500", long_text, url_partial)
    await db.commit()
    results = await check_statute_sections(db, sample=5, allow_private_for_tests=True)
    await db.commit()
    assert {x["label"]: x["result"] for x in results if "label" in x} == {"TC s. 500": "differs"}
    # 2. A block halts the source, as the public pipeline would, and ends the run.
    await add_section("501", "Blocked section text.", url_blocked)
    await db.commit()
    # sampling is random: run until the blocked row is drawn (two rows, at most a few tries)
    for _ in range(12):
        results = await check_statute_sections(db, sample=1, allow_private_for_tests=True)
        await db.commit()
        if source.state == "HALTED":
            break
    assert source.state == "HALTED" and source.requires_admin_review
    assert any(x.get("result") == "unreachable" and "block" in (x.get("detail") or "") for x in results)
    codes = (await db.execute(select(Notification.code))).scalars().all()
    assert "SOURCE_HALTED" in codes


async def test_statute_spot_check_stale_links_redirects_and_robots_outages_never_halt(db, fixture_server, monkeypatch):
    """A removed PDF (404), a page that redirects outside the allow-list, and a robots.txt the site
    cannot serve are each recorded as unreachable and the run goes on; the source stays ACTIVE."""
    from scraper import security

    source = (await db.execute(select(ScraperSource).where(ScraperSource.source_name == "PakistanCode"))).scalars().first()
    source.allow_list = list(source.allow_list or []) + ["127.0.0.1"]
    fixture_server.add("/robots.txt", "User-agent: *\nAllow: /\n")
    url_gone = fixture_server.add("/ppc/gone.pdf", "not here", status=404, content_type="text/plain")
    url_redirect = fixture_server.add_redirect("/ppc/redirect", "http://localhost:9/elsewhere")  # off the allow-list
    st = Statute(name="Test Code", short_name="TC", source_name="PakistanCode")
    db.add(st)
    await db.flush()

    async def add_section(num, text, url):
        prov = SourceProvenance(source_name="PakistanCode", access_method="public", source_url=url, content_hash=hashlib.sha256(url.encode()).hexdigest(), content_kind="html")
        db.add(prov)
        await db.flush()
        sec = StatuteSection(statute_id=st.id, section_number=num, section_title="t")
        db.add(sec)
        await db.flush()
        ver = StatuteSectionVersion(section_id=sec.id, version_no=1, section_text=text, text_hash=canonical_text_hash(text), source_provenance_id=prov.id)
        db.add(ver)
        await db.flush()
        sec.current_version_id = ver.id

    await add_section("600", "Gone section text.", url_gone)
    await add_section("601", "Redirected section text.", url_redirect)
    await db.commit()

    async def no_sleep(_seconds):
        return None

    results = await check_statute_sections(db, sample=5, allow_private_for_tests=True, sleep=no_sleep)
    await db.commit()
    by_label = {x["label"]: x for x in results if "label" in x}
    assert by_label["TC s. 600"]["result"] == "unreachable" and "404" in by_label["TC s. 600"]["detail"]
    assert by_label["TC s. 601"]["result"] == "unreachable"
    assert source.state == "ACTIVE"
    checks = {c.label: c.detail for c in (await db.execute(select(SpotCheck).where(SpotCheck.kind == "statute_section"))).scalars().all()}
    assert "block" not in checks["TC s. 600"]

    # robots.txt answered with a server error: RFC 9309 treats it as a temporary disallow.
    security.reset_robots_cache()
    fixture_server.add("/robots.txt", "boom", status=503)
    results = await check_statute_sections(db, sample=5, allow_private_for_tests=True, sleep=no_sleep)
    await db.commit()
    security.reset_robots_cache()
    assert results and all(x.get("result") == "unreachable" and x.get("detail") == "robots unavailable" for x in results if "label" in x)
    assert source.state == "ACTIVE"


def test_status_spot_checks_carry_no_urls(client, admin_headers):
    data = client.get("/status.json").json()
    for row in data["spot_checks"]["recent"]:
        assert "source_url" not in row and "http" not in json.dumps(row).lower()
