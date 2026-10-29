"""Manual Capitals controls retain explicit targets and one-shot user intent."""
import asyncio
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from service.main import Application, boundary
from service.model import Store, UserError, default_setup
from service.mqtt import HomeAssistant, discovery_documents
from test_capitals import context as game_context, Stream


A, B, C, D = (letter * 32 for letter in "abcd")
WEB = Path(__file__).resolve().parents[1] / "web"
SOURCE = (WEB / "app.js").read_text()


@pytest.fixture
def controls(tmp_path):
    store = Store(tmp_path)
    settings = default_setup()
    settings["browser"].update(http_proxy="http://fixture-gateway:8888", http_proxy_enabled=False)
    store.setup(settings)
    store.data["receivers"] = [dict(id=key, name="TV " + key[0], slot=index)
                               for index, key in enumerate((A, B, C, D))]
    store.save()
    controller = SimpleNamespace(play=AsyncMock(), stop=AsyncMock(), error="")
    launcher = SimpleNamespace(play_current=AsyncMock(), kick=AsyncMock(), cancel=AsyncMock(),
                               refresh=AsyncMock(), state=lambda: {"status": "idle"})
    channels = SimpleNamespace(rows=[], public=lambda: [])
    return SimpleNamespace(store=store, controller=controller, launcher=launcher, channels=channels)


def test_each_ha_button_targets_only_its_tv_and_never_retains_a_manual_press(controls):
    entries = discovery_documents(controls.store, controls.channels)
    buttons = {topic: value for topic, value in entries.items() if topic.endswith("/capitals_play/config")}
    assert len(buttons) == 4
    for receiver in controls.store.data["receivers"]:
        topic = next(topic for topic in buttons if receiver["id"] in topic)
        button = buttons[topic]
        assert button["name"] == "Play current Capitals game"
        assert button["qos"] == 0 and button["retain"] is False
        assert json.loads(button["payload_press"]) == {
            "action": "capitals_play", "receivers": [receiver["id"]], "join": True}
        assert button["command_topic"] == "airplayvideo/" + controls.store.data["installation_id"] + "/command"
        assert button["device"]["name"] == receiver["name"] + " AirplayVideo"
    # Existing global check/cancel state remains separate from per-TV intent.
    global_entries = [value for topic, value in entries.items() if "_capitals/" in topic]
    assert len(global_entries) == 2
    assert any(json.loads(value.get("payload_press", "{}")) == {"action": "capitals_cancel"}
               for value in global_entries)


@pytest.mark.parametrize("unavailable", ["incomplete", "browser_disabled", "no_gateway"])
def test_manual_ha_buttons_require_completed_browser_and_configured_gateway(controls, unavailable):
    setup = controls.store.data["setup"]
    if unavailable == "incomplete":
        setup["complete"] = False
    elif unavailable == "browser_disabled":
        setup["modes"]["browser"] = False
    else:
        setup["browser"]["http_proxy"] = ""
    assert not any(topic.endswith("/capitals_play/config")
                   for topic in discovery_documents(controls.store, controls.channels))


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"receivers": [A], "join": True}, {"receivers": [A, B, C]},
    {"receivers": [C, A], "join": False},
])
async def test_mqtt_dispatches_manual_intent_once_without_scheduled_check_or_target_expansion(controls, body):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.capitals = controls.launcher
    payload = {"action": "capitals_play", **body}
    await ha.command(payload)
    controls.launcher.play_current.assert_awaited_once_with(payload)
    controls.launcher.kick.assert_not_awaited()
    controls.launcher.cancel.assert_not_awaited()
    controls.controller.play.assert_not_awaited()
    assert D not in controls.launcher.play_current.await_args.args[0]["receivers"]


@pytest.mark.asyncio
async def test_mqtt_manual_failure_reports_error_and_never_falls_back_or_retries(controls):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.capitals = controls.launcher
    controls.launcher.play_current.side_effect = UserError("Choose one or more TVs")
    # A legacy receiver field must not become an implicit target list.
    payload = {"action": "capitals_play", "receiver": A}
    await ha.command(payload)
    controls.launcher.play_current.assert_awaited_once_with(payload)
    assert controls.controller.error == "Choose one or more TVs"
    controls.launcher.kick.assert_not_awaited()
    controls.controller.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_retained_manual_mqtt_payload_is_not_dispatched(controls):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.loop = SimpleNamespace(call_soon_threadsafe=Mock())
    payload = {"action": "capitals_play", "receivers": [A], "join": True}
    message = SimpleNamespace(topic=ha.base + "/command", payload=json.dumps(payload).encode(), retain=True)
    ha.on_message(None, None, message)
    ha.loop.call_soon_threadsafe.assert_not_called()
    message.retain = False
    ha.on_message(None, None, message)
    ha.loop.call_soon_threadsafe.assert_called_once_with(ha.schedule, payload)


@pytest.mark.asyncio
async def test_manual_api_keeps_existing_json_mutation_boundary_and_has_no_implicit_targets(controls):
    application = Application.__new__(Application)
    application.controller, application.capitals = controls.controller, controls.launcher
    application.state = lambda: {"capitals": controls.launcher.state()}
    app = web.Application(middlewares=[boundary])
    app["standalone"] = True
    app.router.add_post("/api/actions/{action}", application.action)
    async with TestClient(TestServer(app)) as client:
        path = "/api/actions/capitals_play"
        response = await client.post(path, json={"receivers": [A, B, C]})
        assert response.status == 403
        controls.launcher.play_current.assert_not_awaited()
        payload = {"receivers": [A, B, C]}
        response = await client.post(path, json=payload, headers={"X-AirplayVideo": "1"})
        assert response.status == 200
        controls.launcher.play_current.assert_awaited_once_with(payload)
        controls.launcher.play_current.reset_mock()
        controls.launcher.play_current.side_effect = UserError("Choose one or more TVs")
        response = await client.post(path, json={}, headers={"X-AirplayVideo": "1"})
        assert response.status == 409 and (await response.json())["error"] == "Choose one or more TVs"
        controls.launcher.play_current.assert_awaited_once_with({})
        controls.launcher.kick.assert_not_awaited()


def run_ui(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser controls checks")
    functions = []
    for name in ("updateButtons", "renderCapitals", "act"):
        match = re.search(rf"^(?:async )?function {name}\([^\n]*\)\s*\{{.*?^\}}", SOURCE, re.M | re.S)
        assert match, name
        functions.append(match.group())
    handlers = "\n".join(re.search(rf"^\$\('{name}'\)\.onclick=[^\n]+", SOURCE, re.M).group()
                         for name in ("capitalsPlay", "capitalsCheck", "capitalsRefresh", "capitalsCancel"))
    harness = r"""
const assert=require('node:assert/strict');
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',hidden:false,disabled:false,dataset:{}});return nodes.get(id);};
const document={querySelectorAll(){return [];}};
let busy=false,busyAction='',draft=null,mode='browser',selectionDirty=true;
let chosenTVs=new Set(['aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','cccccccccccccccccccccccccccccccc']);
let state={setup:{complete:true,modes:{browser:true}},runtime:{phase:'idle',targets:[],source:null,browser_open:true},
 receivers:[],browser_vpn:{configured:true},capitals:{status:'idle',catalog:null},diagnostics:{active:false},
 recordings:{active:null,analysis:{status:'idle'}},frame_probe:{status:'idle'}};
let calls=[],refreshes=0,fail='',messages=[],pendingPlay=null;
function message(value=''){messages.push(value);}
function frameStageBlockReason(){return '';}
function automationExample(){}
function settingsStatus(){}
function renderUsage(){renderCapitals();updateButtons();}
async function refresh(){refreshes++;return state;}
async function api(path,body){calls.push({path,body:structuredClone(body)});if(fail)throw Error(fail);if(pendingPlay&&path.endsWith('/capitals_play'))return pendingPlay;return state;}
"""
    result = subprocess.run([node], input=harness + "\n".join(functions) + handlers +
                            "\n(async()=>{" + script + "})().catch(error=>{console.error(error);process.exitCode=1;});",
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_manual_ui_button_submits_only_selected_tvs_once_and_never_arms_a_check_window():
    run_ui("""
updateButtons();assert.equal($('capitalsPlay').disabled,false);
const selected=[...chosenTVs];
await $('capitalsPlay').onclick();
assert.deepEqual(calls,[{path:'api/actions/capitals_play',body:{receivers:selected}}]);
assert.equal(selectionDirty,false);
assert.equal(calls[0].body.receivers.includes('dddddddddddddddddddddddddddddddd'),false);
""")
    markup = (WEB / "index.html").read_text()
    assert '<button id="capitalsPlay">Play current game</button>' in markup
    assert "checks Sportsnet once" in markup and "If no game is live, playback stays unchanged" in markup
    assert 'id="capitalsCheck"' in markup and "up to 20 minutes" in markup


@pytest.mark.parametrize("condition", [
    "chosenTVs.clear()", "state.setup.complete=false", "state.setup.modes.browser=false",
    "state.browser_vpn.configured=false", "state.diagnostics.active=true",
    "state.recordings.active={id:'sample'}", "state.recordings.analysis.status='running'",
    "state.frame_probe.status='running'", "busy=true",
])
def test_manual_ui_requires_explicit_ready_targets_and_no_exclusive_measurement(condition):
    run_ui(condition + ";updateButtons();assert.equal($('capitalsPlay').disabled,true);assert.deepEqual(calls,[]);")


def test_manual_ui_error_does_not_retry_or_start_the_old_twenty_minute_worker():
    run_ui("""
fail='No live Capitals broadcast was found';
await $('capitalsPlay').onclick();
assert.equal(calls.length,1);assert.equal(calls[0].path,'api/actions/capitals_play');
assert.equal(refreshes,1);assert.equal(busy,false);
assert.equal(messages.at(-1),fail);
""")


@pytest.mark.parametrize("prior_status", ["idle", "started", "error"])
def test_manual_ui_can_cancel_before_first_status_poll_and_keeps_existing_check_controls(prior_status):
    run_ui("state.capitals.status=" + json.dumps(prior_status) + ";" + """
updateButtons();assert.equal($('capitalsCancel').hidden,true);
let finish;pendingPlay=new Promise(resolve=>{finish=resolve;});
const starting=$('capitalsPlay').onclick();
assert.equal($('capitalsCancel').hidden,false);assert.equal($('capitalsCancel').disabled,false);
assert.equal($('capitalsPlay').textContent,'Starting…');
await $('capitalsCancel').onclick();
assert.equal(busy,true);assert.equal(busyAction,'capitals_play');
assert.deepEqual(calls.map(call=>call.path),['api/actions/capitals_play','api/actions/capitals_cancel']);
finish(state);await starting;assert.equal(busy,false);
assert.equal($('capitalsCancel').hidden,true);assert.equal($('capitalsPlay').textContent,'Play current game');
await $('capitalsCheck').onclick();await $('capitalsRefresh').onclick();
assert.deepEqual(calls.slice(-2).map(call=>call.path),['api/actions/capitals_live','api/actions/capitals_refresh']);
""")


@pytest.mark.asyncio
@pytest.mark.parametrize("surface,receiver", [("api", A), ("api", D), ("api", None), ("mqtt", A), ("mqtt", D)])
async def test_external_stop_cancels_only_matching_pending_manual_game_before_targets_exist(game_context, surface, receiver):
    x = game_context
    entered, release = asyncio.Event(), asyncio.Event()
    catalog = x.client.fetch.return_value
    async def held_fetch():
        entered.set()
        await release.wait()
        return catalog
    x.client.fetch.side_effect = held_fetch
    playing = asyncio.create_task(x.launcher.play_current({"receivers": [A]}))
    try:
        await entered.wait()
        assert not x.control.targets and not x.control.pending_targets
        if surface == "api":
            application = Application.__new__(Application)
            application.controller, application.capitals = x.control, x.launcher
            application.state = lambda: {}
            body = {} if receiver is None else {"receiver": receiver}
            await application.action(SimpleNamespace(match_info={"action": "stop"}, json=AsyncMock(return_value=body)))
        else:
            ha = HomeAssistant(x.store, x.control, x.control.channels, None)
            ha.capitals = x.launcher
            await ha.command({"action": "stop", "receiver": receiver})
            assert not x.control.error
        release.set()
        if receiver == D:
            await playing
            assert x.control.targets == {A} and len(Stream.created) == 1
        else:
            with pytest.raises(UserError, match="cancelled"):
                await playing
            assert not Stream.created and not x.control.targets
            x.browser.navigate.assert_not_awaited()
    finally:
        release.set()
        if not playing.done():
            playing.cancel()
        await asyncio.gather(playing, return_exceptions=True)
        await x.launcher.close()
        await x.control.close()


@pytest.mark.asyncio
async def test_http_stop_during_manual_fetch_returns_safe_conflict_not_server_error(game_context):
    x = game_context
    entered, release = asyncio.Event(), asyncio.Event()
    catalog = x.client.fetch.return_value
    async def held_fetch():
        entered.set()
        await release.wait()
        return catalog
    x.client.fetch.side_effect = held_fetch
    application = Application.__new__(Application)
    application.controller, application.capitals = x.control, x.launcher
    application.state = lambda: {}
    app = web.Application(middlewares=[boundary])
    app["standalone"] = True
    app.router.add_post("/api/actions/{action}", application.action)
    try:
        async with TestClient(TestServer(app)) as client:
            playing = asyncio.create_task(client.post("/api/actions/capitals_play", json={"receivers": [A]},
                                                      headers={"X-AirplayVideo": "1"}))
            await entered.wait()
            stopped = await client.post("/api/actions/stop", json={"receiver": A},
                                        headers={"X-AirplayVideo": "1"})
            assert stopped.status == 200
            result = await playing
            assert result.status == 409 and "cancelled" in (await result.json())["error"]
            assert not Stream.created and not x.control.targets
    finally:
        release.set()
        await x.launcher.close()
        await x.control.close()
