"""Bounded trigger-driven launches never recast repeatedly or touch another TV."""
import asyncio
import copy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service import controller as module
from service.browser_vpn import BrowserVPN
from service.capitals import CapitalsLauncher, WINDOW_SECONDS
from service.controller import Controller
from service.main import Application
from service.model import Store, UserError, default_setup
from service.mqtt import HomeAssistant, discovery_documents
from service.sportsnet import SportsnetCatalog, SportsnetEvent
from test_lifecycle import Channels, Stream


A, B, C, D = [letter * 32 for letter in "abcd"]
TARGETS = [A, B, C]
NOW = datetime(2026, 10, 25, 23, 10, tzinfo=timezone.utc)


def live(now=NOW, **changes):
    values = dict(id="123", title="Toronto at Washington", watch_url="https://watch.sportsnet.ca/watch/Toronto_at_Washington_123",
        event_url="https://watch.sportsnet.ca/event/Toronto_at_Washington_123", start=now - timedelta(minutes=10),
        end=now + timedelta(hours=2), home="Washington", away="Toronto", status="Live", broadcast="", feed="home")
    return SportsnetEvent(**(values | changes))


@pytest.fixture
def context(tmp_path, monkeypatch):
    def make(store=None):
        if store is None:
            store = Store(tmp_path)
            settings = default_setup()
            settings["browser"].update(http_proxy="http://fixture-gateway:8888", http_proxy_enabled=False)
            store.setup(settings)
            store.data["receivers"] = [dict(id=key, name="Fixture " + key[0], slot=slot)
                                       for slot, key in enumerate([A, B, C, D])]
            store.save()
        browser = SimpleNamespace(running=True, profile="default", url="", environment={"DISPLAY": ":42.0"})
        async def close():
            browser.running = False
        async def navigate(url, *args):
            browser.running, browser.profile, browser.url = True, "sportsnet", url
        browser.close, browser.navigate = AsyncMock(side_effect=close), AsyncMock(side_effect=navigate)
        browser.start = AsyncMock()
        control = Controller(store, None, browser, Channels())
        control.recordings = SimpleNamespace(current=None, owned=False)
        config_lock, clock = asyncio.Lock(), SimpleNamespace(value=NOW)
        vpn = BrowserVPN(store, browser, control, config_lock)
        client = SimpleNamespace(fetch=AsyncMock(return_value=SportsnetCatalog("live", NOW, (live(),))))
        launcher = CapitalsLauncher(store, control, vpn, config_lock, Mock(), client=client, now=lambda: clock.value)
        return SimpleNamespace(store=store, browser=browser, control=control, vpn=vpn, client=client,
                               launcher=launcher, clock=clock, make=make)
    monkeypatch.setattr(module, "Stream", Stream)
    Stream.created, Stream.fail, Stream.gate = [], False, None
    return make()


async def arm(context):
    await context.launcher.kick({"receivers": TARGETS})
    # Deterministic tests drive one check without a real 30-second timer.
    await context.launcher.stop_worker()
    return context.launcher.generation


@pytest.mark.asyncio
async def test_no_implicit_schedule_and_window_expires_without_launch(context):
    x = context
    x.launcher.start()
    assert x.launcher.task is None
    x.client.fetch.assert_not_awaited()
    generation = await arm(x)
    x.client.fetch.return_value = SportsnetCatalog("none", NOW)
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "checking" and not Stream.created
    x.clock.value += timedelta(seconds=WINDOW_SECONDS)
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "expired"
    assert x.client.fetch.await_count == 1 and not Stream.created
    assert x.store.data["setup"]["browser"]["http_proxy_enabled"] is False


@pytest.mark.asyncio
async def test_duplicate_kick_keeps_first_deadline_and_worker(context):
    x = context
    await arm(x)
    first_deadline = x.launcher.state()["deadline"]
    x.clock.value += timedelta(minutes=10)
    await x.launcher.kick({"receivers": list(reversed(TARGETS))})
    task = x.launcher.task
    await x.launcher.kick({"receivers": TARGETS})
    assert x.launcher.task is task
    assert x.launcher.state()["deadline"] == first_deadline
    await x.launcher.stop_worker()


@pytest.mark.asyncio
async def test_live_game_replaces_only_selected_tvs_and_enables_saved_vpn(context):
    x = context
    await x.control.play({"mode": "generated", "receivers": [A, C]})
    previous = x.control.stream
    generation = await arm(x)
    await x.launcher.check_once(generation)
    assert previous.closed and x.control.targets == set(TARGETS)
    assert x.control.source["url"] == live().watch_url and x.control.source["profile"] == "sportsnet"
    assert sorted(x.control.stream.commands, key=lambda command: command["id"]) == [
        {"action": "add", "id": key, "slot": "abcd".index(key[0])} for key in sorted(x.control.targets)]
    assert all(command["id"] != D for stream in Stream.created for command in stream.commands)
    assert x.store.data["setup"]["browser"]["http_proxy_enabled"] is True
    assert x.store.data["setup"]["browser"]["http_proxy"] == "http://fixture-gateway:8888"
    assert x.launcher.state()["status"] == "started"
    assert x.store.data["capitals"]["last_attempted_game"] == live().game_key
    assert x.launcher.state()["last_started_game"] == live().game_key
    assert x.launcher.state()["deadline"] is None
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", ["targets", "pending_targets"])
async def test_fourth_tv_blocks_every_side_effect_before_attempt_claim(context, outside):
    x = context
    await x.control.play({"mode": "generated", "receivers": [D]})
    old = x.control.stream
    if outside == "pending_targets":
        x.control.targets.clear()
        x.control.pending_targets = {D}
    generation = await arm(x)
    before = x.control.generation
    await x.launcher.run(generation)
    assert x.launcher.state()["status"] == "error" and "Another TV" in x.launcher.state()["error"]
    assert not old.closed and x.control.stream is old and x.control.generation == before
    assert not x.store.data["capitals"]["last_attempted_game"]
    x.browser.close.assert_not_awaited()
    x.browser.navigate.assert_not_awaited()
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["recording", "diagnostic", "picture_analysis", "teardown"])
async def test_capture_or_measurement_defers_without_claim_or_interruption(context, busy):
    x = context
    generation = await arm(x)
    if busy == "recording":
        x.control.recordings.current = SimpleNamespace(owned=False)
    elif busy == "diagnostic":
        x.control.diagnostics = SimpleNamespace(active=True)
    elif busy == "picture_analysis":
        x.control.recordings.analysis_active = True
    else:
        waiting = asyncio.create_task(asyncio.Event().wait())
        x.control.retiring[object()] = waiting
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "checking" and not Stream.created
    assert not x.store.data["capitals"]["last_attempted_game"]
    x.browser.close.assert_not_awaited()
    x.control.recordings.current, x.control.diagnostics = None, None
    x.control.recordings.analysis_active = False
    if busy == "teardown":
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)
        x.control.retiring.clear()
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "started"
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("catalog", [
    SportsnetCatalog("live", NOW, (live(), live(id="456", away="Boston"))),
    SportsnetCatalog("live", NOW - timedelta(seconds=121), (live(),)),
    SportsnetCatalog("live", NOW, (live(status="Scheduled"),)),
    SportsnetCatalog("live", NOW, (live(end=NOW),)),
])
async def test_ambiguous_stale_or_nonlive_catalog_never_changes_tvs(context, catalog):
    x = context
    generation = await arm(x)
    x.client.fetch.return_value = catalog
    await x.launcher.run(generation)
    assert x.launcher.state()["status"] == "error" and not Stream.created
    assert not x.store.data["capitals"]["last_attempted_game"]
    x.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_transient_public_catalog_error_remains_bounded_and_retries(context):
    x = context
    generation = await arm(x)
    x.client.fetch.return_value = SportsnetCatalog("error", NOW, error="Public catalog unavailable")
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "checking" and not Stream.created
    assert x.launcher.state()["error"] == "Public catalog unavailable"
    x.client.fetch.return_value = SportsnetCatalog("live", NOW, (live(),))
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "started"
    await x.control.close()


@pytest.mark.asyncio
async def test_cancelled_fetch_result_cannot_launch_and_cancel_never_stops_playback(context):
    x = context
    await x.control.play({"mode": "generated", "receivers": [A]})
    old = x.control.stream
    entered = asyncio.Event()
    async def fetch():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return SportsnetCatalog("live", NOW, (live(),))
    x.client.fetch.side_effect = fetch
    await x.launcher.kick({"receivers": TARGETS})
    await entered.wait()
    await x.launcher.cancel()
    assert x.launcher.state()["status"] == "idle" and x.launcher.state()["deadline"] is None
    assert x.control.stream is old and not old.closed and x.control.targets == {A}
    assert not x.store.data["capitals"]["last_attempted_game"]
    await x.control.close()


@pytest.mark.asyncio
async def test_restart_resumes_only_unexpired_explicit_intent_and_deduplicates(context):
    x = context
    await arm(x)
    persisted = copy.deepcopy(x.store.data["capitals"])
    await x.launcher.close()
    assert x.store.data["capitals"] == persisted
    resumed = x.make(Store(x.store.root))
    assert resumed.launcher.task is None  # Construction never navigates.
    resumed.launcher.start()
    await resumed.launcher.task
    assert resumed.launcher.state()["status"] == "started"
    count = len(Stream.created)
    generation = await arm(resumed)
    await resumed.launcher.check_once(generation)
    assert resumed.launcher.state()["status"] == "started" and len(Stream.created) == count
    old = resumed.control.stream
    await resumed.launcher.cancel()
    assert resumed.control.stream is old and not old.closed
    await resumed.control.close()


@pytest.mark.asyncio
async def test_expired_persisted_window_does_not_fetch_on_restart(context):
    x = context
    await arm(x)
    x.store.data["capitals"]["deadline"] = (NOW - timedelta(seconds=1)).isoformat()
    x.store.save()
    resumed = x.make(Store(x.store.root))
    resumed.launcher.start()
    assert resumed.launcher.state()["status"] == "expired" and resumed.launcher.task is None
    resumed.client.fetch.assert_not_awaited()
    assert not Stream.created


@pytest.mark.asyncio
async def test_already_enabled_route_preserves_prepared_sportsnet_browser(context):
    x = context
    settings = copy.deepcopy(x.store.data["setup"])
    settings["browser"]["http_proxy_enabled"] = True
    x.store.setup(settings)
    x.browser.profile = "sportsnet"
    generation = await arm(x)
    await x.launcher.check_once(generation)
    assert x.launcher.state()["status"] == "started"
    x.browser.close.assert_not_awaited()
    await x.control.close()


@pytest.mark.asyncio
async def test_final_preflight_checks_fourth_tv_started_while_catalog_was_loading(context):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog("live", NOW, (live(),))
    x.client.fetch.side_effect = fetch
    await x.launcher.kick({"receivers": TARGETS})
    await entered.wait()
    await x.control.play({"mode": "generated", "receivers": [D]})
    old = x.control.stream
    release.set()
    await x.launcher.task
    assert x.launcher.state()["status"] == "error"
    assert x.control.stream is old and not old.closed and x.control.targets == {D}
    assert not x.store.data["capitals"]["last_attempted_game"]
    await x.control.close()


@pytest.mark.asyncio
async def test_failed_start_claim_survives_restart_and_is_not_recast(context):
    x = context
    generation = await arm(x)
    Stream.fail = True
    await x.launcher.run(generation)
    assert x.launcher.state()["status"] == "error"
    assert x.store.data["capitals"]["last_attempted_game"] == live().game_key
    assert not x.launcher.state()["last_started_game"]
    resumed = x.make(Store(x.store.root))
    Stream.fail = False
    count = len(Stream.created)
    generation = await arm(resumed)
    await resumed.launcher.check_once(generation)
    assert resumed.launcher.state()["status"] == "error" and len(Stream.created) == count


@pytest.mark.asyncio
async def test_save_failure_prevents_stop_vpn_or_play(context, monkeypatch):
    x = context
    await x.control.play({"mode": "generated", "receivers": [A]})
    old = x.control.stream
    generation = await arm(x)
    monkeypatch.setattr(x.store, "save", Mock(side_effect=OSError("fixture disk full")))
    with pytest.raises(OSError):
        await x.launcher.check_once(generation)
    assert not old.closed and x.control.stream is old
    assert not x.store.data["capitals"]["last_attempted_game"]
    x.browser.close.assert_not_awaited()
    await x.control.close()


@pytest.mark.asyncio
async def test_manual_stop_during_route_change_cancels_launch(context):
    x = context
    generation = await arm(x)
    x.browser.close.side_effect = x.control.stop
    await x.launcher.run(generation)
    assert x.launcher.state()["status"] == "error" and not Stream.created
    assert x.launcher.state()["error"] == "Playback was cancelled"


@pytest.mark.asyncio
async def test_manual_stop_during_owned_stream_teardown_is_not_absorbed(context):
    x = context
    await x.control.play({"mode": "generated", "receivers": [A]})
    old = x.control.stream
    entered, release = asyncio.Event(), asyncio.Event()
    async def close():
        entered.set()
        await release.wait()
        old.closed = True
    old.close = AsyncMock(side_effect=close)
    await x.launcher.kick({"receivers": TARGETS})
    await entered.wait()
    user_stop = asyncio.create_task(x.control.stop())
    await asyncio.sleep(0)
    release.set()
    await user_stop
    await x.launcher.task
    assert x.launcher.state()["status"] == "error"
    assert x.launcher.state()["error"] == "Playback was cancelled"
    assert x.control.stream is None and not x.control.targets and len(Stream.created) == 1
    x.browser.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_launcher_never_marks_started_when_play_returns_after_stop(context):
    x = context
    generation = await arm(x)
    async def play(request, add=False):
        x.control.generation += 1
        await x.control.stop()
    x.control.play_locked = AsyncMock(side_effect=play)
    await x.launcher.run(generation)
    assert x.launcher.state()["status"] == "error"
    assert x.launcher.state()["error"] == "Playback was cancelled"
    assert not x.launcher.state()["last_started_game"]
    assert x.store.data["capitals"]["last_attempted_game"] == live().game_key


@pytest.mark.asyncio
async def test_kick_queued_during_close_and_after_close_cannot_spawn_or_persist(context):
    x = context
    await arm(x)
    before = x.store.path.read_bytes()
    entered, release = asyncio.Event(), asyncio.Event()
    stop = x.launcher.stop_worker
    async def stopping():
        entered.set()
        await release.wait()
        await stop()
    x.launcher.stop_worker = AsyncMock(side_effect=stopping)
    closing = asyncio.create_task(x.launcher.close())
    await entered.wait()
    kicked = asyncio.create_task(x.launcher.kick({"receivers": TARGETS}))
    await asyncio.sleep(0)
    assert not kicked.done()
    release.set()
    await closing
    with pytest.raises(UserError, match="shutting down"):
        await kicked
    with pytest.raises(UserError, match="shutting down"):
        await x.launcher.kick({"receivers": [A]})
    assert x.launcher.task is None and x.store.path.read_bytes() == before
    x.client.fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_while_new_kick_is_cancelling_previous_worker_blocks_rearm(context):
    x = context
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            return SportsnetCatalog("live", NOW, (live(),))
    x.client.fetch.side_effect = fetch
    await x.launcher.kick({"receivers": TARGETS})
    await entered.wait()
    before = x.store.path.read_bytes()
    replacement = asyncio.create_task(x.launcher.kick({"receivers": [A]}))
    await cancelled.wait()
    closing = asyncio.create_task(x.launcher.close())
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(UserError, match="shutting down"):
        await replacement
    await closing
    assert x.launcher.task is None and x.store.path.read_bytes() == before and not Stream.created


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["capitals_live", "capitals_cancel", "capitals_refresh"])
async def test_application_actions_and_global_mqtt_need_no_receiver_field(context, action):
    x = context
    x.launcher.kick, x.launcher.cancel, x.launcher.refresh = AsyncMock(), AsyncMock(), AsyncMock()
    app = Application.__new__(Application)
    app.controller, app.capitals = x.control, x.launcher
    app.state = lambda: {"capitals": x.launcher.state()}
    await app.action(SimpleNamespace(match_info={"action": action}, json=AsyncMock(return_value={"receivers": TARGETS})))
    method = {"capitals_live": x.launcher.kick, "capitals_cancel": x.launcher.cancel, "capitals_refresh": x.launcher.refresh}[action]
    method.assert_awaited_once()
    if action != "capitals_refresh":
        ha = HomeAssistant(x.store, x.control, Channels(), None)
        ha.capitals = x.launcher
        await ha.command({"action": action, "receivers": TARGETS})
        assert method.await_count == 2
        assert not x.control.error


def test_global_status_and_cancel_discovery_are_stable(context):
    documents = discovery_documents(context.store, Channels())
    entries = {topic: value for topic, value in documents.items() if "_capitals/" in topic}
    assert len(entries) == 2
    cancel = next(value for topic, value in entries.items() if "/cancel/" in topic)
    status = next(value for topic, value in entries.items() if "/status/" in topic)
    assert json.loads(cancel["payload_press"]) == {"action": "capitals_cancel"}
    assert status["state_topic"].endswith("/capitals/state")
    assert status["json_attributes_topic"] == status["state_topic"]
