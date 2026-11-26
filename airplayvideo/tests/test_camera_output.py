"""Private camera media routes and source ownership, without a TV or HA Core."""
import asyncio
import json
from pathlib import Path
import shutil
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import aiohttp
import pytest
import pytest_asyncio

from service import camera as module
from service.camera import CameraOutput, NativeCameraSession
from service.model import Store, UserError, private_json


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
    transferred = hevc.producer
    hevc.producer = None
    await x.camera.select(SimpleNamespace(producer=transferred))
    assert x.camera.directory == converted and x.camera.identity["epoch"] == epoch
    assert spawn.await_count == 1 and not process.terminated
    await x.camera.select(None)
    assert not converted.exists() and process.terminated
    assert h264.producer.directory.exists() and transferred.directory.exists()


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


def complete_stream(stream):
    directory = Path(stream.config["camera_directory"])
    with (directory / "index.m3u8").open("a") as playlist:
        playlist.write("#EXT-X-ENDLIST\n")
    stream.close = AsyncMock(side_effect=lambda: shutil.rmtree(directory))
    return stream


@pytest.mark.asyncio
async def test_completed_camera_tail_serves_final_playlist_and_segments_until_global_stop(output):
    x = output
    stream = complete_stream(x.controller.stream)
    assert x.camera.retain_completed(stream)
    assert 19 < x.camera.drain_deadline - module.time.monotonic() <= 20
    x.controller.stream = None
    # Wait beyond one monitor cycle: a cleared Controller source must retain
    # completed files, without keeping a decoder, tuner or engine alive.
    await asyncio.sleep(0.6)
    assert x.camera.owner is stream and x.camera.state()["draining"]
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "#EXT-X-ENDLIST" in await response.text()
    async with x.client.get(x.camera.base_url + "/generation-one/segment000000001.ts") as response:
        assert response.status == 200 and await response.read() == b"transport-stream"
    stream.close.assert_not_awaited()
    await asyncio.wait_for(x.camera.cancel_drain(), 1)
    stream.close.assert_awaited_once()
    assert not Path(stream.config["camera_directory"]).exists() and not x.camera.state()["active"]
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 503


@pytest.mark.asyncio
async def test_new_source_immediately_replaces_retained_tail_and_denies_old_generation(output):
    x = output
    previous = complete_stream(x.controller.stream)
    assert x.camera.retain_completed(previous)
    x.controller.stream = replacement = source(x.root / "after-completed-source")
    await asyncio.wait_for(x.camera.select(replacement), 1)
    previous.close.assert_awaited_once()
    assert not x.camera.state()["draining"] and x.camera.owner is replacement
    async with x.client.get(x.camera.base_url + "/generation-one/segment000000001.ts") as response:
        assert response.status == 404
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "/after-completed-source/" in await response.text()


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["expiry", "close"])
async def test_retained_tail_releases_files_on_deadline_or_app_close(output, stop):
    x = output
    stream = complete_stream(x.controller.stream)
    assert x.camera.retain_completed(stream)
    x.controller.stream = None
    if stop == "expiry":
        x.camera.drain_deadline = module.time.monotonic() - 1
        async with asyncio.timeout(2):
            while x.camera.owner is not None:
                await asyncio.sleep(0.05)
    else:
        await asyncio.wait_for(x.camera.close(), 1)
    stream.close.assert_awaited_once()
    assert not x.camera.state()["active"] and not x.camera.state()["draining"]
    assert not Path(stream.config["camera_directory"]).exists()


@pytest.mark.asyncio
async def test_completed_unselected_or_unready_sources_stay_with_controller_cleanup(output):
    x = output
    unrelated = complete_stream(source(x.root / "unselected"))
    assert not x.camera.retain_completed(unrelated)
    (x.camera.directory / "index.m3u8").unlink()
    assert not x.camera.retain_completed(x.controller.stream)
    assert x.camera.draining is None


def pending_camera(x, stream):
    x.controller.pending = stream
    x.controller.generation = getattr(x.controller, "generation", 0) + 1
    return stream


@pytest.mark.asyncio
async def test_prewarm_keeps_old_camera_serving_until_complete_candidate_is_atomically_activated(output):
    x = output
    old = x.camera.owner
    directory = x.root / "prewarming-source"
    directory.mkdir()
    candidate = pending_camera(x, SimpleNamespace(config={"camera_directory": str(directory)}))
    preparing = asyncio.create_task(x.camera.prepare(candidate))
    async with asyncio.timeout(1):
        while not x.camera.has_prepared(candidate):
            await asyncio.sleep(0.01)
    # A playlist alone is insufficient; the advertised final segment must exist.
    (directory / "index.m3u8").write_text("#EXTM3U\n#EXTINF:2,\nsegment000000001.ts\n")
    await asyncio.sleep(0.1)
    assert not preparing.done() and x.camera.owner is old
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "/generation-one/" in await response.text()
    (directory / "segment000000001.ts").write_bytes(b"new-media")
    await asyncio.wait_for(preparing, 1)
    assert x.camera.owner is old, "Successful prewarm does not commit playback"
    await x.camera.activate(candidate)
    # During Controller's old-TV teardown, activated pending owns the camera.
    await asyncio.sleep(0.6)
    assert x.camera.owner is candidate and not x.camera.has_prepared(candidate)
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "/prewarming-source/" in await response.text()
    async with x.client.get(x.camera.base_url + "/generation-one/segment000000001.ts") as response:
        assert response.status == 404
    x.controller.stream, x.controller.pending = candidate, None


@pytest.mark.asyncio
async def test_cancelling_hevc_prewarm_reaps_only_staging_converter_promptly_and_preserves_current(output, monkeypatch):
    x = output
    old = x.camera.owner
    process = Process()
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(return_value=process))
    candidate = pending_camera(x, source(x.root / "prewarm-hevc", native="hevc"))
    preparing = asyncio.create_task(x.camera.prepare(candidate))
    async with asyncio.timeout(1):
        while not x.camera.has_prepared(candidate) or x.camera.prepared[id(candidate)].transcoder is None:
            await asyncio.sleep(0.01)
    staged_directory = x.camera.prepared[id(candidate)].directory
    x.controller.pending = None
    x.controller.generation += 1
    with pytest.raises(UserError, match="cancelled"):
        await asyncio.wait_for(preparing, 0.3)
    assert process.terminated and not staged_directory.exists()
    assert candidate.producer.directory.is_dir()
    assert x.camera.owner is old and x.camera.ready and not x.camera.prepared
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "/generation-one/" in await response.text()


@pytest.mark.asyncio
async def test_prewarm_timeout_preserves_served_camera_and_stream_owned_directory(output, monkeypatch):
    x = output
    monkeypatch.setattr(module, "_PREPARE_SECONDS", 0)
    directory = x.root / "unready-camera"
    directory.mkdir()
    candidate = pending_camera(x, SimpleNamespace(config={"camera_directory": str(directory)}))
    with pytest.raises(UserError, match="did not produce video in time"):
        await x.camera.prepare(candidate)
    assert directory.is_dir() and x.camera.owner is x.controller.stream and x.camera.ready
    assert not x.camera.prepared


@pytest.mark.asyncio
async def test_cancel_before_activation_lock_commit_keeps_old_output_until_explicit_discard(output):
    x = output
    old = x.camera.owner
    candidate = pending_camera(x, source(x.root / "ready-but-cancelled"))
    await x.camera.prepare(candidate)
    await x.camera.selection_lock.acquire()
    activating = asyncio.create_task(x.camera.activate(candidate))
    await asyncio.sleep(0)
    x.controller.pending = None
    x.controller.generation += 1
    x.camera.selection_lock.release()
    with pytest.raises(UserError, match="cancelled"):
        await activating
    assert x.camera.owner is old and x.camera.ready
    await x.camera.discard_prepared(candidate)
    assert not x.camera.prepared and Path(candidate.config["camera_directory"]).is_dir()


@pytest.mark.asyncio
async def test_hevc_prewarm_activation_and_producer_adoption_keep_converter_and_generation(output, monkeypatch):
    x = output
    process = Process()

    async def spawn(*args, **options):
        source(Path(args[-1]).parent)
        return process

    create = AsyncMock(side_effect=spawn)
    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", create)
    candidate = pending_camera(x, source(x.root / "new-native-hevc", native="hevc"))
    old = x.camera.owner
    await x.camera.prepare(candidate)
    assert x.camera.owner is old and x.camera.ready
    await x.camera.activate(candidate)
    converted, epoch = x.camera.directory, x.camera.identity["epoch"]
    assert converted != candidate.producer.directory and x.camera.owned_directory
    transferred, candidate.producer = candidate.producer, None
    adopted = SimpleNamespace(producer=transferred)
    await x.camera.select(adopted)
    x.controller.stream, x.controller.pending = adopted, None
    assert x.camera.directory == converted and x.camera.transcoder is process
    assert x.camera.identity["epoch"] == epoch and create.await_count == 1
    assert not process.terminated and x.camera.ready


@pytest.mark.asyncio
async def test_app_close_discards_prepared_hevc_resources_without_deleting_stream_owned_media(output, monkeypatch):
    x = output
    process = Process()

    async def spawn(*args, **options):
        source(Path(args[-1]).parent)
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    candidate = pending_camera(x, source(x.root / "staged-native", native="hevc"))
    await x.camera.prepare(candidate)
    staged = x.camera.prepared[id(candidate)].directory
    await x.camera.close()
    assert process.terminated and not staged.exists() and not x.camera.prepared
    assert candidate.producer.directory.is_dir()


@pytest.mark.asyncio
async def test_completed_candidate_final_playlist_can_be_activated_after_engine_exit(output):
    x = output
    candidate = pending_camera(x, complete_stream(source(x.root / "already-completed")))
    candidate.process = SimpleNamespace(returncode=0)
    await x.camera.prepare(candidate)
    await x.camera.activate(candidate)
    assert x.camera.retain_completed(candidate)
    x.controller.stream, x.controller.pending = None, None
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "#EXT-X-ENDLIST" in await response.text()
    await x.camera.cancel_drain()
    candidate.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_borrowed_unready_candidate_cancels_stage_without_closing_shared_producer(output):
    x = output
    active = source(x.root / "borrowed-native", native="h264")
    x.controller.stream = active
    await x.camera.select(active)
    (active.producer.directory / "index.m3u8").unlink()
    adopted = pending_camera(x, SimpleNamespace(producer=active.producer))
    preparing = asyncio.create_task(x.camera.prepare(adopted))
    async with asyncio.timeout(1):
        while not x.camera.has_prepared(adopted) or x.camera.prepared[id(adopted)].task is None:
            await asyncio.sleep(0.01)
    candidate = x.camera.prepared[id(adopted)]
    assert candidate.borrowed
    x.controller.pending = None
    x.controller.generation += 1
    with pytest.raises(UserError, match="cancelled"):
        await asyncio.wait_for(preparing, 0.3)
    assert candidate.task.done() and candidate.task.cancelled()
    assert x.camera.owner is active and active.producer.directory.is_dir()
    assert x.camera.selected_producer is active.producer and not x.camera.prepared


@pytest.mark.asyncio
async def test_hevc_clean_converter_eof_preserves_ready_final_buffer(output, monkeypatch):
    x = output
    process = Process()
    process.returncode = 0

    async def spawn(*args, **options):
        directory = Path(args[-1]).parent
        complete_stream(source(directory))
        return process

    monkeypatch.setattr(module.asyncio, "create_subprocess_exec", AsyncMock(side_effect=spawn))
    candidate = pending_camera(x, source(x.root / "finished-native", native="hevc"))
    await x.camera.prepare(candidate)
    await x.camera.activate(candidate)
    x.controller.stream, x.controller.pending = candidate, None
    await asyncio.sleep(0.6)
    assert x.camera.ready and not x.camera.error
    async with x.client.get(x.camera.base_url + "/stream.m3u8") as response:
        assert response.status == 200 and "#EXT-X-ENDLIST" in await response.text()
