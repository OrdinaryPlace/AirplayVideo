"""Exercise the real selected-camera pipeline without accounts, LAN, or TVs.

Unlike mocked registration tests, this starts the production controller and C++
engine, fetches authenticated HLS/PNG over loopback HTTP and decodes its media.
It intentionally does not create a Home Assistant camera or call its API.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import time
from urllib.parse import urlsplit

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.browser import Browser, child_environment
from service.camera import CameraOutput
from service.controller import Controller, PairingEngine
from service.hdhomerun import Channels
from service.model import Store, default_setup


async def until(predicate, message, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(message)


async def media_command(args, payload, timeout=20):
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL, env=child_environment(),
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
        assert process.returncode == 0, "Installed media command failed"
        assert 0 < len(output) <= 2 * 1024 * 1024, "Media output exceeded its test bound"
        return output
    finally:
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def png_dimensions(data):
    assert data.startswith(b"\x89PNG\r\n\x1a\n"), "Camera must return decoded PNG bytes"
    assert data[12:16] == b"IHDR" and data[-12:] == b"\x00\x00\x00\x00IEND\xaeB`\x82", "Camera PNG is complete"
    return struct.unpack(">II", data[16:24])


async def verify_segment(data):
    # Decode actual HTTP-fetched media: metadata alone cannot establish a
    # usable picture or presentation timeline.
    result = json.loads(await media_command([
        "ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe",
        "-show_entries", "stream=codec_type,codec_name,width,height:frame=best_effort_timestamp_time,pict_type",
        "-show_frames", "-of", "json", "pipe:0",
    ], data))
    streams = result["streams"]
    assert len(streams) == 1 and streams[0]["codec_type"] == "video", "Generated camera has no invented audio track"
    assert streams[0]["codec_name"] == "h264", "HA camera requires H.264"
    assert (streams[0]["width"], streams[0]["height"]) == (1280, 720)
    frames = result["frames"]
    assert len(frames) >= 20 and frames[0]["pict_type"] == "I", "The HLS segment starts with an independently decoded keyframe"
    pts = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    assert all(a < b for a, b in zip(pts, pts[1:])) and pts[-1] - pts[0] >= 1, "Decoded frames have advancing presentation timestamps"
    images = []
    for offset in ("0", "1"):
        image = await media_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-protocol_whitelist", "file,pipe", "-i", "pipe:0", "-ss", offset,
            "-vf", "scale=320:180", "-frames:v", "1", "-an", "-c:v", "png",
            "-f", "image2pipe", "pipe:1",
        ], data)
        assert png_dimensions(image) == (320, 180)
        images.append(image)
    assert images[0] != images[1], "Decoded generated pixels advance, rather than repeating one still image"
    return len(frames)


async def fetch(session, url, expected=200):
    async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=25)) as response:
        # Do not put a bearer URL or response object in assertion output.
        assert response.status == expected, "Unexpected private camera HTTP status"
        return await response.read()


async def playlist(session, camera, owner):
    await until(lambda: camera.owner is owner and camera.ready,
                "The real engine did not produce a ready camera playlist")
    text = (await fetch(session, camera.base_url + "/stream.m3u8")).decode("utf-8")
    assert text.startswith("#EXTM3U\n") and "#EXTINF:" in text
    paths = [line for line in text.splitlines() if line and not line.startswith("#")]
    assert paths and all(path.startswith("/") and urlsplit(path).netloc == "" for path in paths)
    origin = f"http://127.0.0.1:{camera.identity['port']}"
    segment_url = origin + paths[0]
    segment = await fetch(session, segment_url)
    assert 188 <= len(segment) < 32 * 1024 * 1024, "Camera segment is bounded MPEG-TS"
    return segment_url, segment


class LiveDecoder:
    """One real player kept open while the stable camera URL changes sources."""
    def __init__(self):
        self.process = self.reader = None
        self.frames = 0
        self.last_frame_at = 0
        self.error = False

    async def start(self, url):
        self.process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
            "-protocol_whitelist", "http,tcp,crypto", "-live_start_index", "-1",
            "-analyzeduration", "1000000", "-probesize", "2000000", "-i", url,
            "-map", "0:v:0", "-an", "-vf", "scale=160:90", "-fps_mode", "passthrough",
            "-c:v", "png", "-f", "image2pipe", "pipe:1",
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=child_environment(),
        )
        self.reader = asyncio.create_task(self.consume())

    async def consume(self):
        try:
            while True:
                signature = await self.process.stdout.readexactly(8)
                assert signature == b"\x89PNG\r\n\x1a\n", "Continuous decoder returns PNG frames"
                size = 8
                while True:
                    header = await self.process.stdout.readexactly(8)
                    length = struct.unpack(">I", header[:4])[0]
                    size += length + 12
                    assert size <= 1024 * 1024, "Continuous decoder PNG is bounded"
                    await self.process.stdout.readexactly(length + 4)
                    if header[4:] == b"IEND":
                        break
                self.frames += 1
                self.last_frame_at = time.monotonic()
        except asyncio.CancelledError:
            raise
        except (asyncio.IncompleteReadError, AssertionError):
            self.error = True

    async def close(self):
        if self.process and self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            self.reader.cancel()
            await asyncio.gather(self.reader, return_exceptions=True)


async def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="airplayvideo-camera-smoke-") as temporary:
        store = Store(Path(temporary))
        settings = default_setup()
        settings["video"].update(resolution="720p", fps=30, encoder="libopenh264", bitrate_mbps=4)
        settings["home_assistant"]["enabled"] = True
        store.setup(settings)
        assert store.data["receivers"] == [], "This test must never target a TV"
        async with aiohttp.ClientSession() as session:
            pairing = PairingEngine(store.root, session)
            browser = Browser(store.root, session)
            controller = Controller(store, pairing, browser, Channels(store, session))
            camera = CameraOutput(store, controller, session, bind_address="127.0.0.1")
            controller.camera = camera
            processes, directories = [], []
            decoder = LiveDecoder()
            try:
                await pairing.start()
                controller.capabilities = await pairing.call("/capabilities")
                await camera.start()
                origin = f"http://127.0.0.1:{camera.identity['port']}"
                await fetch(session, origin + "/not-the-private-bearer/stream.m3u8", expected=404)
                await fetch(session, camera.base_url + "/stream.m3u8", expected=503)
                for index, title in enumerate(("First camera source", "Replacement camera source")):
                    await controller.play({"mode": "generated", "receivers": [], "camera": True,
                        "generated": {"title": title, "tagline": "Account-free camera test",
                                      "duration_seconds": 30, "display": "countdown", "timezone": "UTC"}})
                    owner = controller.stream
                    assert owner and owner.process and owner.config["camera_only"] is True
                    processes.append(owner.process)
                    directory = Path(owner.config["camera_directory"])
                    directories.append(directory)
                    assert controller.targets == set() and controller.phase == "playing"
                    segment_url, segment = await playlist(session, camera, owner)
                    if index == 0:
                        old_segment_url = segment_url
                        await decoder.start(camera.base_url + "/stream.m3u8")
                        decoded = await verify_segment(segment)
                        image = await fetch(session, camera.base_url + "/snapshot.png")
                        assert png_dimensions(image) == (1280, 720), "Snapshot uses the actual selected video"
                        def three_segments():
                            return sum(line.startswith("segment") for line in
                                (directory / "index.m3u8").read_text().splitlines()) >= 3
                        await until(three_segments, "First source did not produce three HLS segments")
                        await until(lambda: decoder.frames >= 30 and not decoder.error,
                                    "Continuous HTTP decoder did not decode the first source")
                        before_replacement = decoder.frames
                        switched_at = time.monotonic()
                    else:
                        assert directory != directories[0] and not directories[0].exists()
                        assert processes[0].returncode is not None, "Replaced source process is reaped"
                        await fetch(session, old_segment_url, expected=404)
                        assert segment != first_segment, "Source replacement produces different media"
                        # The first source's process is gone and its files are
                        # deleted. Its final two-second segment cannot account
                        # for frames arriving eight seconds after replacement.
                        await until(lambda: not decoder.error and decoder.frames >= before_replacement + 90
                                    and decoder.last_frame_at >= switched_at + 8,
                                    "The same HTTP decoder did not continue across selected-source replacement", timeout=25)
                        assert decoder.process.returncode is None, "Stable-URL decoder stays running across sources"
                    if index == 0:
                        first_segment = segment
                await controller.stop()
                await until(lambda: camera.owner is None, "Camera monitor did not release the stopped source")
                assert controller.stream is None and not controller.pending and not controller.targets
                assert all(process.returncode is not None for process in processes), "Stopped source processes are reaped"
                assert all(not directory.exists() for directory in directories), "Stopped sources release their media directories"
                await fetch(session, camera.base_url + "/stream.m3u8", expected=503)
                assert not browser.running, "Generated camera playback must not start Chrome"
            finally:
                await decoder.close()
                await camera.close()
                await controller.close()
                await pairing.close()
            assert camera.task.done() and camera.transcoder is None and camera.snapshot_process is None
            assert pairing.process.returncode is not None
            assert decoder.process.returncode is not None and decoder.reader.done()
            assert not list((store.root / "run").iterdir()), "No generated stream configuration or camera buffer remains"
    print(json.dumps({"fixture": "real controller and engine; generated source; no HA API, accounts, LAN, or TVs",
                      "video_codec": "h264", "audio_tracks": 0, "decoded_frames": decoded,
                      "checks": ["bearer boundary", "HLS HTTP media", "advancing decoded pixels", "PNG snapshot",
                                 "source replacement", "same HTTP decoder continues", "old generation denied", "process and buffer cleanup"]}))


if __name__ == "__main__":
    asyncio.run(asyncio.wait_for(main(), 120))
