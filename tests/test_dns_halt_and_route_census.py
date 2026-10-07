"""A failed DNS lookup is a passing fault, not a block (slot 1 halted for good on 30 September 2026),
and the hourly census of the routes the authenticated dashboard offers."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from scraper import pls_navigation, security
from scraper.auth.session_manager import BrowserDisconnected, PlaywrightBrowser
from scraper.models import BrowserSessionSlot, Notification
from scraper.pls_navigation import dashboard_route_census, log_dashboard_route_census
from scraper.security import DNSUnavailable, ExplicitBlock, URLPolicyError, check_url_policy
from scraper.tasks.login_recovery import recover_slot
from tests.fixtures import BrowserScript
from tests.test_auth_playwright import _activate

PLS = "www.pakistanlawsite.com"
DNS_HALT = f"url_policy: host {PLS} resolves to a private, loopback, link-local or metadata address"


def _failing_resolver(host):
    raise OSError("Temporary failure in name resolution")


def test_failed_lookup_is_dns_unavailable_but_private_address_is_not():
    with pytest.raises(DNSUnavailable):
        check_url_policy(f"https://{PLS}/Login/Check", [PLS], resolver=_failing_resolver)
    with pytest.raises(DNSUnavailable):
        check_url_policy(f"https://{PLS}/Login/Check", [PLS], resolver=lambda h: [])
    with pytest.raises(URLPolicyError) as private:
        check_url_policy(f"https://{PLS}/Login/Check", [PLS], resolver=lambda h: ["10.0.0.7"])
    assert not isinstance(private.value, DNSUnavailable)
    assert check_url_policy(f"https://{PLS}/Login/Check", [PLS], resolver=lambda h: ["8.8.8.8"]).startswith("https://")


async def test_browser_turns_failed_lookup_into_a_reconnect_not_a_block(monkeypatch):
    browser = PlaywrightBrowser({}, 1, base_url=f"https://{PLS}", allow_private_for_tests=False)
    monkeypatch.setattr(security, "_default_resolver", _failing_resolver)
    with pytest.raises(BrowserDisconnected):
        await browser.goto(f"https://{PLS}/Login/Check")
    with pytest.raises(BrowserDisconnected):
        await browser.download(f"https://{PLS}/file.pdf")
    monkeypatch.setattr(security, "_default_resolver", lambda h: ["127.0.0.1"])
    with pytest.raises(ExplicitBlock):
        await browser.goto(f"https://{PLS}/Login/Check")


async def _halted_slot(db, source, reason):
    mgr = await _activate(db, source, slots=(1,))
    slot = (await db.execute(select(BrowserSessionSlot).where(BrowserSessionSlot.source_name == "PakistanLawSite", BrowserSessionSlot.slot_number == 1))).scalars().first()
    slot.state = "HALTED"
    slot.state_reason = reason
    slot.halted_at = datetime.now(timezone.utc)
    await db.commit()
    return mgr, slot


async def test_dns_halted_slot_is_released_once_the_host_resolves_publicly(db, login_source, monkeypatch):
    mgr, slot = await _halted_slot(db, login_source, DNS_HALT)
    monkeypatch.setattr(security, "_default_resolver", lambda h: ["104.21.5.9"])
    result = await recover_slot(db, mgr, slot, browser_factory=BrowserScript().factory())
    await db.commit()
    assert "scheduled" in result  # back in the ordinary recovery path: verify, then sign in again
    assert slot.state == "NEEDS_HUMAN_LOGIN" and slot.halted_at is None
    assert "failed DNS lookup" in slot.state_reason
    codes = [n.code for n in (await db.execute(select(Notification).where(Notification.source_name == "PakistanLawSite"))).scalars().all()]
    assert "SLOT_RELEASED" in codes


async def test_slot_stays_halted_while_the_host_resolves_privately_or_for_another_block(db, login_source, monkeypatch):
    mgr, slot = await _halted_slot(db, login_source, DNS_HALT)
    monkeypatch.setattr(security, "_default_resolver", lambda h: ["192.168.1.4"])
    result = await recover_slot(db, mgr, slot, browser_factory=BrowserScript().factory())
    assert result == {"skipped": "HALTED: host still does not resolve to public addresses"}
    assert slot.state == "HALTED"

    slot.state_reason = "block: HTTP 403"
    await db.commit()
    monkeypatch.setattr(security, "_default_resolver", lambda h: ["104.21.5.9"])
    assert await recover_slot(db, mgr, slot, browser_factory=BrowserScript().factory()) == {"skipped": "HALTED"}
    assert slot.state == "HALTED"


DASHBOARD = """<html><body>
<a href="/Login/CitationSearch">Citation search</a>
<a href="/Login/Search?token=SECRET123&amp;q=1">Search</a>
<a href="https://www.pakistanlawsite.com/Login/Statutes#top">Statutes</a>
<a href="https://elsewhere.example/Login/Other">offsite</a>
<a href="javascript:void(0)">menu</a>
<form method="post" action="/Login/AdvanceSearch">
  <input type="hidden" name="__RequestVerificationToken" value="SECRETVALUE">
  <input name="Keyword"><select name="Journal"><option>PLD</option></select>
</form>
<script>$.ajax({ url: '/Login/GetJudgments?x=1' }); var p = "/Login/GetStatuesSearch";</script>
</body></html>"""


def test_route_census_lists_same_site_paths_and_form_fields_never_values():
    census = dashboard_route_census(DASHBOARD, f"https://{PLS}")
    assert census["paths"] == [
        "/Login/AdvanceSearch",
        "/Login/CitationSearch",
        "/Login/GetJudgments",
        "/Login/GetStatuesSearch",
        "/Login/Search",
        "/Login/Statutes",
    ]
    assert census["forms"] == ["post /Login/AdvanceSearch fields=Journal,Keyword,__RequestVerificationToken"]
    flat = repr(census)
    assert "SECRET" not in flat and "elsewhere" not in flat


def test_route_census_is_logged_at_most_hourly(monkeypatch, caplog):
    monkeypatch.setattr(pls_navigation, "_route_census_logged_at", 0.0)
    caplog.set_level(logging.INFO, logger="scraper.pls_navigation")
    assert log_dashboard_route_census(DASHBOARD, f"https://{PLS}", now=1000.0) is not None
    assert log_dashboard_route_census(DASHBOARD, f"https://{PLS}", now=1000.0 + 1800) is None
    assert log_dashboard_route_census(DASHBOARD, f"https://{PLS}", now=1000.0 + 3601) is not None
    lines = [r.getMessage() for r in caplog.records if "route census" in r.getMessage()]
    assert len(lines) == 2 and "SECRET" not in lines[0] and "/Login/AdvanceSearch" in lines[0]


def test_route_census_survives_a_malformed_link():
    html = '<a href="http://[broken">x</a><a href="/Login/Search">s</a><form action="http://[x"><input name="q"></form>'
    census = dashboard_route_census(html, f"https://{PLS}")
    assert census["paths"] == ["/Login/Search"]
    assert census["forms"] == ["get (unparsable action) fields=q"]
