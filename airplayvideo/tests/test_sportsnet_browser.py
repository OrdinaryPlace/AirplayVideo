"""Native autofill acts only on the owned fullscreen browser and never submits."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.browser import Browser
from service.model import UserError


RECT = {"x": 300, "y": 250, "width": 400, "height": 50,
        "viewport_width": 1920, "viewport_height": 1080}


@pytest.fixture
def browser(tmp_path):
    browser = Browser(tmp_path, None)
    browser.running = True
    browser.profile = "sportsnet"
    browser.chrome_process = SimpleNamespace(pid=123)
    browser.calls = []
    browser.selected = False
    browser.offer = True
    browser.focus = "11"
    browser.geometry = "X=0\nY=0\nWIDTH=1920\nHEIGHT=1080"

    async def native(*args, **_kwargs):
        browser.calls.append(args)
        if args[0] == "x11vnc":
            return "viewonly:1" if "viewonly" in args else ""
        if args[1] == "getactivewindow":
            return browser.focus
        if args[1] == "getwindowpid":
            return "123"
        if args[1] == "getwindowgeometry":
            return browser.geometry
        if args[1] == "key":
            browser.selected = browser.offer
        return ""

    async def call(_action, **_kwargs):
        return {"stage": "email", "filled": browser.selected,
                "rect": RECT, "next": "continue" if browser.selected else "autofill"}

    browser.native_command = AsyncMock(side_effect=native)
    browser.companion = SimpleNamespace(call=AsyncMock(side_effect=call))
    return browser


@pytest.mark.asyncio
async def test_native_saved_suggestion_uses_trusted_click_and_tab_without_submission(browser):
    result = await browser.sportsnet_autofill("email")
    assert result["filled"] is True
    assert ("xdotool", "mousemove", "500", "275", "click", "1") in browser.calls
    assert ("xdotool", "key", "--clearmodifiers", "Down", "Tab") in browser.calls
    assert not any("Return" in call for call in browser.calls)
    assert all(call.kwargs.get("operation") != "advance" for call in browser.companion.call.await_args_list)
    assert browser.calls[-1][-3:] == ("-R", "noviewonly", "-sync")


@pytest.mark.asyncio
async def test_absent_saved_suggestion_does_not_submit_or_repeat_keys(browser):
    browser.offer = False
    result = await browser.sportsnet_autofill("email")
    assert result["filled"] is False
    assert sum(call[:2] == ("xdotool", "key") for call in browser.calls) == 1
    assert all(call.kwargs.get("operation") != "advance" for call in browser.companion.call.await_args_list)


@pytest.mark.asyncio
async def test_challenge_or_already_filled_field_never_sends_native_input(browser):
    for result in ({"stage": "verification"}, {"stage": "password", "filled": True}):
        browser.calls.clear()
        browser.companion.call = AsyncMock(return_value=result)
        await browser.sportsnet_autofill("password")
        assert not any(call[:2] in {("xdotool", "key"), ("xdotool", "mousemove")} for call in browser.calls)
        assert browser.calls[-1][-3:] == ("-R", "noviewonly", "-sync")


@pytest.mark.asyncio
@pytest.mark.parametrize("geometry", ["X=1\nY=0\nWIDTH=1920\nHEIGHT=1080", "X=0\nY=0\nWIDTH=800\nHEIGHT=600"])
async def test_nonfullscreen_geometry_refuses_native_autofill(browser, geometry):
    browser.geometry = geometry
    with pytest.raises(UserError, match="fullscreen"):
        await browser.sportsnet_autofill("email")
    assert not any(call[:2] in {("xdotool", "key"), ("xdotool", "mousemove")} for call in browser.calls)


@pytest.mark.asyncio
async def test_owner_guard_is_rechecked_after_waiting_for_browser_control(browser):
    active = True

    def guard():
        if not active:
            raise UserError("cancelled fixture")

    await browser.control_lock.acquire()
    task = asyncio.create_task(browser.sportsnet_step("password", guard=guard))
    await asyncio.sleep(0)
    active = False
    browser.control_lock.release()
    with pytest.raises(UserError, match="cancelled fixture"):
        await task
    browser.companion.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_stop_after_native_field_click_prevents_later_autofill_keys(browser):
    active = True
    native = browser.native_command.side_effect

    async def stopping_native(*args, **kwargs):
        nonlocal active
        result = await native(*args, **kwargs)
        if args[:2] == ("xdotool", "mousemove"):
            active = False
        return result

    def guard():
        if not active:
            raise UserError("cancelled fixture")

    browser.native_command.side_effect = stopping_native
    with pytest.raises(UserError, match="cancelled fixture"):
        await browser.sportsnet_autofill("email", guard=guard)
    assert not any(call[:2] == ("xdotool", "key") for call in browser.calls)
    assert browser.calls[-1][-3:] == ("-R", "noviewonly", "-sync")


@pytest.mark.asyncio
async def test_status_projects_only_fixed_values_and_bounded_geometry(browser):
    browser.companion.call = AsyncMock(return_value={
        "stage": "password", "filled": True, "next": "submit", "video_time": float("nan"),
        "rect": {**RECT, "x": -10}, "account": "private fixture",
        "password": "private fixture", "url": "private fixture", "error": "private fixture"})
    result = await browser.sportsnet_status()
    assert not any(key in result for key in ("account", "password", "url", "error", "rect"))
    assert result["video_time"] is None and result["filled"] is True
    assert set(result) == {"stage", "filled", "next", "video_time", "target_matches",
                           "paused", "muted", "fullscreen", "acted"}


@pytest.mark.asyncio
async def test_invalid_provider_state_has_fixed_error_without_raw_content(browser):
    browser.companion.call = AsyncMock(return_value={"stage": "private fixture"})
    with pytest.raises(UserError, match="valid state") as failed:
        await browser.sportsnet_status()
    assert "private fixture" not in str(failed.value)
