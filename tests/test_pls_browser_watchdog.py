"""The browser watchdog: a PakistanLawSite operation that freezes is restarted on the same slot, from the same cursor."""

from __future__ import annotations

import asyncio

import pytest

from scraper.auth.session_manager import BrowserDisconnected, ContinuityRunner
from tests.fixtures import BrowserScript, judgment_html
from tests.test_auth_playwright import _activate


async def test_a_frozen_operation_is_restarted_on_the_same_slot_from_the_same_cursor(db, login_source):
    mgr = await _activate(db, login_source, slots=(1, 2))
    sc = BrowserScript()
    url = "https://www.pakistanlawsite.com/case/11"
    sc.page(("goto", url), judgment_html("PLD 2024 SC 11"))
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    runner = ContinuityRunner(mgr, sc.factory(), sleep=fake_sleep)
    cursor = {"page_no": 11}
    attempts = []

    async def op(browser):
        attempts.append((browser.slot_number, dict(cursor)))
        if len(attempts) == 1:
            await asyncio.sleep(3600)  # the browser froze: no answer, no error
        return await browser.goto(url)

    page = await runner.run(op, timeout=0.2)
    assert page.status == 200
    assert attempts == [(1, cursor), (1, cursor)]  # fresh browser, same slot, same cursor
    assert slept == [runner.reconnect_seconds]
    assert (await mgr.slot(1)).reconnect_count == 1


async def test_an_operation_frozen_twice_is_handed_to_the_alternate_slot(db, login_source):
    mgr = await _activate(db, login_source, slots=(1, 2))
    sc = BrowserScript()
    url = "https://www.pakistanlawsite.com/case/12"
    sc.page(("goto", url), judgment_html("PLD 2024 SC 12"))

    async def nosleep(_s):
        return None

    runner = ContinuityRunner(mgr, sc.factory(), sleep=nosleep)
    slots = []

    async def op(browser):
        slots.append(browser.slot_number)
        if browser.slot_number == 1:
            await asyncio.sleep(3600)
        return await browser.goto(url)

    page = await runner.run(op, timeout=0.2)
    assert page.status == 200 and slots == [1, 1, 2]


async def test_a_healthy_operation_is_never_interrupted(db, login_source):
    mgr = await _activate(db, login_source, slots=(1,))
    sc = BrowserScript()
    url = "https://www.pakistanlawsite.com/case/13"
    sc.page(("goto", url), judgment_html("PLD 2024 SC 13"))
    runner = ContinuityRunner(mgr, sc.factory())

    async def op(browser):
        await asyncio.sleep(0.05)
        return await browser.goto(url)

    assert (await runner.run(op, timeout=5)).status == 200
    assert runner.reconnects == 0


async def test_zero_disables_the_watchdog(db, login_source):
    mgr = await _activate(db, login_source, slots=(1,))

    async def nosleep(_s):
        return None

    runner = ContinuityRunner(mgr, BrowserScript().factory(), sleep=nosleep)

    async def op(_browser):
        raise BrowserDisconnected("real disconnect")

    with pytest.raises(BrowserDisconnected):
        await runner.run(op, timeout=0)
