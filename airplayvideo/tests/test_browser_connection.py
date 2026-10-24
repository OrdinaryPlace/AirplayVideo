"""A stale browser window must not silently follow another window's route edit."""
import asyncio
import copy
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service.controller import Controller
from service.main import Application
from service.model import Store, UserError, default_setup


PROXY = "http://fixture-surfshark-gateway:8888"
OTHER = "http://other-surfshark-gateway:8888"
RECEIVER = "a" * 32


@pytest.fixture
def application(tmp_path):
    store = Store(tmp_path)
    settings = default_setup()
    settings["browser"]["http_proxy"] = PROXY
    store.setup(settings)
    store.save_page({"name": "Keep", "url": "https://example.invalid/"})
    store.data["receivers"] = [{"id": RECEIVER, "name": "Fixture TV", "slot": 0}]
    browser = SimpleNamespace(running=False, navigate=AsyncMock(), start=AsyncMock(),
                              close=AsyncMock(), environment={"DISPLAY": ":42.0"})
    channels = SimpleNamespace(rows=[], refresh=AsyncMock())
    app = Application.__new__(Application)
    app.store, app.browser, app.channels = store, browser, channels
    app.controller = Controller(store, None, browser, channels)
    app.configuration_lock = asyncio.Lock()
    app.recordings = SimpleNamespace(current=None)
    app.ha = SimpleNamespace(close=AsyncMock(), start=AsyncMock(), refresh=Mock())
    app.state = store.public
    return app


def request(action, fields):
    return SimpleNamespace(match_info={"action": action}, json=AsyncMock(return_value=fields))


async def change_proxy(app, value):
    prior = app.store.data["setup"]["browser"]["http_proxy"]
    await app.setup(request("update", {
        "changes": {"browser": {"http_proxy": value}},
        "expected": {"browser": {"http_proxy": prior}},
    }))


def browser_request(expected=PROXY):
    return {"mode": "browser", "browser_source": "url", "url": "https://example.invalid/",
            "receivers": [RECEIVER], "expected_http_proxy": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", ["", OTHER])
@pytest.mark.parametrize("operation", ["setup", "preview", "play"])
async def test_stale_window_is_rejected_after_another_window_changes_route(application, replacement, operation):
    app = application
    await change_proxy(app, replacement)
    value = browser_request()
    before = copy.deepcopy(app.store.data)
    if operation == "setup":
        start = app.setup(request("open_browser", value))
    elif operation == "preview":
        start = app.controller.open_browser(value)
    else:
        start = app.controller.play(value)
    with pytest.raises(UserError, match="connection changed in another window"):
        await start
    app.browser.navigate.assert_not_awaited()
    app.browser.start.assert_not_awaited()
    assert app.store.data == before
    assert app.controller.generation == 0
    assert app.controller.stream is None
    assert not app.controller.targets


@pytest.mark.asyncio
async def test_rejected_play_preserves_an_existing_receiver_session(application):
    app = application
    await change_proxy(app, "")
    active = SimpleNamespace(close=AsyncMock(), command=AsyncMock())
    control = app.controller
    control.stream = active
    control.source = {"kind": "browser", "key": "browser", "url": "https://keep.invalid/"}
    control.targets = {RECEIVER}
    control.receivers = {RECEIVER: {"state": "streaming"}}
    control.phase, control.generation, control.error = "playing", 7, "Keep this status"
    before = copy.deepcopy(control.status())
    with pytest.raises(UserError, match="connection changed in another window"):
        await control.play(browser_request())
    assert control.status() == before
    assert control.generation == 7 and control.stream is active
    active.close.assert_not_awaited()
    active.command.assert_not_awaited()
    app.browser.navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_preview_compares_after_waiting_for_settings_lock(application):
    app = application
    async with app.controller.lock:
        opening = asyncio.create_task(app.controller.open_browser(browser_request()))
        await asyncio.sleep(0)
        assert not opening.done()
        # An update already owning this shared lock changes the saved route.
        settings = copy.deepcopy(app.store.data["setup"])
        settings["browser"]["http_proxy"] = ""
        app.store.setup(settings)
    with pytest.raises(UserError, match="connection changed in another window"):
        await opening
    app.browser.navigate.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["setup", "preview"])
@pytest.mark.parametrize("expected", [PROXY, "HTTP://FIXTURE-SURFSHARK-GATEWAY:8888/", None])
async def test_matching_and_legacy_requests_use_the_saved_browser_route(application, operation, expected):
    app = application
    value = browser_request(expected)
    if expected is None:
        del value["expected_http_proxy"]  # Existing clients and MQTT omit the guard.
    if operation == "setup":
        await app.setup(request("open_browser", value))
    else:
        await app.controller.open_browser(value)
    app.browser.navigate.assert_awaited_once()
    assert app.browser.navigate.await_args.args[1]["browser"]["http_proxy"] == PROXY


@pytest.mark.asyncio
async def test_native_youtube_ignores_browser_connection_expectation(application):
    app = application
    app.controller.play_native = AsyncMock()
    value = browser_request("http://private-user:private-password@invalid:8888") | {
        "browser_source": "youtube", "url": "https://www.youtube.com/watch?v=abcdefghijk",
        "delivery": "native",
    }
    await app.controller.play(value)
    app.controller.play_native.assert_awaited_once()
    app.browser.navigate.assert_not_awaited()
    app.browser.start.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_expectation_is_safe_and_does_not_navigate(application):
    app = application
    value = browser_request("http://private-user:private-password@invalid:8888")
    with pytest.raises(UserError) as error:
        await app.controller.open_browser(value)
    assert "private-user" not in str(error.value)
    assert "private-password" not in str(error.value)
    app.browser.navigate.assert_not_awaited()


@pytest.mark.asyncio
async def test_ui_request_retains_its_snapshot_for_server_stale_window_check(application):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for the browser request check")
    source = (Path(__file__).parents[1] / "web" / "app.js").read_text()
    function = re.search(r"^function requestSource\(\).*?^\}", source, re.M | re.S).group()
    harness = """
const state={setup:{browser:{http_proxy:'http://fixture-surfshark-gateway:8888'}}};
const mode='browser';
const fields={browserKind:'url',pageSelect:'',sourceUrl:'https://example.invalid/',youtubeDelivery:'browser',nativeQuality:'best'};
const $=id=>({value:fields[id]});
"""
    result = subprocess.run([node], input=harness + function + "\nprocess.stdout.write(JSON.stringify(requestSource()));",
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    import json
    value = json.loads(result.stdout)
    assert value["expected_http_proxy"] == PROXY
    await change_proxy(application, "")
    with pytest.raises(UserError, match="connection changed in another window"):
        await application.controller.open_browser(value)
    application.browser.navigate.assert_not_awaited()
