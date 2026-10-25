"""The numeric frame probe owns no receivers, recordings, or browser mutations."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from service import frame_probe as module
from service.browser_vpn import BrowserVPN
from service.diagnostics import Diagnostics
from service.frame_probe import FrameProbe
from service.main import Application, make_app
from service.model import Store, UserError
from service.recordings import Recordings
from test_lifecycle import setup
from test_picture_cadence import Output


def report():
    threshold = lambda delta, fraction: {"luma_delta": delta, "changed_fraction": fraction,
        "updates": 150, "updates_per_second": 10.0, "longest_hold_ms": 100.0}
    timing = lambda value: {"count": 450, "total": value * 450, "mean": value, "p95": value, "max": value}
    return {"version": 1, "warmup_seconds": 2, "duration_seconds": 15, "requested_fps": 30,
        "capture": {"frames": 450, "fps": 30.0, "nonmonotonic_pts": 0,
            "pts": {"median_ms": 33.333, "p95_ms": 34.0, "max_ms": 35.0, "gaps_over_50ms": 0}},
        "gate": {"accepted": 450, "rejected": 0}, "converted": {"frames": 450, "fps": 30.0},
        "encoded": {"frames": 450, "fps": 30.0}, "raw_picture": {"frames": 450, "regions": [
            {"name": name, "exact_updates": 150, "exact_updates_per_second": 10.0,
             "longest_exact_hold_ms": 100.0, "thresholds": [threshold(delta, fraction)
                for delta in (1, 2, 4) for fraction in (0.005, 0.02)]}
            for name in ("center", "left", "right")]},
        "timings_ms": {"conversion": timing(1), "upload": timing(0.2),
                       "encode": timing(2), "observation": timing(0.1)}}


class Process:
    def __init__(self, data=None, gate=None, code=0):
        self.stdout = Output(json.dumps(report()).encode() if data is None else data, gate)
        self.payload = bytearray()
        self.stdin = SimpleNamespace(write=self.payload.extend, drain=AsyncMock(), close=Mock())
        self.returncode, self.code, self.killed, self.reaped = None, code, False, False
    def kill(self):
        self.killed, self.returncode = True, -9
    async def wait(self):
        self.reaped = True
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode


@pytest.fixture
def probe(setup, monkeypatch):
    store, control = setup
    control.recordings = recorder = Recordings(control)
    control.frame_probe = job = FrameProbe(control)
    control.browser.environment = {"DISPLAY": ":42.0", "XAUTHORITY": "/data/browser-sportsnet/Xauthority",
        "PULSE_SERVER": "unix:/data/browser-sportsnet/pulse.sock", "PULSE_COOKIE": "/data/browser-sportsnet/pulse-cookie",
        "AIRPLAYVIDEO_COMPANION_SOCKET": "not passed", "SUPERVISOR_TOKEN": "not passed"}
    control.browser.running = True
    control.browser.close = AsyncMock()
    control.browser.navigate = AsyncMock()
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    return SimpleNamespace(store=store, control=control, recorder=recorder, job=job, process=process, spawn=spawn)


@pytest.mark.asyncio
async def test_open_prepared_player_with_stale_error_is_measured_without_tv_or_files(probe):
    x = probe
    x.control.phase, x.control.error = "error", "Previous receiver disconnected"
    x.control.source = {"kind": "browser", "url": "https://watch.sportsnet.ca/watch/fixture_123"}
    before, runtime = copy.deepcopy(x.store.data), copy.deepcopy(x.control.status())
    files = set(x.store.root.rglob("*"))
    assert runtime["media_busy"] is False
    await x.job.start()
    await x.job.task
    assert x.job.state() == {"status": "complete", "error": "", "report": report()}
    assert x.control.status() == runtime and x.store.data == before and set(x.store.root.rglob("*")) == files
    x.control.browser.close.assert_not_awaited()
    x.control.browser.navigate.assert_not_awaited()
    assert not x.control.targets and x.control.stream is None
    args, options = x.spawn.call_args
    assert args == ("airplayvideo-frame-probe",) and options["stderr"] == asyncio.subprocess.DEVNULL
    assert not ({"SUPERVISOR_TOKEN", "AIRPLAYVIDEO_COMPANION_SOCKET", "AIRPLAYVIDEO_COMPANION_ID"} & options["env"].keys())
    config = json.loads(x.process.payload)
    assert config == x.control.source_config({"kind": "browser", "display": ":42.0"})
    assert not ({"receivers", "url", "path", "seconds"} & config.keys())
    detached = x.job.state()
    detached["report"]["capture"]["frames"] = 0
    assert x.job.state()["report"]["capture"]["frames"] == 450


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["stream", "pending", "targets", "pending_targets", "teardown", "recording", "analysis", "sync", "closed_browser"])
async def test_incompatible_lifecycle_rejects_before_process_creation(probe, busy):
    x = probe
    waiting = None
    if busy == "recording":
        x.recorder.current = {"id": "a" * 32}
    elif busy == "analysis":
        waiting = asyncio.create_task(asyncio.Event().wait())
        x.recorder.analysis.task = waiting
    elif busy == "sync":
        x.control.diagnostics = SimpleNamespace(active=True)
    elif busy == "closed_browser":
        x.control.browser.running = False
    elif busy == "teardown":
        waiting = asyncio.create_task(asyncio.Event().wait())
        x.control.retiring[object()] = waiting
    else:
        setattr(x.control, busy, {"a" * 32} if "targets" in busy else object())
    with pytest.raises(UserError):
        await x.job.start()
    x.spawn.assert_not_awaited()
    if waiting:
        waiting.cancel()
        await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["extra", "nan", "fps", "region", "duration", "timing", "oversized", "exit"])
async def test_only_bounded_complete_numeric_schema_reaches_state(probe, failure):
    x = probe
    value = report()
    if failure == "extra":
        value["private_path"] = "never return"
    elif failure == "nan":
        value["capture"]["fps"] = float("nan")
    elif failure == "fps":
        value["requested_fps"] = 60
    elif failure == "region":
        value["raw_picture"]["regions"][1]["name"] = "center"
    elif failure == "duration":
        value["duration_seconds"] = 21
    elif failure == "timing":
        value["timings_ms"]["conversion"]["max"] = 0
    data = b"x" * (module.MAX_OUTPUT + 1) if failure == "oversized" else json.dumps(value).encode()
    process = Process(data, code=5 if failure == "exit" else 0)
    x.spawn.return_value = process
    await x.job.start()
    await x.job.task
    assert x.job.state() == {"status": "error", "error": module.SAFE_ERROR, "report": None}
    assert process.reaped and not x.control.targets


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["cancel", "shutdown", "timeout"])
async def test_cancel_shutdown_timeout_reap_only_probe_and_restore_exclusivity(probe, monkeypatch, stop):
    x = probe
    process = Process(gate=asyncio.Event())
    x.spawn.return_value = process
    if stop == "timeout":
        monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.01)
    await x.job.start()
    await process.stdout.entered.wait()
    if stop == "cancel":
        await x.job.cancel()
    elif stop == "shutdown":
        await x.job.close()
    else:
        await x.job.task
    assert process.killed and process.reaped and not x.job.active
    assert x.job.state()["status"] == ("error" if stop == "timeout" else "cancelled")
    assert x.job.report is None and x.control.browser.running and not x.control.targets
    x.control.browser.close.assert_not_awaited()
    x.control.require_no_diagnostic()


@pytest.mark.asyncio
async def test_active_probe_blocks_reverse_media_route_settings_and_browser_actions(probe):
    x = probe
    x.spawn.return_value = Process(gate=asyncio.Event())
    await x.job.start()
    await x.spawn.return_value.stdout.entered.wait()
    vpn = BrowserVPN(x.store, x.control.browser, x.control, asyncio.Lock())
    diagnostic = Diagnostics(x.control, None)
    for action in (x.control.play({}), x.control.open_browser({}), x.control.close_browser(), x.recorder.start({}),
                   x.recorder.analysis.start("a" * 32), vpn.set_enabled({"enabled": False}), diagnostic.start({})):
        with pytest.raises(UserError, match="frame measurement"):
            await action
    with pytest.raises(UserError, match="already running"):
        await x.job.start()
    app = Application.__new__(Application)
    app.controller, app.recordings, app.browser = x.control, x.recorder, x.control.browser
    app.configuration_lock = asyncio.Lock()
    with pytest.raises(UserError, match="frame measurement"):
        await app.setup(SimpleNamespace(match_info={"action": "save"}, json=AsyncMock(return_value={})))
    x.control.browser.control = AsyncMock()
    with pytest.raises(UserError, match="frame measurement"):
        await app.action(SimpleNamespace(match_info={"action": "browser"}, json=AsyncMock(return_value={"action": "reload"})))
    x.control.browser.control.assert_not_awaited()
    await x.job.cancel()


@pytest.mark.asyncio
async def test_shutdown_wins_against_start_waiting_for_controller_lock(probe):
    x = probe
    async with x.control.lock:
        starting = asyncio.create_task(x.job.start())
        await asyncio.sleep(0)
        closing = asyncio.create_task(x.job.close())
        await asyncio.sleep(0)
    with pytest.raises(UserError, match="shutting down"):
        await starting
    await closing
    with pytest.raises(UserError, match="shutting down"):
        await x.job.start()
    x.spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_at_subprocess_spawn_boundary_adopts_child(probe):
    x = probe
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return x.process
    x.spawn.side_effect = spawn
    await x.job.start()
    await entered.wait()
    cancelling = asyncio.create_task(x.job.cancel())
    await asyncio.sleep(0)
    release.set()
    await cancelling
    assert x.process.killed and x.process.reaped and x.job.state()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_frame_probe_actions_keep_ingress_and_json_mutation_boundary(tmp_path):
    async with ClientSession() as session:
        app = Application(Store(tmp_path), session)
        app.frame_probe.start, app.frame_probe.cancel = AsyncMock(), AsyncMock()
        async with TestClient(TestServer(make_app(app, web_root=tmp_path))) as client:
            assert (await client.post('/api/actions/measure_frame_stages', json={}, headers={'X-AirplayVideo': '1'})).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True, web_root=tmp_path))) as client:
            assert (await client.post('/api/actions/measure_frame_stages', json={})).status == 403
            for action in ('measure_frame_stages', 'cancel_frame_stages'):
                response = await client.post('/api/actions/' + action, json={}, headers={'X-AirplayVideo': '1'})
                assert response.status == 200 and (await response.json())['frame_probe']['status'] == 'idle'
        app.frame_probe.start.assert_awaited_once_with()
        app.frame_probe.cancel.assert_awaited_once_with()
