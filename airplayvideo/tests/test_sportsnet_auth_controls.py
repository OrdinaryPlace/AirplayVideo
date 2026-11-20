"""Saved-login controls share safe status without collecting credentials or TVs."""
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service.model import Store, default_setup
from service.mqtt import HomeAssistant, discovery_documents, sportsnet_auth_state
from test_capitals_controls import run_ui as run_playback_ui


WEB = Path(__file__).resolve().parents[1] / "web"
SOURCE = (WEB / "app.js").read_text()


@pytest.fixture
def controls(tmp_path):
    store = Store(tmp_path)
    store.setup(default_setup())
    controller = SimpleNamespace(play=AsyncMock(), stop=AsyncMock(), error="", status=lambda: {
        "targets": [], "source": None, "receivers": {}, "error": ""})
    channels = SimpleNamespace(rows=[], public=lambda: [])
    auth = SimpleNamespace(request=AsyncMock(), state=lambda: {
        "status": "sign_in_required", "busy": False, "available": True})
    return SimpleNamespace(store=store, controller=controller, channels=channels, auth=auth)


def test_ha_sign_in_discovery_is_global_without_a_gateway_or_receiver(controls):
    entries = {topic: value for topic, value in discovery_documents(controls.store, controls.channels).items()
               if "_sportsnet/" in topic}
    assert len(entries) == 2
    button = next(value for topic, value in entries.items() if "/button/" in topic)
    sensor = next(value for topic, value in entries.items() if "/sensor/" in topic)
    assert button["name"] == "Sign in" and button["qos"] == 0 and button["retain"] is False
    assert json.loads(button["payload_press"]) == {"action": "sportsnet_sign_in"}
    assert button["availability_mode"] == "all"
    assert button["availability"][0]["topic"].endswith("/availability")
    assert button["availability"][1]["topic"] == sensor["state_topic"]
    assert "value_json.available" in button["availability"][1]["value_template"]
    assert "value_json.status != 'permission_required'" in button["availability"][1]["value_template"]
    assert sensor["value_template"] == "{{ value_json.status }}"
    assert button["device"] == sensor["device"]
    before = {topic: value["unique_id"] for topic, value in entries.items()}
    controls.store.data["setup"]["browser"]["vpn_name"] = "Renamed route"
    after = {topic: value["unique_id"] for topic, value in discovery_documents(controls.store, controls.channels).items()
             if "_sportsnet/" in topic}
    assert before == after


@pytest.mark.parametrize("condition", ["incomplete", "browser_disabled"])
def test_discovery_does_not_offer_sign_in_without_enabled_browser_setup(controls, condition):
    if condition == "incomplete":
        controls.store.data["setup"]["complete"] = False
    else:
        controls.store.data["setup"]["modes"]["browser"] = False
    assert not any("_sportsnet/" in topic for topic in discovery_documents(controls.store, controls.channels))


@pytest.mark.asyncio
async def test_mqtt_sign_in_requests_one_background_attempt_without_playback(controls):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.sportsnet_auth = controls.auth
    await ha.command({"action": "sportsnet_sign_in"})
    controls.auth.request.assert_awaited_once_with()
    controls.controller.play.assert_not_awaited()
    controls.controller.stop.assert_not_awaited()
    assert controls.controller.error == ""


@pytest.mark.asyncio
async def test_retained_sign_in_command_cannot_replay_after_reconnect(controls):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.loop = SimpleNamespace(call_soon_threadsafe=Mock())
    payload = {"action": "sportsnet_sign_in"}
    message = SimpleNamespace(topic=ha.base + "/command", payload=json.dumps(payload).encode(), retain=True)
    ha.on_message(None, None, message)
    ha.loop.call_soon_threadsafe.assert_not_called()
    message.retain = False
    ha.on_message(None, None, message)
    ha.loop.call_soon_threadsafe.assert_called_once_with(ha.schedule, payload)


@pytest.mark.asyncio
async def test_mqtt_publishes_only_fixed_status_and_booleans(controls):
    ha = HomeAssistant(controls.store, controls.controller, controls.channels, None)
    ha.sportsnet_auth = controls.auth
    ha.connected = True
    ha.client = Mock()
    controls.auth.state = lambda: {"status": "signed_in", "busy": False, "available": True,
                                   "account": "private fixture", "url": "private fixture", "password": "private fixture"}
    ha.publish_state()
    call = next(call for call in ha.client.publish.call_args_list if call.args[0].endswith("/sportsnet_auth/state"))
    assert json.loads(call.args[1]) == {"status": "signed_in", "busy": False, "available": True}
    assert call.kwargs == {"qos": 1, "retain": True}


@pytest.mark.parametrize("status", ["private fixture", {"private": "fixture"}, None])
def test_unrecognized_auth_status_fails_closed_without_echoing_values(status):
    assert sportsnet_auth_state({"status": status, "busy": True, "available": True}) == {
        "status": "unavailable", "busy": False, "available": False}


def run_ui(script):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for browser controls checks")
    functions = []
    for name in ("sportsnetAuthPresentation", "renderSportsnetAuth"):
        match = re.search(rf"^function {name}\([^\n]*\)\s*\{{.*?^\}}", SOURCE, re.M | re.S)
        assert match, name
        functions.append(match.group())
    handler = re.search(r"^\$\('sportsnetSignIn'\)\.onclick=[^\n]+", SOURCE, re.M).group()
    harness = """
const assert=require('node:assert/strict');
const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,{});return nodes.get(id);};
let state={sportsnet_auth:{status:'sign_in_required',available:true,busy:false}},calls=[],expanded=0;
function act(action){calls.push(action);}
function expandPreview(){expanded++;}
"""
    result = subprocess.run([node], input=harness + "\n".join(functions) + handler + "\n" + script,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


def test_sign_in_button_uses_saved_login_action_and_fixed_status_text():
    run_ui("""
renderSportsnetAuth();assert.equal($('sportsnetSignIn').hidden,false);
assert.equal($('sportsnetSignIn').textContent,'Sign in to Sportsnet');
$('sportsnetSignIn').onclick();assert.deepEqual(calls,['sportsnet_sign_in']);
assert.equal(expanded,0);
state.sportsnet_auth.status='private fixture';renderSportsnetAuth();
assert.equal($('sportsnetAuthStatus').hidden,true);assert.equal($('sportsnetAuthStatus').textContent,'');
state.sportsnet_auth.available=false;renderSportsnetAuth();assert.equal($('sportsnetSignIn').hidden,true);
""")


@pytest.mark.parametrize("status", ["verification_required", "saved_login_required"])
def test_verification_opens_existing_preview_instead_of_resubmitting_login(status):
    run_ui("state.sportsnet_auth.status=" + json.dumps(status) + ";" + """
renderSportsnetAuth();assert.equal($('sportsnetSignIn').textContent,'Open Sportsnet sign-in');
$('sportsnetSignIn').onclick();assert.equal(expanded,1);assert.deepEqual(calls,[]);
""")


def test_busy_and_signed_in_presentations_disable_redundant_sign_in():
    run_ui("""
for(const status of ['signed_in','playing'])assert.equal(sportsnetAuthPresentation({status}).disabled,true);
assert.equal(sportsnetAuthPresentation({status:'signing_in',busy:true}).disabled,true);
assert.equal(sportsnetAuthPresentation({status:'failed'}).label,'Retry Sportsnet sign-in');
""")


def test_enabling_opens_permission_flow_without_granting_access_or_submitting_login():
    run_ui("""
state.sportsnet_auth.status='permission_required';renderSportsnetAuth();
assert.equal($('sportsnetSignIn').textContent,'Enable Sportsnet sign-in');
assert.equal(sportsnetAuthPresentation(state.sportsnet_auth).disabled,false);
assert.deepEqual(calls,[]);
$('sportsnetSignIn').onclick();assert.deepEqual(calls,['sportsnet_enable']);
assert.equal(expanded,0);
""")


def test_manual_sign_in_response_leaves_stop_available_without_a_tv_or_playback_phase():
    run_playback_ui("""
state.runtime.phase='idle';state.runtime.targets=[];
state.sportsnet_auth={status:'sign_in_required',available:true,busy:false};
updateButtons();assert.equal($('stopAll').disabled,true);
api=async(path,body)=>{
  calls.push({path,body:structuredClone(body)});
  return {...state,sportsnet_auth:{status:'signing_in',available:true,busy:true}};
};
// The HTTP request ends before the background login attempt. Rendering must
// use its returned auth state after the ordinary action-busy flag clears.
await act('sportsnet_sign_in');
assert.equal(busy,false);assert.equal(busyAction,'');
assert.equal(state.runtime.phase,'idle');assert.deepEqual(state.runtime.targets,[]);
assert.equal(state.sportsnet_auth.busy,true);
assert.equal($('stopAll').disabled,false);
assert.equal($('sportsnetSignIn').disabled,true);
assert.deepEqual(calls,[{path:'api/actions/sportsnet_sign_in',body:{}}]);
state.sportsnet_auth={status:'saved_login_required',available:true,busy:false};
updateButtons();assert.equal($('stopAll').disabled,true);
""")
