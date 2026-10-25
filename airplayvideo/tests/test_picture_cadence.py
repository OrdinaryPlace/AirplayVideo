"""Numeric analysis owns only its process and never exports or deletes media."""
import asyncio
import copy
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from service import picture_cadence as module
from service.main import Application, make_app
from service.model import Store, UserError, atomic_json, default_setup
from service.recordings import Recordings
from test_lifecycle import setup


ID, OTHER = "e" * 32, "f" * 32


def result():
    return {"version": 1, "video_frames": 451, "duration_seconds": 15.0, "encoded_fps": 30.0,
        "pts": {"median_ms": 33.333, "p95_ms": 34.0, "max_ms": 35.0, "gaps_over_50ms": 0},
        "regions": [{"name": name, "thresholds": [
            {"luma_delta": delta, "changed_fraction": fraction, "updates": 150,
             "updates_per_second": 10.0, "longest_hold_ms": 100.0}
            for delta in (1, 2, 4) for fraction in (0.005, 0.02)]}
            for name in ("center", "left", "right")]}


class Output:
    def __init__(self, data, gate=None):
        self.data, self.gate, self.entered = data, gate, asyncio.Event()
    async def read(self, size):
        self.entered.set()
        if self.gate:
            await self.gate.wait()
        chunk, self.data = self.data[:size], self.data[size:]
        return chunk


class Process:
    def __init__(self, data=None, gate=None, code=0):
        self.stdout = Output(json.dumps(result()).encode() if data is None else data, gate)
        self.returncode, self.code, self.killed, self.reaped = None, code, False, False
    def kill(self):
        self.killed, self.returncode = True, -9
    async def wait(self):
        self.reaped = True
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode


@pytest.fixture
def analysis(setup, monkeypatch):
    store, control = setup
    recorder = Recordings(control)
    control.recordings = recorder
    media, report = recorder.root / (ID + ".mkv"), recorder.root / (ID + ".json")
    media.write_bytes(b"synthetic fixture only")
    atomic_json(report, {"id": ID, "status": "complete", "seconds_requested": 15,
                         "receiver_timing": {}, "keep": "unchanged"})
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    return SimpleNamespace(store=store, control=control, recorder=recorder, job=recorder.analysis,
                           media=media, report=report, process=process, spawn=spawn)


@pytest.mark.asyncio
async def test_completed_recording_gets_only_numeric_summary_without_media_change(analysis):
    x = analysis
    before = x.media.read_bytes()
    await x.job.start(ID)
    assert x.job.state()["status"] == "running"
    await x.job.task
    saved = json.loads(x.report.read_text())
    assert saved["picture_cadence"] == result() and saved["keep"] == "unchanged"
    assert x.media.read_bytes() == before and x.job.state() == {"id": ID, "status": "complete", "error": ""}
    assert x.recorder.state()["items"][0]["picture_cadence"] == result()
    args, options = x.spawn.call_args
    fd = options["pass_fds"][0]
    assert args == ("airplayvideo-analyze-picture", f"/proc/self/fd/{fd}")
    assert options["stderr"] == asyncio.subprocess.DEVNULL
    assert options["stdin"] == asyncio.subprocess.DEVNULL
    assert not ({"SUPERVISOR_TOKEN", "HASSIO_TOKEN", "MQTT_PASSWORD"} & options["env"].keys())
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not x.control.targets and x.control.stream is None


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["stream", "pending", "targets", "pending_targets", "recording", "diagnostic"])
async def test_busy_analysis_rejected_without_launch_or_files_changed(analysis, busy):
    x = analysis
    if busy == "recording":
        x.recorder.current = {"id": OTHER}
    elif busy == "diagnostic":
        x.control.diagnostics = SimpleNamespace(active=True)
    else:
        setattr(x.control, busy, {OTHER} if "targets" in busy else object())
    original = x.report.read_bytes()
    with pytest.raises(UserError):
        await x.job.start(ID)
    x.spawn.assert_not_awaited()
    assert x.report.read_bytes() == original and x.media.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["identifier", "media_symlink", "report_symlink", "unfinished", "oversize"])
async def test_only_exact_completed_regular_recording_is_eligible(analysis, invalid):
    x = analysis
    capture_id = ID
    if invalid == "identifier":
        capture_id = "../settings"
    elif invalid == "media_symlink":
        x.media.unlink()
        x.media.symlink_to(x.store.path)
    elif invalid == "report_symlink":
        x.report.unlink()
        x.report.symlink_to(x.store.path)
    elif invalid == "unfinished":
        atomic_json(x.report, {"id": ID, "status": "recording"})
    else:
        with x.media.open("r+b") as stream:
            stream.truncate(module.MAX_BYTES + 1)
    with pytest.raises(UserError):
        await x.job.start(capture_id)
    assert not x.job.active
    x.spawn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["extra_field", "nan", "wrong_rate", "too_many_frames", "oversized_output", "exit_failure"])
async def test_malformed_child_result_is_safe_and_never_persisted(analysis, failure):
    x = analysis
    value = result()
    if failure == "extra_field":
        value["private_url"] = "never display"
    elif failure == "nan":
        value["pts"]["median_ms"] = float("nan")
    elif failure == "wrong_rate":
        value["regions"][0]["thresholds"][0]["updates_per_second"] = 300
    elif failure == "too_many_frames":
        value["video_frames"] = 2001
    data = b"x" * (module.MAX_OUTPUT + 1) if failure == "oversized_output" else json.dumps(value).encode()
    process = Process(data, code=7 if failure == "exit_failure" else 0)
    x.spawn.return_value = process
    original = x.report.read_bytes()
    await x.job.start(ID)
    await x.job.task
    assert x.job.state()["status"] == "error" and x.job.state()["error"] == module.SAFE_ERROR
    assert x.report.read_bytes() == original and x.media.exists()
    assert "private_url" not in json.dumps(x.job.state()) and process.reaped


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", ["explicit", "shutdown", "timeout"])
async def test_cancel_timeout_shutdown_reap_only_own_process_and_keep_all_media(analysis, monkeypatch, cancel):
    x = analysis
    process = Process(gate=asyncio.Event())
    x.spawn.return_value = process
    if cancel == "timeout":
        monkeypatch.setattr(module, "TIMEOUT_SECONDS", 0.01)
    original = x.report.read_bytes()
    await x.job.start(ID)
    await process.stdout.entered.wait()
    if cancel == "explicit":
        with pytest.raises(UserError):
            await x.job.cancel(OTHER)
        await x.job.cancel(ID)
    elif cancel == "shutdown":
        await x.job.close()
    else:
        await x.job.task
    assert process.killed and process.reaped and not x.job.active
    assert x.job.state()["status"] == ("error" if cancel == "timeout" else "cancelled")
    assert x.report.read_bytes() == original and x.media.exists()
    assert not x.control.targets and x.control.stream is None


@pytest.mark.asyncio
async def test_immediate_cancel_and_shutdown_gate_prevent_detached_job(analysis):
    x = analysis
    await x.job.start(ID)
    await x.job.cancel(ID)
    assert not x.job.active
    await x.job.close()
    original = x.report.read_bytes()
    with pytest.raises(UserError, match="shutting down"):
        await x.job.start(ID)
    assert x.report.read_bytes() == original


@pytest.mark.asyncio
async def test_shutdown_wins_against_start_waiting_for_controller_lock(analysis):
    x = analysis
    async with x.control.lock:
        starting = asyncio.create_task(x.job.start(ID))
        await asyncio.sleep(0)
        closing = asyncio.create_task(x.job.close())
        await asyncio.sleep(0)
        assert not starting.done() and not closing.done()
    with pytest.raises(UserError, match="shutting down"):
        await starting
    await closing
    x.spawn.assert_not_awaited()
    assert not x.job.active and "picture_cadence" not in json.loads(x.report.read_text())


@pytest.mark.asyncio
async def test_spawn_cancellation_adopts_and_reaps_late_process(analysis):
    x = analysis
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return x.process
    x.spawn.side_effect = spawn
    await x.job.start(ID)
    await entered.wait()
    cancelled = asyncio.create_task(x.job.cancel(ID))
    await asyncio.sleep(0)
    release.set()
    await cancelled
    assert x.process.killed and x.process.reaped and x.job.state()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_active_analysis_blocks_competing_media_setup_and_record_removal(analysis):
    x = analysis
    process = Process(gate=asyncio.Event())
    x.spawn.return_value = process
    await x.job.start(ID)
    await process.stdout.entered.wait()
    for start in (x.control.play({}), x.control.open_browser({}), x.recorder.start({})):
        with pytest.raises(UserError, match="picture analysis"):
            await start
    with pytest.raises(UserError, match="already running"):
        await x.job.start(ID)
    with pytest.raises(UserError, match="Cancel picture analysis"):
        x.recorder.remove(ID)
    app = Application.__new__(Application)
    app.configuration_lock, app.controller, app.recordings = asyncio.Lock(), x.control, x.recorder
    request = SimpleNamespace(match_info={"action": "save"}, json=AsyncMock(return_value=default_setup()))
    with pytest.raises(UserError, match="picture analysis"):
        await app.setup(request)
    await x.job.cancel(ID)


@pytest.mark.asyncio
async def test_changed_media_identity_prevents_summary_write(analysis):
    x = analysis
    gate = asyncio.Event()
    process = Process(gate=gate)
    x.spawn.return_value = process
    original = x.report.read_bytes()
    await x.job.start(ID)
    await process.stdout.entered.wait()
    x.media.unlink()
    x.media.write_bytes(b"replacement synthetic file")
    gate.set()
    await x.job.task
    assert x.job.state()["status"] == "error" and x.report.read_bytes() == original
    assert x.media.read_bytes() == b"replacement synthetic file"


@pytest.mark.asyncio
async def test_report_write_failure_preserves_original_media_and_metadata(analysis, monkeypatch):
    x = analysis
    original = x.report.read_bytes()
    monkeypatch.setattr(module, "atomic_json", Mock(side_effect=OSError("synthetic failure")))
    await x.job.start(ID)
    await x.job.task
    assert x.job.state()["status"] == "error" and x.report.read_bytes() == original
    assert x.media.read_bytes() == b"synthetic fixture only"


@pytest.mark.asyncio
async def test_numeric_action_retains_normal_ingress_and_mutation_boundary(tmp_path, monkeypatch):
    async with ClientSession() as session:
        app = Application(Store(tmp_path), session)
        app.recordings.analysis.start = AsyncMock()
        async with TestClient(TestServer(make_app(app, web_root=tmp_path))) as client:
            assert (await client.post('/api/actions/analyze_recording', json={"id": ID}, headers={"X-AirplayVideo": "1"})).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True, web_root=tmp_path))) as client:
            assert (await client.post('/api/actions/analyze_recording', json={"id": ID})).status == 403
            response = await client.post('/api/actions/analyze_recording', json={"id": ID}, headers={"X-AirplayVideo": "1"})
            assert response.status == 200
            app.recordings.analysis.start.assert_awaited_once_with(ID)
            value = await response.json()
            assert value["recordings"]["analysis"] == {"id": None, "status": "idle", "error": ""}
