"""Selected-source camera lifecycle with receiver transport mocked out."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service import controller as module
from service.camera import NativeCameraSession
from service.model import UserError
from test_capitals import context as game_context
from test_lifecycle import A, B, Stream, request, setup
from test_native_controller import native


def camera_output(allocate):
    return SimpleNamespace(enabled=True, allocate=allocate, prepare=AsyncMock(),
                           activate=AsyncMock(), discard_prepared=AsyncMock(), cancel_drain=AsyncMock())


@pytest.fixture
def camera(setup, tmp_path):
    store, control = setup
    directories = []

    def allocate():
        directory = tmp_path / ("camera-" + str(len(directories)))
        directory.mkdir()
        directories.append(directory)
        return directory

    control.camera = camera_output(allocate)
    return SimpleNamespace(store=store, control=control, directories=directories)


def only_camera(**changes):
    return {"receivers": [], "camera": True, **changes}


class Producer:
    def __init__(self):
        self.status = {"state": "streaming", "error": ""}
        self.finished = asyncio.Event()
        self.closed = False
        self.start = AsyncMock()

    async def wait_finished(self):
        await self.finished.wait()

    async def close(self):
        self.closed = True
        self.status["state"] = "closed"
        self.finished.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    {"mode": "hdhomerun", "channel": "ABCDEF12:4.1"},
    {"mode": "browser", "browser_source": "url", "url": "https://example.com/"},
    {"mode": "generated", "generated": {"title": "Camera fixture", "duration_seconds": 2}},
])
async def test_play_without_tvs_has_one_source_and_global_stop_releases_it(camera, source):
    c = camera.control
    await c.play(only_camera(**source))
    active = c.stream
    assert c.phase == "playing" and c.source and c.status()["media_busy"]
    assert not c.targets and not c.receivers and not active.commands
    assert active.config["camera_directory"] == str(camera.directories[0])
    assert active.config["camera_only"] is True
    assert len(Stream.created) == 1
    await c.stop()
    assert active.closed and c.stream is None and c.pending is None
    assert c.phase == "idle" and c.source is None and not c.status()["media_busy"]
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled, camera_flag", [(False, True), (True, False), (True, "true")])
async def test_no_tvs_requires_explicit_enabled_camera_before_any_source_mutation(camera, enabled, camera_flag):
    c = camera.control
    c.camera.enabled = enabled
    with pytest.raises(UserError):
        await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.1", camera=camera_flag))
    assert c.stream is None and c.pending is None and not Stream.created and not camera.directories
    assert c.generation == 0


@pytest.mark.asyncio
async def test_stop_last_tv_keeps_camera_source_and_next_tv_joins_same_stream(camera):
    c = camera.control
    await c.play(request())
    active = c.stream
    await c.stop(A)
    assert c.stream is active and not active.closed and c.phase == "playing"
    assert c.source and not c.targets and not c.receivers
    assert active.commands[-1] == {"action": "remove", "id": A}
    await c.stop(A)  # an idle TV stop cannot stop the camera either
    assert not active.closed
    await c.play(request(B), add=True)
    assert c.stream is active and c.targets == {B} and len(Stream.created) == 1
    assert active.commands[-1] == {"action": "add", "id": B, "slot": 1}
    await c.close()
    assert active.closed


@pytest.mark.asyncio
async def test_camera_source_replacement_prepares_before_retiring_and_failure_keeps_old_camera(camera):
    c = camera.control
    await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.1"))
    original, source = c.stream, copy.deepcopy(c.source)
    Stream.fail = True
    with pytest.raises(UserError, match="Source unavailable"):
        await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.2"))
    assert c.stream is original and not original.closed and c.source == source and c.phase == "playing"
    assert c.pending is None and Stream.created[-1].closed
    Stream.fail = False
    await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.2"))
    assert original.closed and c.stream is not original
    assert c.source["key"] == "ABCDEF12:4.2" and not c.targets
    assert len({stream.config["camera_directory"] for stream in Stream.created}) == 3
    await c.close()


@pytest.mark.asyncio
async def test_global_stop_cancels_camera_only_warmup_without_a_receiver_command(camera):
    c = camera.control
    Stream.gate = asyncio.Event()
    starting = asyncio.create_task(c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.1")))
    while not Stream.created:
        await asyncio.sleep(0)
    await Stream.created[0].entered.wait()
    await c.stop()
    with pytest.raises(UserError, match="cancelled"):
        await starting
    assert c.stream is None and c.pending is None and c.phase == "idle"
    assert all(stream.closed and not stream.commands for stream in Stream.created)
    await c.close()


@pytest.mark.asyncio
async def test_receiver_failure_does_not_terminate_the_selected_camera(camera):
    c = camera.control
    await c.play(request())
    active = c.stream
    await c.event(active, "receiver_error", {"id": A, "message": "Fixture receiver gone"})
    await asyncio.sleep(0)
    assert c.stream is active and not active.closed and c.source
    await c.close()


@pytest.fixture
def native_camera(camera, monkeypatch):
    class Session:
        created = []
        gate = None

        def __init__(self, root, *, config, on_event):
            self.config, self.on_event = config, on_event
            self.closed = False
            self.preparing, self.done = asyncio.Event(), asyncio.Event()
            self.created.append(self)

        @property
        def status(self):
            return {"kind": "native", "phase": "stopped" if self.closed else "playing",
                    "camera_only": True, "error": "", "physical_playback_verified": False}

        async def prepare(self, url):
            self.url = url
            self.preparing.set()
            if self.gate:
                await self.gate.wait()
            if self.closed:
                raise UserError("Playback was cancelled")

        async def connect(self):
            pass

        async def wait(self):
            await self.done.wait()
            return self.status

        async def close(self):
            self.closed = True
            self.done.set()
            if self.gate:
                self.gate.set()

    monkeypatch.setattr(module, "NativeCameraSession", Session)
    camera.control.browser.close = AsyncMock()
    return camera, Session


@pytest.mark.asyncio
async def test_direct_youtube_without_tv_owns_camera_producer_and_stops_cleanly(native_camera):
    x, Session = native_camera
    c = x.control
    await c.play(only_camera(mode="browser", browser_source="youtube", url="https://youtu.be/aqz-KE-bpKQ"))
    active = c.stream
    assert active is Session.created[0] and active.status["camera_only"]
    assert c.source["kind"] == "youtube" and c.phase == "playing" and not c.targets and not c.receivers
    assert not Stream.created and c.native_report["physical_playback_verified"] is False
    await c.stop(A)
    assert c.stream is active and not active.closed
    await c.stop()
    assert active.closed and c.stream is None and c.source is None and c.phase == "idle"
    await c.close()


@pytest.mark.asyncio
async def test_direct_camera_warmup_is_cancelled_by_global_stop(native_camera):
    x, Session = native_camera
    c = x.control
    Session.gate = asyncio.Event()
    starting = asyncio.create_task(c.play(only_camera(mode="browser", browser_source="youtube", url="https://youtu.be/aqz-KE-bpKQ")))
    while not Session.created:
        await asyncio.sleep(0)
    pending = Session.created[0]
    await pending.preparing.wait()
    await c.stop()
    with pytest.raises(UserError, match="cancelled"):
        await starting
    assert pending.closed and c.stream is None and c.pending is None and c.phase == "idle"
    await c.close()


@pytest.mark.asyncio
async def test_targeted_native_tv_stop_preserves_selected_camera_source(native, tmp_path):
    _, c, _, _ = native
    c.camera = camera_output(lambda: tmp_path / "camera")
    await c.play({"mode": "browser", "browser_source": "youtube", "url": "https://youtu.be/aqz-KE-bpKQ",
                  "receivers": [A], "camera": True})
    source = copy.deepcopy(c.source)
    original = c.stream
    original.producer = producer = Producer()
    await c.stop(A)
    assert c.stream is not None and c.source == source and c.phase == "playing"
    assert not c.targets and not c.receivers and c.status()["media_busy"]
    assert c.stream.producer is producer and original.producer is None and not producer.closed
    await asyncio.wait_for(c.close(), 1)
    assert producer.closed


@pytest.mark.asyncio
async def test_stop_pending_native_tv_keeps_prior_camera_only_source_playing(native, tmp_path):
    _, c, Session, _ = native
    c.camera = camera_output(lambda: tmp_path / "camera")
    await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.1"))
    old = c.stream
    Session.gate = asyncio.Event()
    starting = asyncio.create_task(c.play({"mode": "browser", "browser_source": "youtube",
                                          "url": "https://youtu.be/aqz-KE-bpKQ", "receivers": [B], "camera": True}))
    while not Session.created:
        await asyncio.sleep(0)
    await Session.created[0].entered.wait()
    await c.stop(B)
    with pytest.raises(UserError, match="cancelled"):
        await starting
    assert c.stream is old and not old.closed and c.phase == "playing"
    assert c.source and not c.targets and c.pending is None
    await c.close()


@pytest.mark.asyncio
async def test_capitals_manual_play_can_select_camera_without_a_tv(game_context, tmp_path):
    x = game_context
    x.control.camera = camera_output(lambda: tmp_path / "camera")
    await x.launcher.play_current({"receivers": [], "camera": True})
    assert x.launcher.state()["status"] == "started"
    assert x.control.source["profile"] == "sportsnet" and x.control.phase == "playing"
    assert x.control.stream and not x.control.stream.commands and not x.control.targets
    await x.launcher.close()
    await x.control.close()


@pytest.mark.asyncio
async def test_adopted_native_camera_does_not_restart_producer_and_close_interrupts_drain(tmp_path):
    producer = Producer()
    session = NativeCameraSession(tmp_path, config={"max_resolution": "720p"}, producer=producer)
    await session.prepare("https://www.youtube.com/watch?v=aqz-KE-bpKQ")
    producer.start.assert_not_awaited()
    waiting = asyncio.create_task(session.wait())
    await asyncio.sleep(0)
    await session.close()
    await asyncio.wait_for(waiting, 1)
    assert producer.closed and session.closed.is_set()


def replacement_request(kind):
    if kind == "native":
        return {"mode": "browser", "browser_source": "youtube", "url": "https://youtu.be/aqz-KE-bpKQ",
                "receivers": [B], "camera": True}
    return request(B, channel="ABCDEF12:4.2")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["broadcast", "native"])
async def test_camera_prewarm_must_finish_before_replacing_current_source_and_tv(camera, native, kind):
    c = camera.control
    assert native[1] is c
    await c.play(request())
    old, source = c.stream, copy.deepcopy(c.source)
    await c.event(old, "streaming", {"id": A})
    warming, release = asyncio.Event(), asyncio.Event()
    order = []

    async def prepare(_stream):
        warming.set()
        await release.wait()
        order.append("camera_ready")

    async def activate(_stream):
        order.append("activate")

    original_close = old.close

    async def close_old():
        order.append("old_close")
        await original_close()

    old.close = close_old
    c.camera.prepare.side_effect = prepare
    c.camera.activate.reset_mock()
    c.camera.activate.side_effect = activate
    starting = asyncio.create_task(c.play(replacement_request(kind)))
    try:
        await asyncio.wait_for(warming.wait(), 1)
        assert c.stream is old and not old.closed and c.source == source and c.targets == {A}
        assert c.receivers[A]["state"] == "streaming" and c.pending is not old
        c.camera.activate.assert_not_awaited()
        assert not order
    finally:
        release.set()
    await starting
    assert order == ["camera_ready", "activate", "old_close"]
    assert old.closed and c.stream is not old and c.targets == {B}
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["broadcast", "native"])
async def test_camera_prewarm_error_preserves_previous_source_and_tv(camera, native, kind):
    c = camera.control
    await c.play(request())
    old, source = c.stream, copy.deepcopy(c.source)
    c.camera.activate.reset_mock()
    c.camera.discard_prepared.reset_mock()
    pending = []

    async def unavailable(stream):
        pending.append(stream)
        raise UserError("Fixture camera prewarm failed")

    c.camera.prepare.side_effect = unavailable
    with pytest.raises(UserError, match="camera prewarm failed"):
        await c.play(replacement_request(kind))
    assert c.stream is old and not old.closed and c.source == source and c.targets == {A}
    assert c.phase == "playing" and c.pending is None and c.status()["media_busy"]
    assert len(pending) == 1 and pending[0].closed
    c.camera.activate.assert_not_awaited()
    c.camera.discard_prepared.assert_awaited_once_with(pending[0])
    await c.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["broadcast", "native"])
async def test_scoped_stop_during_camera_prewarm_cancels_new_source_and_keeps_old_camera(camera, native, kind):
    c = camera.control
    await c.play(only_camera(mode="hdhomerun", channel="ABCDEF12:4.1"))
    old, source = c.stream, copy.deepcopy(c.source)
    warming, release = asyncio.Event(), asyncio.Event()

    async def prepare(_stream):
        warming.set()
        await release.wait()

    c.camera.prepare.side_effect = prepare
    c.camera.activate.reset_mock()
    c.camera.discard_prepared.reset_mock()
    starting = asyncio.create_task(c.play(replacement_request(kind)))
    try:
        await asyncio.wait_for(warming.wait(), 1)
        pending = c.pending
        assert c.pending_targets == {B}
        await c.stop(B)
        assert pending.closed and c.stream is old and not old.closed
        assert c.source == source and c.phase == "playing" and not c.targets
        c.camera.activate.assert_not_awaited()
    finally:
        release.set()
    with pytest.raises(UserError, match="cancelled"):
        await starting
    assert c.stream is old and not old.closed and c.source == source and c.phase == "playing"
    assert not c.targets and not c.pending_targets and c.pending is None
    c.camera.activate.assert_not_awaited()
    c.camera.discard_prepared.assert_awaited_once_with(pending)
    await c.close()


@pytest.mark.asyncio
async def test_short_camera_clip_completed_during_prewarm_is_replayed_once_after_activation(camera, monkeypatch):
    c = camera.control
    order, retained = [], []
    original_start, original_event = Stream.start, c.event
    completed = {"code": 0, "requested": False, "completed": True}

    async def event(stream, name, fields):
        if name == "process_exit":
            order.append("pending_exit" if stream is c.pending else "active_exit")
        await original_event(stream, name, fields)

    async def short_clip(stream):
        await original_start(stream)
        stream.exit_result = completed.copy()
        await c.event(stream, "process_exit", stream.exit_result)

    async def activate(stream):
        assert stream.exit_result == completed and stream is c.pending
        order.append("activate")

    def retain(stream):
        order.append("retain")
        retained.append(stream)
        return True

    async def cancel_drain():
        for stream in retained:
            await stream.close()

    c.event = event
    monkeypatch.setattr(Stream, "start", short_clip)
    c.camera.activate.side_effect = activate
    c.camera.retain_completed = Mock(side_effect=retain)
    c.camera.cancel_drain.side_effect = cancel_drain
    await c.play(only_camera(mode="generated", generated={"title": "Short fixture", "duration_seconds": 1}))
    finished = Stream.created[0]
    assert order == ["pending_exit", "activate", "active_exit", "retain"]
    c.camera.retain_completed.assert_called_once_with(finished)
    assert retained == [finished] and not finished.closed
    runtime = c.status()
    assert runtime["phase"] == "idle" and runtime["source"] is None and not runtime["media_busy"]
    assert c.stream is None and c.pending is None and not c.targets and not c.error
    await c.close()
    assert finished.closed
