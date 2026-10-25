"""Profile routing must preserve active playback, captures, and receiver state."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service.model import UserError, browser_profile
from test_lifecycle import A, B, setup


SPORTSNET = "https://watch.sportsnet.ca/"


@pytest.fixture
def profile_control(setup):
    store, control = setup
    browser = SimpleNamespace(profile="default", running=True, url="",
                              environment={"DISPLAY": ":42.0"}, close=AsyncMock(),
                              start=AsyncMock(), navigate=AsyncMock())
    async def navigate(url, *_):
        browser.profile, browser.url, browser.running = browser_profile(url), url, True
    browser.navigate.side_effect = navigate
    control.browser = browser
    control.changed = Mock()
    return store, control


def browser_request(url=SPORTSNET):
    return {"mode": "browser", "browser_source": "url", "url": url, "receivers": [A]}


@pytest.mark.parametrize("url,expected", [
    ("https://sportsnet.ca/", "sportsnet"),
    ("https://www.sportsnet.ca/", "sportsnet"),
    (SPORTSNET, "sportsnet"),
    ("https://ACCOUNT.SPORTSNET.CA:443/login", "sportsnet"),
    ("http://sportsnet.ca:8080/", "sportsnet"),
    ("https://sportsnet.ca.evil.invalid/", "default"),
    ("https://other.sportsnet.ca/", "default"),
    ("https://sportsnet.ca./", "default"),
    ("https://sportsnet%2eca/", "default"),
    ("https://example.invalid/?next=https://watch.sportsnet.ca", "default"),
    ("https://www.youtube.com/watch?v=abcdefghijk", "default"),
])
def test_profile_selection_uses_validated_exact_hosts(url, expected):
    assert browser_profile(url) == expected


@pytest.mark.parametrize("url", ["file:///data/profile", "javascript:alert(1)",
                               "https://user:password@sportsnet.ca/", "https://sportsnet.ca:0/"])
def test_profile_selection_does_not_bypass_url_validation(url):
    with pytest.raises(UserError):
        browser_profile(url)


def test_requested_browser_source_keys_include_profile(profile_control):
    store, control = profile_control
    sportsnet, capture = control.requested_source(browser_request())
    ordinary, ordinary_capture = control.requested_source(browser_request("https://example.invalid/"))
    assert (sportsnet["profile"], sportsnet["key"]) == ("sportsnet", "browser:sportsnet")
    assert (ordinary["profile"], ordinary["key"]) == ("default", "browser:default")
    assert capture == ordinary_capture == {"kind": "browser", "pulse": "airplayvideo.monitor"}
    page = store.save_page({"name": "Sportsnet", "url": SPORTSNET})
    saved, _ = control.requested_source({"browser_source": "page", "page": page["id"]})
    assert saved["profile"] == "sportsnet"


@pytest.mark.asyncio
async def test_matching_url_in_wrong_profile_cannot_reuse_browser(profile_control):
    _, control = profile_control
    control.browser.url = SPORTSNET
    await control.play(browser_request())
    control.browser.navigate.assert_awaited_once()
    control.browser.start.assert_not_awaited()
    assert control.browser.profile == control.source["profile"] == "sportsnet"
    await control.stop()


@pytest.mark.asyncio
async def test_matching_profile_and_url_reuse_prepared_browser(profile_control):
    store, control = profile_control
    control.browser.profile, control.browser.url = "sportsnet", SPORTSNET
    await control.play(browser_request())
    control.browser.navigate.assert_not_awaited()
    control.browser.start.assert_awaited_once_with(store.data["setup"], profile="sportsnet")
    await control.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["preview", "play", "add"])
@pytest.mark.parametrize("busy", ["stream", "pending", "targets", "pending_targets", "tearing_down",
                                  "recording-tap", "recording-owned", "diagnostic"])
async def test_cross_profile_busy_rejection_preserves_all_state(profile_control, operation, busy):
    store, control = profile_control
    active = SimpleNamespace(close=AsyncMock(), command=AsyncMock())
    release, teardown = asyncio.Event(), None
    if busy in {"stream", "pending"}:
        setattr(control, busy, active)
    elif busy in {"targets", "pending_targets"}:
        setattr(control, busy, {B})
    elif busy == "tearing_down":
        teardown = asyncio.create_task(release.wait())
        control.retiring[object()] = teardown
    elif busy.startswith("recording"):
        control.recordings = SimpleNamespace(current={"id": "c" * 32}, owned=busy == "recording-owned")
    else:
        control.diagnostics = SimpleNamespace(active=True)
    control.source = {"kind": "browser", "key": "browser:default", "profile": "default", "url": "https://keep.invalid/"}
    control.phase, control.error, control.generation = "error", "Preserve this status", 17
    control.receivers = {B: {"state": "streaming"}}
    control.metrics, control.native_report = {"preserve": 1}, {"phase": "failed"}
    before = copy.deepcopy(control.status())
    saved = copy.deepcopy(store.data)
    pending, pending_targets = control.pending, control.pending_targets.copy()
    stream = control.stream
    try:
        with pytest.raises(UserError):
            if operation == "preview":
                await control.open_browser(browser_request())
            else:
                await control.play(browser_request(), add=operation == "add")
        assert control.status() == before and store.data == saved
        assert control.generation == 17 and control.stream is stream and control.pending is pending
        assert control.pending_targets == pending_targets
        control.browser.navigate.assert_not_awaited()
        control.browser.start.assert_not_awaited()
        control.browser.close.assert_not_awaited()
        active.command.assert_not_awaited()
        active.close.assert_not_awaited()
        control.changed.assert_not_called()
    finally:
        release.set()
        if teardown:
            await teardown


@pytest.mark.asyncio
async def test_native_youtube_source_does_not_select_or_switch_browser_profile(profile_control):
    _, control = profile_control
    control.browser.profile = "sportsnet"
    control.play_native = AsyncMock()
    value = {"mode": "browser", "browser_source": "youtube", "url": "https://youtu.be/abcdefghijk",
             "delivery": "native", "native_resolution": "1080p", "receivers": [A]}
    source, capture = control.requested_source(value)
    assert source == {"kind": "youtube", "key": "https://www.youtube.com/watch?v=abcdefghijk:1080p",
                      "label": "YouTube video", "url": "https://www.youtube.com/watch?v=abcdefghijk",
                      "browser_source": "youtube", "native_resolution": "1080p"}
    assert capture == {}
    await control.play(value)
    control.play_native.assert_awaited_once()
    assert control.browser.profile == "sportsnet"
    control.browser.navigate.assert_not_awaited()
    control.browser.start.assert_not_awaited()
    control.browser.close.assert_not_awaited()
