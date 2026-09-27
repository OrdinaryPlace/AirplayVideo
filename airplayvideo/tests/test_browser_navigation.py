"""Uncertain or history navigation cannot reuse a stale requested URL."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.browser import Browser
from service.model import UserError, default_setup


@pytest.fixture
def browser(tmp_path):
    instance = Browser(tmp_path, None)
    instance.running = True
    instance.url = "https://example.com/prior"
    instance.start = AsyncMock()
    instance.companion = SimpleNamespace(call=AsyncMock(return_value={"ok": True}))
    return instance


@pytest.mark.asyncio
async def test_navigation_tracks_url_only_after_acknowledgement(browser):
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(action, **fields):
        assert action == "navigate"
        assert fields == {"url": "https://example.com/next"}
        entered.set()
        await release.wait()
        return {"ok": True}

    browser.companion.call.side_effect = call
    navigation = asyncio.create_task(browser.navigate("https://example.com/next", default_setup()))
    await entered.wait()
    assert browser.url == ""
    release.set()
    await navigation
    assert browser.url == "https://example.com/next"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [UserError("Browser controls disconnected"), asyncio.CancelledError()])
async def test_uncertain_navigation_invalidates_previous_url(browser, error):
    browser.companion.call.side_effect = error
    with pytest.raises(type(error)):
        await browser.navigate("https://example.com/next", default_setup())
    assert browser.url == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["back", "forward"])
@pytest.mark.parametrize("failed", [False, True])
async def test_history_navigation_invalidates_url_even_when_acknowledgement_fails(browser, action, failed):
    if failed:
        browser.companion.call.side_effect = UserError("Browser controls disconnected")
        with pytest.raises(UserError):
            await browser.control(action)
    else:
        await browser.control(action)
    browser.companion.call.assert_awaited_once_with(action)
    assert browser.url == ""


@pytest.mark.asyncio
async def test_reload_retains_requested_address(browser):
    await browser.control("reload")
    assert browser.url == "https://example.com/prior"


@pytest.mark.asyncio
async def test_rejected_url_does_not_invalidate_current_page(browser):
    with pytest.raises(UserError):
        await browser.navigate("about:blank", default_setup())
    browser.start.assert_not_awaited()
    browser.companion.call.assert_not_awaited()
    assert browser.url == "https://example.com/prior"
