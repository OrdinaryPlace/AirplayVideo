"""Explicit direct/gateway selection preserves private browser sessions and media."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service.browser_proxy import chrome_proxy_arguments
from service.browser_vpn import BrowserVPN
from service.main import Application
from service.model import Store, UserError, default_setup, effective_browser_proxy
from service.mqtt import HomeAssistant, discovery_documents


PROXY = "http://fixture-gateway:8888"
OTHER = "http://other-gateway:8888"


@pytest.fixture
def vpn(tmp_path):
    store = Store(tmp_path)
    settings = default_setup()
    settings["browser"].update(http_proxy=PROXY, http_proxy_enabled=True, vpn_name="Montréal VPN")
    store.setup(settings)
    browser = SimpleNamespace(close=AsyncMock())
    controller = SimpleNamespace(lock=asyncio.Lock(), stream=None, pending=None,
        targets=set(), pending_targets=set(), tearing_down=False, generation=17,
        error="Keep existing status", recordings=SimpleNamespace(current=None),
        require_no_diagnostic=Mock())
    return BrowserVPN(store, browser, controller, asyncio.Lock(), Mock())


@pytest.mark.parametrize("proxy", ["", PROXY])
def test_legacy_upgrade_retains_route_and_unrelated_state(tmp_path, proxy):
    store = Store(tmp_path)
    store.setup(default_setup())
    store.save_page({"name": "Keep", "url": "https://watch.sportsnet.ca/"})
    store.data["setup"]["browser"]["http_proxy"] = proxy
    del store.data["setup"]["browser"]["http_proxy_enabled"]
    del store.data["setup"]["browser"]["vpn_name"]
    store.save()
    before = copy.deepcopy(store.data)
    reopened = Store(tmp_path)
    assert reopened.data["setup"]["browser"]["http_proxy_enabled"] is bool(proxy)
    assert effective_browser_proxy(reopened.data["setup"]) == proxy
    assert reopened.data["setup"]["browser"]["vpn_name"] == "Browser VPN"
    for key in ("http_proxy_enabled", "vpn_name"):
        del reopened.data["setup"]["browser"][key]
    assert reopened.data == before


@pytest.mark.parametrize("changes", [
    {"http_proxy_enabled": "false"}, {"http_proxy_enabled": 1},
    {"http_proxy": "", "http_proxy_enabled": True}, {"vpn_name": ""},
    {"vpn_name": "x" * 65}, {"vpn_name": "Bad\nname"}, {"vpn_name": "Bad\x7fname"},
])
def test_invalid_vpn_settings_are_atomic(vpn, changes):
    before = vpn.store.path.read_bytes()
    settings = copy.deepcopy(vpn.store.data["setup"])
    settings["browser"].update(changes)
    with pytest.raises(UserError):
        vpn.store.setup(settings)
    assert vpn.store.path.read_bytes() == before


@pytest.mark.asyncio
async def test_route_toggle_closes_chrome_and_keeps_both_profiles_and_gateway(vpn):
    roots = [vpn.store.root / name for name in ("browser", "browser-sportsnet")]
    for root in roots:
        (root / "profile").mkdir(parents=True)
        (root / "profile" / "fixture-session").write_bytes(b"fixture-only")
    key = vpn.store.root / "browser-controls.pem"
    key.write_bytes(b"fixture-key-not-real")
    before = copy.deepcopy(vpn.store.data)
    await vpn.set_enabled({"enabled": False, "expected_http_proxy": PROXY,
                           "expected_http_proxy_enabled": True})
    vpn.browser.close.assert_awaited_once()
    assert vpn.state() == {"name": "Montréal VPN", "configured": True,
        "enabled": False, "route": "direct", "gateway_reachable": None}
    assert vpn.store.data["setup"]["browser"]["http_proxy"] == PROXY
    assert chrome_proxy_arguments(effective_browser_proxy(vpn.store.data["setup"])) == []
    assert Store(vpn.store.root).data == vpn.store.data
    await vpn.set_enabled({"enabled": True})
    assert vpn.browser.close.await_count == 2
    arguments = chrome_proxy_arguments(effective_browser_proxy(vpn.store.data["setup"]))
    assert "--proxy-server=" + PROXY in arguments
    assert not any("DIRECT" in value for value in arguments)
    assert vpn.store.data == before
    assert vpn.controller.generation == 17 and vpn.controller.error == "Keep existing status"
    assert key.read_bytes() == b"fixture-key-not-real"
    assert all((root / "profile" / "fixture-session").read_bytes() == b"fixture-only" for root in roots)


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["stream", "pending", "targets", "pending_targets", "tearing_down", "recording", "diagnostic"])
async def test_busy_rejection_precedes_close_save_or_media_mutation(vpn, busy):
    if busy == "diagnostic":
        vpn.controller.require_no_diagnostic.side_effect = UserError("Wait for the diagnostic")
    elif busy == "recording":
        # Tapped recordings with owned=False also protect the current session.
        vpn.controller.recordings.current = SimpleNamespace(owned=False)
    else:
        setattr(vpn.controller, busy, {"fixture-tv"} if "targets" in busy else object())
    before = vpn.store.path.read_bytes()
    with pytest.raises(UserError):
        await vpn.set_enabled({"enabled": False})
    assert vpn.store.path.read_bytes() == before
    vpn.browser.close.assert_not_awaited()
    vpn.changed.assert_not_called()
    assert vpn.controller.generation == 17 and vpn.controller.error == "Keep existing status"


@pytest.mark.asyncio
async def test_idempotent_mqtt_retry_does_not_interrupt_active_media(vpn):
    vpn.controller.stream = object()
    await vpn.set_enabled({"enabled": True})
    vpn.browser.close.assert_not_awaited()
    vpn.changed.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("expected", [
    {"expected_http_proxy": OTHER}, {"expected_http_proxy_enabled": False},
    {"expected_http_proxy_enabled": "true"},
])
async def test_stale_or_invalid_snapshot_cannot_change_the_route(vpn, expected):
    before = vpn.store.path.read_bytes()
    with pytest.raises(UserError):
        await vpn.set_enabled({"enabled": False, **expected})
    vpn.browser.close.assert_not_awaited()
    assert vpn.store.path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [{}, {"enabled": 1}, {"enabled": "false"}, None])
async def test_invalid_command_is_rejected_without_mutation(vpn, value):
    before = vpn.store.path.read_bytes()
    with pytest.raises(UserError):
        await vpn.set_enabled(value)
    assert vpn.store.path.read_bytes() == before
    vpn.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_gateway_cannot_be_enabled(vpn):
    settings = copy.deepcopy(vpn.store.data["setup"])
    settings["browser"].update(http_proxy="", http_proxy_enabled=False)
    vpn.store.setup(settings)
    before = vpn.store.path.read_bytes()
    with pytest.raises(UserError, match="Set a VPN gateway"):
        await vpn.set_enabled({"enabled": True})
    assert vpn.store.path.read_bytes() == before
    vpn.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_toggle_rechecks_snapshot_under_both_locks(vpn):
    async with vpn.configuration_lock:
        task = asyncio.create_task(vpn.set_enabled({"enabled": False, "expected_http_proxy_enabled": True}))
        await asyncio.sleep(0)
        assert not task.done()
        settings = copy.deepcopy(vpn.store.data["setup"])
        settings["browser"]["http_proxy_enabled"] = False
        vpn.store.setup(settings)
    with pytest.raises(UserError, match="another window"):
        await task
    vpn.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_action_uses_shared_setter_and_returns_current_state(vpn):
    app = Application.__new__(Application)
    app.controller = vpn.controller
    app.browser_vpn = vpn
    app.state = lambda: {"browser_vpn": vpn.state()}
    response = await app.action(SimpleNamespace(match_info={"action": "browser_vpn"},
        json=AsyncMock(return_value={"enabled": False, "expected_http_proxy_enabled": True})))
    assert json.loads(response.text)["browser_vpn"]["route"] == "direct"
    vpn.browser.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_reachability_is_not_route_or_vpn_proof(vpn, monkeypatch):
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    connection = AsyncMock(return_value=(object(), writer))
    monkeypatch.setattr("service.browser_vpn.asyncio.open_connection", connection)
    await vpn.probe()
    connection.assert_awaited_once_with("fixture-gateway", 8888)
    assert vpn.state()["gateway_reachable"] is True
    connection.side_effect = OSError("fixture down")
    await vpn.probe()
    assert vpn.state()["gateway_reachable"] is False
    assert vpn.state()["route"] == "gateway"  # A failed gateway never enables direct browsing.
    vpn.browser.close.assert_not_awaited()
    assert effective_browser_proxy(vpn.store.data["setup"]) == PROXY


@pytest.mark.asyncio
async def test_probe_discards_old_gateway_and_closes_socket_on_cancel(vpn, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    writer = SimpleNamespace(close=Mock(), wait_closed=AsyncMock())
    async def connection(*args):
        entered.set()
        await release.wait()
        return object(), writer
    monkeypatch.setattr("service.browser_vpn.asyncio.open_connection", connection)
    task = asyncio.create_task(vpn.probe())
    await entered.wait()
    settings = copy.deepcopy(vpn.store.data["setup"])
    settings["browser"]["http_proxy"] = OTHER
    vpn.store.setup(settings)
    release.set()
    await task
    assert vpn.state()["gateway_reachable"] is None
    vpn.changed.assert_not_called()
    writer.wait_closed = AsyncMock(side_effect=asyncio.CancelledError)
    task = asyncio.create_task(vpn.probe())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert writer.close.call_count >= 2


def test_global_switch_has_stable_identity_and_no_receiver_requirement(vpn):
    channels = SimpleNamespace(public=lambda: [])
    first = {topic: item for topic, item in discovery_documents(vpn.store, channels).items()
             if topic.endswith("/routing/config")}
    assert len(first) == 1
    topic, document = next(iter(first.items()))
    assert document["name"] == "Montréal VPN"
    assert json.loads(document["payload_off"]) == {"action": "browser_vpn", "enabled": False}
    assert document["json_attributes_topic"].endswith("/browser_vpn/state")
    vpn.store.data["setup"]["browser"]["vpn_name"] = "Another gateway"
    renamed = {topic: item for topic, item in discovery_documents(vpn.store, channels).items()
               if topic.endswith("/routing/config")}
    assert set(renamed) == {topic}
    assert renamed[topic]["unique_id"] == document["unique_id"]


@pytest.mark.asyncio
async def test_mqtt_global_toggle_uses_setter_and_publishes_actual_state(vpn):
    vpn.controller.status = lambda: {"receivers": {}, "targets": [], "source": None, "error": ""}
    ha = HomeAssistant(vpn.store, vpn.controller, SimpleNamespace(rows=[]), None)
    ha.browser_vpn = vpn
    ha.connected, ha.client = True, SimpleNamespace(publish=Mock())
    await ha.command({"action": "browser_vpn", "enabled": False})
    assert vpn.state()["route"] == "direct"
    publication = next(call for call in ha.client.publish.call_args_list
                       if call.args[0].endswith("/browser_vpn/state"))
    assert json.loads(publication.args[1]) == vpn.state()
    assert publication.kwargs == {"qos": 1, "retain": True}
    assert vpn.controller.error == "Keep existing status"
