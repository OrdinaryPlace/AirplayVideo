"""Private camera media routes and source ownership, without a TV or HA Core."""
import asyncio
import json
from pathlib import Path
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import pytest_asyncio

from service import camera as module
from service.camera import CameraOutput, NativeCameraSession
from service.model import Store, private_json


def source(directory, *, native=None):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.m3u8").write_text("#EXTM3U\n#EXT-X-TARGETDURATION:2\n#EXTINF:2,\nsegment000000001.ts\n")
    (directory / "segment000000001.ts").write_bytes(b"transport-stream")
    if native:
        return SimpleNamespace(producer=SimpleNamespace(directory=directory, plan={"target": {"video_codec": native}}))
    return SimpleNamespace(config={"camera_directory": str(directory)})


@pytest_asyncio.fixture
async def output(tmp_path):
    store = Store(tmp_path / "data")
    controller = SimpleNamespace(stream=source(tmp_path / "generation-one"))
    async with aiohttp.ClientSession() as client:
        camera = CameraOutput(store, controller, client, bind_address="127.0.0.1")
        await camera.select(controller.stream)
        await camera.start()
        try:
            yield SimpleNamespace(camera=camera, controller=controller, client=client, store=store, root=tmp_path)
        finally:
            await camera.close()


@pytest.mark.asyncio
async def test_camera_origin_requires_bearer_even_when_no_video_is_selected(output):
    x = output
    base = x.camera.base_url.rsplit("/", 1)[0]
    for token in ("wrong", "é", ""):
        async with x.client.get(base + "/" + token + "/stream.m3u8") as response:
            assert response.status == 404
    x.controller.stream = None
    await x.camera.select(None)
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 503
    assert x.camera.identity["token"] not in json.dumps(x.camera.state())


@pytest.mark.asyncio
async def test_playlist_rewrites_every_segment_to_authenticated_generation_and_serves_exact_bytes(output):
    x = output
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200
        assert response.content_type == "application/vnd.apple.mpegurl"
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Referrer-Policy"] == "no-referrer"
        playlist = await response.text()
    media, = [line for line in playlist.splitlines() if line and not line.startswith("#")]
    assert media == f"/{x.camera.identity['token']}/generation-one/segment000000001.ts"
    assert str(x.root) not in playlist
    async with x.client.get(x.camera.base_url.rsplit("/", 1)[0] + media) as response:
        assert response.status == 200 and response.content_type == "video/mp2t"
        assert await response.read() == b"transport-stream"


@pytest.mark.asyncio
async def test_native_fmp4_init_map_is_private_and_old_generation_is_unavailable_after_switch(output):
    x = output
    directory = x.camera.directory
    (directory / "index.m3u8").write_text('#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4,\nsegment-00000001.m4s\n')
    (directory / "init.mp4").write_bytes(b"initialization")
    (directory / "segment-00000001.m4s").write_bytes(b"fragment")
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        playlist = await response.text()
        assert response.status == 200
    prefix = f"/{x.camera.identity['token']}/generation-one/"
    assert f'URI="{prefix}init.mp4"' in playlist
    for name, data in (("init.mp4", b"initialization"), ("segment-00000001.m4s", b"fragment")):
        async with x.client.get(x.camera.base_url + "/generation-one/" + name) as response:
            assert response.status == 200 and response.content_type == "video/mp4"
            assert await response.read() == data
    x.controller.stream = source(x.root / "generation-two")
    await x.camera.select(x.controller.stream)
    async with x.client.get(x.camera.base_url + "/generation-one/init.mp4") as response:
        assert response.status == 404
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert "/generation-two/" in await response.text()
    assert directory.exists(), "The origin does not own the producer's files"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["index.m3u8", "camera.json", "segment000000001.ts", "generation-one/snapshot.png", "generation-one/private.txt"])
async def test_unlisted_paths_are_never_served(output, name):
    async with output.client.get(output.camera.base_url + "/" + name) as response:
        assert response.status == 404


@pytest.mark.asyncio
async def test_media_symlink_fifo_or_vanished_directory_cannot_escape_or_block_origin(output):
    x = output
    directory = x.camera.directory
    path = directory / "segment000000001.ts"
    path.unlink()
    path.symlink_to(x.store.root / "camera.json")
    url = x.camera.base_url + "/generation-one/segment000000001.ts"
    async with x.client.get(url) as response:
        assert response.status == 404
    path.unlink()
    import os
    os.mkfifo(path)
    async with asyncio.timeout(2):
        async with x.client.get(url) as response:
            assert response.status == 404
    path.unlink()
    (directory / "index.m3u8").unlink()
    directory.rmdir()
    async with x.client.get(url) as response:
        assert response.status == 503
    assert not x.camera.task.done(), "Owner cleanup must not terminate camera monitoring"


@pytest.mark.asyncio
@pytest.mark.parametrize("playlist", [
    b"not-hls\nsegment000000001.ts\n", b"#EXTM3U\n\xff\n", b"#EXTM3U\nhttps://example.invalid/private\n",
    b"#EXTM3U\n../camera.json\n", b'#EXTM3U\n#EXT-X-MAP:URI="../private"\n',
    b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://example.invalid/private"\n',
    b"#EXTM3U\n" + b"#" * 65536,
])
async def test_bad_or_external_playlist_references_fail_without_relaying_them(output, playlist):
    (output.camera.directory / "index.m3u8").write_bytes(playlist)
    async with output.client.get(output.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 503
        body = await response.text()
        assert "example.invalid" not in body and "camera.json" not in body


@pytest.mark.asyncio
async def test_restart_preserves_listener_identity_and_private_permissions(output):
    x = output
    original = x.camera.base_url
    identity = private_json(x.camera.identity_path)
    assert stat.S_IMODE(x.camera.identity_path.stat().st_mode) == 0o600
    await x.camera.close()
    replacement = CameraOutput(x.store, x.controller, x.client, bind_address="127.0.0.1")
    try:
        await replacement.select(x.controller.stream)
        await replacement.start()
        assert replacement.base_url == original
        assert replacement.identity["token"] == identity["token"] and replacement.identity["port"] == identity["port"]
        assert replacement.identity["epoch"] > identity["epoch"]
        assert replacement.identity["sequence_base"] > identity["sequence_base"]
        async with x.client.get(original + "/stream.m3u8") as response:
            assert response.status == 200
    finally:
        await replacement.close()


class Process:
    def __init__(self, blocks=()):
        self.stdout = SimpleNamespace(read=AsyncMock(side_effect=[*blocks, b""]))
        self.returncode = None
        self.terminated = False

    async def wait(self):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15


@pytest.mark.asyncio
async def test_snapshot_reads_complete_png_and_caches_without_inheriting_supervisor_token(output, monkeypatch):
    x = output
    blocks = [b"\x89PNG\r\n\x1a\n", b"first-part", b"last-part"]
    process = Process(blocks)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private-token")
    for _ in range(2):
        async with x.client.get(x.camera.base_url + "/snapshot.png") as response:
            assert response.status == 200 and response.content_type == "image/png"
            assert await response.read() == b"".join(blocks)
    assert spawn.await_count == 1
    assert "SUPERVISOR_TOKEN" not in spawn.call_args.kwargs["env"]
    assert x.camera.snapshot_process is None


@pytest.mark.asyncio
async def test_snapshot_rejects_excessive_output_and_reaps_child(output, monkeypatch):
    process = Process([b"\x89PNG\r\n\x1a\n", b"too-many-bytes"])
    monkeypatch.setattr(module, "_SNAPSHOT_LIMIT", 12)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    async with output.client.get(output.camera.base_url + "/snapshot.png") as response:
        assert response.status == 503
    assert process.terminated and output.camera.snapshot_process is None
    assert output.camera.snapshot is None


@pytest.mark.asyncio
async def test_native_h264_shares_producer_but_hevc_gets_independent_h264_conversion_and_cleanup(output, monkeypatch):
    x = output
    process = Process()
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", spawn)
    h264 = source(x.root / "native-h264", native="h264")
    x.controller.stream = h264
    await x.camera.select(h264)
    assert x.camera.directory == h264.producer.directory
    assert not x.camera.owned_directory and not spawn.called
    hevc = source(x.root / "native-hevc", native="hevc")
    x.controller.stream = hevc
    await x.camera.select(hevc)
    converted = x.camera.directory
    assert converted != hevc.producer.directory and x.camera.owned_directory
    args = spawn.call_args.args
    assert args[args.index("-c:v") + 1] == "libopenh264"
    assert args[args.index("-c:a") + 1] == "copy"
    assert "min(1920,iw)" in args[args.index("-vf") + 1]
    epoch = x.camera.identity["epoch"]
    await x.camera.select(SimpleNamespace(producer=hevc.producer))
    assert x.camera.directory == converted and x.camera.identity["epoch"] == epoch
    assert spawn.await_count == 1 and not process.terminated
    await x.camera.select(None)
    assert not converted.exists() and process.terminated
    assert h264.producer.directory.exists() and hevc.producer.directory.exists()


@pytest.mark.asyncio
async def test_registration_finishes_across_source_switch_without_cancellation(output):
    x = output
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def ensure(*urls):
        assert urls == (x.camera.base_url + "/stream.m3u8", x.camera.base_url + "/snapshot.png")
        started.set()
        await release.wait()
        finished.set()

    x.camera.registration = SimpleNamespace(ensure=ensure)
    await asyncio.wait_for(started.wait(), 2)
    replacement = source(x.root / "registration-new-source")
    x.controller.stream = replacement
    async with asyncio.timeout(2):
        while x.camera.owner is not replacement:
            await asyncio.sleep(0.05)
    release.set()
    await asyncio.wait_for(finished.wait(), 2)
    assert not x.camera.task.done()


@pytest.mark.asyncio
async def test_native_camera_creates_output_before_constructor_and_caps_new_video_at_h264_1080p(tmp_path, monkeypatch):
    producer = SimpleNamespace(root=tmp_path / "run", start=AsyncMock(), close=AsyncMock(),
                               status={"state": "streaming", "quality": {"video_codec": "h264"}})

    def create(**kwargs):
        assert kwargs["output_dir"].is_dir()
        assert kwargs["ffmpeg"] == "/fixture/ffmpeg" and kwargs["ffprobe"] == "/fixture/ffprobe"
        assert kwargs["managed_lifecycle"] is True
        assert kwargs["force_h264"] is True
        return producer

    constructor = Mock(side_effect=create)
    monkeypatch.setattr(module, "NativeHLSStream", constructor)
    native = NativeCameraSession(tmp_path, config={"ffmpeg": "/fixture/ffmpeg", "ffprobe": "/fixture/ffprobe"})
    await native.prepare("https://www.youtube.com/watch?v=abcdefghijk")
    producer.start.assert_awaited_once_with("https://www.youtube.com/watch?v=abcdefghijk", "AppleTV5,3", max_resolution="1080p")
    assert native.status["phase"] == "playing" and native.status["camera_only"]
    await native.close()
    assert native.closed.is_set()
    adopted = NativeCameraSession(tmp_path, producer=producer, config={"max_resolution": "720p"})
    assert adopted.producer is producer and constructor.call_count == 1
    await adopted.prepare("https://www.youtube.com/watch?v=abcdefghijk")
    assert producer.start.await_count == 1, "Adoption preserves the already-running producer"
    limited = NativeCameraSession(tmp_path, config={"max_resolution": "720p", "ffmpeg": "/fixture/ffmpeg", "ffprobe": "/fixture/ffprobe"})
    await limited.prepare("https://www.youtube.com/watch?v=abcdefghijk")
    assert producer.start.call_args.kwargs["max_resolution"] == "720p"


@pytest.mark.asyncio
async def test_stable_playlist_sequence_and_discontinuity_advance_across_sources_and_rollover(output, monkeypatch):
    x = output

    async def playlist():
        async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
            assert response.status == 200
            lines = (await response.text()).splitlines()
        values = {line.split(":", 1)[0]: int(line.split(":", 1)[1]) for line in lines
                  if line.startswith(("#EXT-X-MEDIA-SEQUENCE:", "#EXT-X-DISCONTINUITY-SEQUENCE:"))}
        return values, lines

    first, lines = await playlist()
    assert "#EXT-X-DISCONTINUITY" in lines
    path = x.camera.directory / "index.m3u8"
    path.write_text(path.read_text().replace("#EXTM3U\n", "#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:12\n"))
    rolled, lines = await playlist()
    assert rolled["#EXT-X-MEDIA-SEQUENCE"] == first["#EXT-X-MEDIA-SEQUENCE"] + 12
    assert rolled["#EXT-X-DISCONTINUITY-SEQUENCE"] == first["#EXT-X-DISCONTINUITY-SEQUENCE"] + 1
    assert "#EXT-X-DISCONTINUITY" not in lines
    monkeypatch.setattr(module.time, "time", lambda: 1)
    x.controller.stream = source(x.root / "replacement-source")
    await x.camera.select(x.controller.stream)
    changed, lines = await playlist()
    assert changed["#EXT-X-MEDIA-SEQUENCE"] > rolled["#EXT-X-MEDIA-SEQUENCE"]
    assert changed["#EXT-X-DISCONTINUITY-SEQUENCE"] == rolled["#EXT-X-DISCONTINUITY-SEQUENCE"]
    assert "#EXT-X-DISCONTINUITY" in lines
    assert private_json(x.camera.identity_path)["epoch"] == x.camera.identity["epoch"]


@pytest.mark.asyncio
async def test_start_removes_only_stale_owned_camera_directories(output):
    x = output
    await x.camera.close()
    run = x.store.root / "run"
    run.mkdir(exist_ok=True)
    stale = run / "camera-abcdefgh"
    stale.mkdir()
    (stale / "segment000000001.ts").write_bytes(b"old-generated-video")
    unrelated = run / "private-content"
    unrelated.mkdir()
    link = run / "camera-12345678"
    link.symlink_to(unrelated, target_is_directory=True)
    replacement = CameraOutput(x.store, x.controller, x.client, bind_address="127.0.0.1")
    try:
        await replacement.start()
        assert not stale.exists()
        assert unrelated.is_dir() and link.is_symlink()
    finally:
        await replacement.close()



@pytest.mark.asyncio
@pytest.mark.parametrize("finished", [False, True])
async def test_native_camera_stop_interrupts_production_wait_or_final_drain(tmp_path, finished):
    completed = asyncio.Event()
    if finished:
        completed.set()
    producer = SimpleNamespace(wait_finished=completed.wait, close=AsyncMock(), status={"state": "streaming"})
    native = NativeCameraSession(tmp_path, producer=producer)
    watching = asyncio.create_task(native.wait())
    await asyncio.sleep(0)
    await native.close()
    await asyncio.wait_for(watching, 1)
    assert native.closed.is_set()
