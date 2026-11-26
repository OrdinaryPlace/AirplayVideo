"""Selected-source camera lifecycle with receiver transport mocked out."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service import controller as module
from service.camera import NativeCameraSession
from service.model import UserError
from test_capitals import context as game_context
from test_lifecycle import A, B, Stream, request, setup
from test_native_controller import native


@pytest.fixture
def camera(setup, tmp_path):
    store, control = setup
    directories = []

    def allocate():
        directory = tmp_path / ("camera-" + str(len(directories)))
        directory.mkdir()
        directories.append(directory)
        return directory

    control.camera = SimpleNamespace(enabled=True, allocate=allocate)
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
    c.camera = SimpleNamespace(enabled=True, allocate=lambda: tmp_path / "camera")
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
    c.camera = SimpleNamespace(enabled=True, allocate=lambda: tmp_path / "camera")
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
    x.control.camera = SimpleNamespace(enabled=True, allocate=lambda: tmp_path / "camera")
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
