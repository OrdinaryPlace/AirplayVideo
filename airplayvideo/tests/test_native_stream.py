import asyncio
from fractions import Fraction
import json
import os
from pathlib import Path
import sys
import time

import pytest

from service.native_plan import plan_media
from service.native_stream import (NativeHLSStream, StreamError, choose_encoder, hardware_arguments,
                                   hls_arguments, parse_capabilities, playlist_segments, child_environment)
from service.native_stream import validate_source_durations, playlist_durations


def plan(*, four_k=False, convert_audio=False, convert_video=False):
    video = {"format_id": "v", "vcodec": "vp9" if four_k or convert_video else "avc1.64002a", "acodec": "none",
             "width": 3840 if four_k else 1920, "height": 2160 if four_k else 1080, "fps": 60, "dynamic_range": "SDR"}
    audio = {"format_id": "a", "vcodec": "none", "acodec": "opus" if convert_audio else "mp4a.40.2",
             "asr": 44100, "audio_channels": 2, "abr": 128}
    return plan_media({"formats": [video, audio]}, "AppleTV6,2")


def session(tmp_path, **kwargs):
    return NativeHLSStream(output_dir=tmp_path, ffmpeg=sys.executable, ffprobe=sys.executable, **kwargs)


def write_playlist(directory, names=("segment-00000000.m4s",), *, end=False):
    directory.mkdir(exist_ok=True)
    (directory / "init.mp4").write_bytes(b"init")
    for name in names:
        if "/" not in name:
            (directory / name).write_bytes(b"segment")
    (directory / "index.m3u8").write_text('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init.mp4"\n'
                                         + "".join("#EXTINF:4.0,\n" + name + "\n" for name in names)
                                         + ("#EXT-X-ENDLIST\n" if end else ""))


def test_capability_inventory_parsing_uses_actual_ffmpeg_columns():
    assert parse_capabilities(" V....D libopenh264 description\n V....D hevc_vaapi description\n A..... aac text", "encoders") == {"libopenh264", "hevc_vaapi", "aac"}
    assert parse_capabilities(" VFS..D vp9 text\n A....D opus text", "decoders") == {"vp9", "opus"}
    assert parse_capabilities("Input:\n  https\n  tls\nOutput:\n  file\n", "protocols") == {"https", "tls", "file"}
    assert parse_capabilities(" E hls name\n E mov,mp4 name", "muxers") == {"hls", "mov", "mp4"}
    assert parse_capabilities(" ..C color |->V\n ... hwupload V->V", "filters") == {"color", "hwupload"}
    assert parse_capabilities(" V....D libdav1d text", "decoders") == {"av1", "libdav1d"}


def test_child_environment_does_not_inherit_service_credentials_or_arbitrary_import_paths(monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "private")
    monkeypatch.setenv("MQTT_PASSWORD", "private")
    monkeypatch.setenv("PYTHONPATH", "/private/import/path")
    environment = child_environment()
    assert "SUPERVISOR_TOKEN" not in environment and "MQTT_PASSWORD" not in environment
    assert environment["PYTHONPATH"].endswith("/airplayvideo")
    assert "private" not in json.dumps(environment)


def test_copy_tracks_need_no_encoder_even_when_none_are_installed():
    assert choose_encoder(plan(), set()) is None
    args = hls_arguments("ffmpeg", "v.source", "a.source", Path("out"), plan(), remote=False)
    assert args[args.index("-c:v") + 1] == args[args.index("-c:a") + 1] == "copy"
    assert "-r" not in args and "-s" not in args and "-t" not in args
    assert "-shortest" in args
    assert args.count("-readrate") == 2
    assert args[args.index("-hls_segment_type") + 1] == "fmp4"
    assert args[args.index("-hls_list_size") + 1] == "12"
    assert "delete_segments+independent_segments+temp_file" in args


def test_audio_conversion_does_not_force_video_conversion():
    args = hls_arguments("ffmpeg", "v", "a", Path("out"), plan(convert_audio=True), remote=False)
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"


def test_four_k_sixty_is_preserved_and_software_gpl_requires_explicit_opt_in():
    p = plan(four_k=True)
    with pytest.raises(StreamError, match="opt-in"):
        choose_encoder(p, {"libx265"}, requested="libx265")
    assert choose_encoder(p, {"libx265"}, requested="libx265", allow_gpl=True) == "libx265"
    args = hls_arguments("ffmpeg", "v", "a", Path("out"), p, encoder="libx265", remote=False)
    assert "-r" not in args and "-s" not in args
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[args.index("-tag:v") + 1] == "hvc1"
    assert "keyint=240" in args[args.index("-x265-params") + 1]


def test_production_choices_do_not_infer_available_hevc_encoder():
    with pytest.raises(StreamError, match="unavailable"):
        choose_encoder(plan(four_k=True), {"libopenh264", "libx265"})
    with pytest.raises(StreamError, match="render device"):
        choose_encoder(plan(four_k=True), {"hevc_vaapi"})
    assert choose_encoder(plan(convert_video=True), {"libopenh264"}) == "libopenh264"
    assert choose_encoder(plan(four_k=True), {"hevc_vaapi"}, vaapi_device="/dev/dri/renderD128") == "hevc_vaapi"
    with pytest.raises(StreamError, match="required target"):
        choose_encoder(plan(four_k=True), {"libopenh264"}, requested="libopenh264")


@pytest.mark.parametrize("device", ["/dev/video0", "/dev/dri/renderD128:secret", "http://secret", None])
def test_vaapi_device_is_an_explicit_render_node(device):
    with pytest.raises(StreamError):
        hardware_arguments("hevc_vaapi", device)


def test_remote_arguments_reject_non_youtube_media_hosts():
    with pytest.raises(ValueError):
        hls_arguments("ffmpeg", "https://private.invalid/secret", "https://private.invalid/audio", Path("out"), plan())


def test_playlist_accepts_only_finalized_generated_files(tmp_path):
    assert playlist_segments(tmp_path) == []
    write_playlist(tmp_path)
    assert playlist_segments(tmp_path) == ["segment-00000000.m4s"]
    (tmp_path / "segment-00000000.m4s").unlink()
    assert playlist_segments(tmp_path) == []


@pytest.mark.parametrize("name", ["../secret.mp4", "https://private/secret", "segment-00000000.m4s.tmp", "other.mp4"])
def test_playlist_never_allows_paths_urls_or_temporary_files(tmp_path, name):
    write_playlist(tmp_path, [name])
    with pytest.raises(StreamError, match="unexpected"):
        playlist_segments(tmp_path)


def test_playlist_does_not_follow_symlinked_segments(tmp_path):
    write_playlist(tmp_path)
    part = tmp_path / "segment-00000000.m4s"
    part.unlink()
    part.symlink_to(tmp_path / "init.mp4")
    assert playlist_segments(tmp_path) == []


def test_source_duration_checks_allow_extractor_rounding_but_reject_mismatched_tracks():
    video = {"streams": [{"codec_type": "video", "duration": "20.016667"}]}
    audio = {"streams": [{"codec_type": "audio", "duration": "20.02"}]}
    validate_source_durations(20, video, audio)
    audio["streams"][0]["duration"] = "90"
    with pytest.raises(StreamError, match="cover"):
        validate_source_durations(20, video, audio)
    audio["streams"][0]["duration"] = "20.5"
    with pytest.raises(StreamError, match="do not agree"):
        validate_source_durations(20, video, audio)


def test_playlist_sequence_and_durations_are_accounted_exactly(tmp_path):
    write_playlist(tmp_path, ["segment-00000000.m4s", "segment-00000001.m4s"])
    assert playlist_durations(tmp_path) == [(0, Fraction(4)), (1, Fraction(4))]
    text = (tmp_path / "index.m3u8").read_text().replace("#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-MEDIA-SEQUENCE:5")
    (tmp_path / "index.m3u8").write_text(text)
    with pytest.raises(StreamError, match="sequence"):
        playlist_durations(tmp_path)


@pytest.mark.asyncio
async def test_process_timeout_kills_owned_process_and_never_exposes_raw_logs(tmp_path):
    stream = session(tmp_path)
    with pytest.raises(StreamError) as error:
        await stream._command([sys.executable, "-c", "import sys,time;sys.stderr.write('secret-url');time.sleep(30)"], timeout=0.05)
    assert "secret" not in str(error.value)
    assert not stream._jobs
    await stream.close()
    assert stream.closed.is_set() and not stream.status["ready"]


@pytest.mark.asyncio
async def test_command_drains_every_chunk_through_eof(tmp_path):
    stream = session(tmp_path)
    data = await stream._command([sys.executable, "-c",
                                  "import sys,time;sys.stdout.write('a'*100000);sys.stdout.flush();time.sleep(.03);sys.stdout.write('b'*100000)"], timeout=3)
    assert data == b"a" * 100000 + b"b" * 100000
    await stream.close()


@pytest.mark.asyncio
async def test_command_cleans_descendant_even_after_leader_exits(tmp_path):
    stream = session(tmp_path)
    pidfile = tmp_path / "child.pid"
    source = ("import subprocess,sys,pathlib; "
              "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],"
              "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              "pathlib.Path(sys.argv[1]).write_text(str(p.pid))")
    await stream._command([sys.executable, "-c", source, str(pidfile)], timeout=5)
    child_pid = int(pidfile.read_text())
    # On Linux the killed orphan can briefly remain a zombie until init reaps
    # it; it must no longer be an executing process in either case.
    try:
        os.kill(child_pid, 0)
    except ProcessLookupError:
        pass
    else:
        import subprocess
        state = subprocess.run(["ps", "-o", "stat=", "-p", str(child_pid)], capture_output=True, text=True).stdout.strip()
        assert not state or state.startswith("Z")
    assert not stream._owned_groups
    await stream.close()


@pytest.mark.asyncio
async def test_cancelled_spawn_adopts_and_terminates_late_child(tmp_path, monkeypatch):
    stream = session(tmp_path)
    original = asyncio.create_subprocess_exec
    spawned, release = asyncio.Event(), asyncio.Event()
    children = []

    async def delayed(*args, **kwargs):
        child = await original(*args, **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed)
    task = asyncio.create_task(stream._command([sys.executable, "-c", "import time;time.sleep(30)"], timeout=30))
    await spawned.wait()
    task.cancel()
    close = asyncio.create_task(stream.close())
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await close
    assert all(child.returncode is not None for child in children)
    assert not stream._spawning


@pytest.mark.asyncio
async def test_encoder_verification_actually_attempts_requested_hardware_geometry(tmp_path, monkeypatch):
    stream = session(tmp_path, vaapi_device="/dev/dri/renderD128")
    stream.plan = plan(four_k=True)
    seen = []

    async def command(args, **kwargs):
        seen.append(args)
        assert kwargs["timeout"] == 20
        raise StreamError("safe failure")

    monkeypatch.setattr(stream, "_command", command)
    with pytest.raises(StreamError, match="could not encode"):
        await stream._verify_encoder({"encoders": {"hevc_vaapi"}, "decoders": {"vp9"}, "filters": {"color"}})
    args, = seen
    assert "color=c=black:s=3840x2160:r=60/1" in args
    assert "vaapi=airplay:/dev/dri/renderD128" in args
    assert args[args.index("-profile:v") + 1] == "main"
    await stream.close()


@pytest.mark.asyncio
async def test_missing_audio_encoder_fails_explicitly(tmp_path):
    stream = session(tmp_path)
    stream.plan = plan(convert_audio=True)
    with pytest.raises(StreamError, match="AAC"):
        await stream._verify_encoder({"encoders": set(), "decoders": {"opus"}, "filters": set()})
    await stream.close()


@pytest.mark.asyncio
async def test_start_cancellation_cleans_processes_and_preserves_existing_recordings(tmp_path, monkeypatch):
    existing = tmp_path / "recording.mp4"
    existing.write_bytes(b"original")
    stream = session(tmp_path)
    began = asyncio.Event()

    async def start(*args):
        began.set()
        await stream._command([sys.executable, "-c", "import time;time.sleep(30)"], timeout=30)

    monkeypatch.setattr(stream, "_start", start)
    task = asyncio.create_task(stream.start("https://youtu.be/aqz-KE-bpKQ", "AppleTV6,2"))
    await began.wait()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stream.closed.is_set() and not stream._jobs
    assert existing.read_bytes() == b"original"
    await stream.close()


@pytest.mark.asyncio
async def test_monitor_publishes_ready_only_after_segment_verification_and_detects_bad_exit(tmp_path, monkeypatch):
    stream = session(tmp_path)
    stream.directory = tmp_path / "native-stream-owned"
    write_playlist(stream.directory)
    stream._started = time.monotonic()
    stream.plan = plan()
    verified = asyncio.Event()

    async def verify(name):
        assert name == "segment-00000000.m4s"
        await verified.wait()

    monkeypatch.setattr(stream, "_verify_first_segment", verify)
    stream.process = await stream._spawn([sys.executable, "-c", "import time;time.sleep(30)"])
    stream._monitor_task = asyncio.create_task(stream._monitor())
    await asyncio.sleep(0.05)
    assert not stream._ready.is_set()
    verified.set()
    await asyncio.wait_for(stream._ready.wait(), 2)
    assert stream.status["ready"]
    stream.process.kill()
    await stream.process.wait()
    await asyncio.wait_for(stream.closed.wait(), 3)
    assert stream.status["state"] == "failed" and not stream.directory.exists()


@pytest.mark.asyncio
async def test_disk_limit_stops_stream_and_removes_only_its_generated_directory(tmp_path):
    stream = session(tmp_path, disk_limit=16 * 1024 * 1024)
    stream.directory = tmp_path / "native-stream-owned"
    stream.directory.mkdir()
    with (stream.directory / "segment-00000000.m4s.tmp").open("wb") as output:
        output.truncate(17 * 1024 * 1024)
    stream._started = time.monotonic()
    stream.process = await stream._spawn([sys.executable, "-c", "import time;time.sleep(30)"])
    stream._monitor_task = asyncio.create_task(stream._monitor())
    await asyncio.wait_for(stream.closed.wait(), 3)
    assert stream.process.returncode is not None
    assert stream.status["error"] == "The native HLS session reached its disk limit"
    assert not stream.directory.exists()


@pytest.mark.asyncio
async def test_clean_early_exit_with_endlist_cannot_claim_full_video_completion(tmp_path, monkeypatch):
    stream = session(tmp_path)
    stream.directory = tmp_path / "native-stream-owned"
    write_playlist(stream.directory, end=True)
    stream.expected_duration = 20
    stream._started = time.monotonic()
    stream.process = await stream._spawn([sys.executable, "-c", "pass"])
    await stream.process.wait()

    async def verify(name):
        pass

    monkeypatch.setattr(stream, "_verify_first_segment", verify)
    stream._monitor_task = asyncio.create_task(stream._monitor())
    await asyncio.wait_for(stream.closed.wait(), 3)
    assert stream.status["prepared_duration_seconds"] == 4
    assert stream.status["error"] == "Native HLS output did not cover the complete resolved video duration"
    assert not stream.status["ready"]


@pytest.mark.asyncio
async def test_finished_session_retains_full_final_drain_near_total_deadline(tmp_path, monkeypatch):
    stream = session(tmp_path, startup_timeout=10, session_timeout=150)
    stream.directory = tmp_path / "native-stream-owned"
    write_playlist(stream.directory, end=True)
    stream.expected_duration = 4
    stream._started = time.monotonic() - 29
    stream.process = await stream._spawn([sys.executable, "-c", "pass"])
    await stream.process.wait()

    async def verify(name):
        pass

    monkeypatch.setattr(stream, "_verify_first_segment", verify)
    stream._monitor_task = asyncio.create_task(stream._monitor())
    await asyncio.wait_for(stream._finished.wait(), 2)
    # Cross the generation deadline with the producer finished. Its reserved
    # final drain remains available; cleanup does not run at this boundary.
    stream._started -= 3
    await asyncio.sleep(0.6)
    assert stream.status["state"] == "finished" and stream.directory.exists()
    assert not stream.closed.is_set()
    await stream.close()


@pytest.mark.parametrize("kwargs", [{"disk_limit": 1}, {"startup_timeout": 999}, {"session_timeout": 99999}])
def test_session_bounds_are_required(tmp_path, kwargs):
    with pytest.raises(StreamError):
        session(tmp_path, **kwargs)
