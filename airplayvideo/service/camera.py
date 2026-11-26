"""Private H.264 camera output of the currently playing source.

The listener is separate from ingress: only the Supervisor bridge is bound,
and every request requires a private, persisted bearer. Public state never
contains that bearer, source addresses, or filesystem paths.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
import hmac
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import tempfile
import time

from aiohttp import web

from .model import UserError, atomic_json, check, private_json
from .native_stream import NativeHLSStream, child_environment


_FILE = re.compile(r"(?:segment[0-9]{9}\.ts|segment-[0-9]{8}\.m4s|init\.mp4)\Z")
_SNAPSHOT_LIMIT = 16 * 1024 * 1024
_DRAIN_SECONDS = 20
_PREPARE_SECONDS = 20


@dataclass
class CameraCandidate:
    owner: object
    producer: object = None
    directory: Path | None = None
    owned: bool = False
    borrowed: bool = False
    generation: int | None = None
    transcoder: object = None
    task: object = None
    spawn: object = None
    cleanup: object = None
    ready: bool = False


async def stop_process(process):
    if process and process.returncode is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                process.kill()
            await process.wait()


class NativeCameraSession:
    """Direct YouTube preparation without an AirPlay receiver."""
    def __init__(self, root, *, config=None, on_event=None, producer=None):
        config = config or {}
        directory = Path(root) / "run"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._adopted = producer is not None
        self.producer = producer if self._adopted else NativeHLSStream(output_dir=directory,
            ffmpeg=config.get("ffmpeg", "/usr/local/bin/ffmpeg"),
            ffprobe=config.get("ffprobe", "/usr/local/bin/ffprobe"), managed_lifecycle=True, force_h264=True)
        self.max_resolution = "720p" if config.get("max_resolution") == "720p" else "1080p"
        self.closed = asyncio.Event()
        self.on_event = on_event
        self._error = ""
        self._completed = False

    @property
    def status(self):
        status = self.producer.status
        phase = "failed" if self._error or status.get("error") else "completed" if self._completed else {
            "new": "preparing", "starting": "preparing", "streaming": "playing",
            "finished": "completed", "closed": "stopped", "failed": "failed",
        }.get(status.get("state"), "preparing")
        return {"kind": "native", "phase": phase, "error": self._error or status.get("error"),
                "quality": status.get("quality"), "preparation_stage": status.get("preparation_stage"),
                "camera_only": True, "physical_playback_verified": False}

    async def prepare(self, url):
        if self._adopted:
            return
        # A camera has an H.264/1080p contract independent of a TV's HEVC support.
        await self.producer.start(url, "AppleTV5,3", max_resolution=self.max_resolution)

    async def connect(self):
        pass

    async def wait(self):
        finished = asyncio.create_task(self.producer.wait_finished())
        stopped = asyncio.create_task(self.closed.wait())
        try:
            done, _ = await asyncio.wait((finished, stopped), return_when=asyncio.FIRST_COMPLETED)
            if finished in done:
                finished.result()
                self._completed = True
                # Drain the final rolling window, but Stop must interrupt it.
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.closed.wait(), 60)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._error = "The selected camera video stopped"
        finally:
            for task in (finished, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(finished, stopped, return_exceptions=True)
            await self.close()
        return self.status

    async def close(self):
        self.closed.set()
        await self.producer.close()


class CameraOutput:
    def __init__(self, store, controller, session, *, bind_address=None):
        self.store, self.controller, self.session = store, controller, session
        supervised = bool(os.environ.get("SUPERVISOR_TOKEN")) and os.environ.get("AIRPLAYVIDEO_STANDALONE") != "1"
        self.bind_address = bind_address or ("172.30.32.1" if supervised else "127.0.0.1")
        self.runner = self.task = self.transcoder = None
        self.directory = self.owner = None
        self.selected_producer = None
        self.owned_directory = False
        self.selection_lock = asyncio.Lock()
        self.prepared = {}
        self.cleanup_tasks = set()
        self.draining = None
        self.drain_deadline = 0
        self.snapshot_lock = asyncio.Lock()
        self.snapshot_process = None
        self.snapshot = None
        self.snapshot_at = 0
        self.error = ""
        self.registration = None
        self.identity_path = store.root / "camera.json"
        self.identity = private_json(self.identity_path) if self.identity_path.exists() else {"token": secrets.token_urlsafe(32), "port": 0}
        check(isinstance(self.identity, dict), "The private camera identity needs attention")
        check(isinstance(self.identity.get("token"), str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", self.identity["token"]), "The private camera identity needs attention")
        check(type(self.identity.get("port")) is int and 0 <= self.identity["port"] <= 65535, "The private camera listener needs attention")
        for key in ("epoch", "sequence_base"):
            self.identity.setdefault(key, 0)
            check(type(self.identity[key]) is int and self.identity[key] >= 0, "The private camera sequence needs attention")
        self.last_sequence = self.identity["sequence_base"]

    def allocate(self):
        run = self.store.root / "run"
        run.mkdir(mode=0o700, exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="camera-", dir=run))

    @property
    def enabled(self):
        return self.store.data["setup"]["home_assistant"]["enabled"]

    @property
    def ready(self):
        return bool(self.enabled and self.directory and (self.directory / "index.m3u8").is_file() and not self.error)

    def state(self):
        return {"active": bool(self.owner and self.enabled), "ready": self.ready, "error": self.error,
                "draining": self.draining is not None}

    def has_prepared(self, stream):
        candidate = self.prepared.get(id(stream))
        return candidate is not None and candidate.owner is stream

    def _check_pending(self, candidate):
        check(self.enabled and getattr(self.controller, "pending", None) is candidate.owner
              and getattr(self.controller, "generation", None) == candidate.generation,
              "Home Assistant camera preparation was cancelled")

    def _candidate(self, stream):
        producer = getattr(stream, "producer", None)
        candidate = CameraCandidate(stream, producer=producer,
                                    generation=getattr(self.controller, "generation", None))
        directory = getattr(stream, "config", {}).get("camera_directory")
        if producer is not None and producer is self.selected_producer and self.directory:
            candidate.directory, candidate.transcoder = self.directory, self.transcoder
            candidate.owned, candidate.borrowed = self.owned_directory, True
        elif directory:
            candidate.directory = Path(directory)
        elif producer and producer.directory:
            if producer.plan["target"]["video_codec"] == "h264":
                candidate.directory = producer.directory
            else:
                candidate.directory, candidate.owned = self.allocate(), True
        return candidate

    @staticmethod
    def _playlist_ready(directory):
        if directory is None:
            return False
        try:
            path = directory / "index.m3u8"
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
                return False
            playlist = path.read_bytes().decode("utf-8")
            if not playlist.startswith("#EXTM3U\n") or "#EXTINF:" not in playlist:
                return False
            lines = playlist.splitlines()
            sequences = [line for line in lines if line.startswith("#EXT-X-MEDIA-SEQUENCE:")]
            if len(sequences) > 1 or sequences and not re.fullmatch(r"#EXT-X-MEDIA-SEQUENCE:[0-9]{1,19}", sequences[0]):
                return False
            if any("URI=" in line and line != '#EXT-X-MAP:URI="init.mp4"'
                   or line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:") for line in lines):
                return False
            names = [line for line in lines if line and not line.startswith("#")]
            if not names or not all(_FILE.fullmatch(name) for name in names):
                return False
            if '#EXT-X-MAP:URI="init.mp4"' in playlist:
                names.append("init.mp4")
            for name in names:
                media = directory / name
                info = media.stat()
                if media.is_symlink() or not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 32 * 1024 * 1024:
                    return False
            return True
        except (OSError, UnicodeError):
            return False

    @staticmethod
    def _conversion_arguments(producer, directory):
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-protocol_whitelist", "file,crypto,data", "-live_start_index", "-1",
                "-i", str(producer.directory / "index.m3u8"), "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale=w='min(1920,iw)':h='min(1080,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,format=yuv420p",
                "-c:v", "libopenh264", "-b:v", "6000k", "-g", "60", "-c:a", "copy",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_delete_threshold", "2",
                "-hls_flags", "delete_segments+independent_segments+temp_file",
                "-hls_segment_filename", str(directory / "segment%09d.ts"), str(directory / "index.m3u8")]

    async def _spawn_conversion(self, candidate):
        candidate.spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *self._conversion_arguments(candidate.producer, candidate.directory), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=child_environment()))
        # Adopt a process that finishes spawning after its preparation is
        # cancelled, so it cannot escape staging cleanup.
        candidate.transcoder = await asyncio.shield(candidate.spawn)
        candidate.spawn = None

    async def _stage(self, candidate):
        check(candidate.directory is not None, "The selected video has no Home Assistant camera output")
        if candidate.owned and not candidate.borrowed:
            await self._spawn_conversion(candidate)
        deadline = time.monotonic() + _PREPARE_SECONDS
        while True:
            if getattr(candidate.owner, "camera_error", None):
                raise UserError("The Home Assistant camera output stopped during preparation")
            process = getattr(candidate.owner, "process", None)
            if process and process.returncode not in (None, 0):
                raise UserError("The selected video stopped during camera preparation")
            if candidate.transcoder and candidate.transcoder.returncode not in (None, 0):
                raise UserError("The Home Assistant camera conversion stopped during preparation")
            if self._playlist_ready(candidate.directory):
                candidate.ready = True
                return
            check(time.monotonic() < deadline, "The Home Assistant camera did not produce video in time")
            await asyncio.sleep(0.05)

    async def prepare(self, stream):
        """Warm the next HLS source while the current camera keeps serving."""
        check(not self.has_prepared(stream), "Home Assistant camera preparation is already running")
        candidate = self._candidate(stream)
        self.prepared[id(stream)] = candidate
        try:
            self._check_pending(candidate)
            candidate.task = asyncio.create_task(self._stage(candidate))
            while not candidate.task.done():
                self._check_pending(candidate)
                await asyncio.wait((candidate.task,), timeout=0.05)
            self._check_pending(candidate)
            check(not candidate.task.cancelled(), "Home Assistant camera preparation was cancelled")
            candidate.task.result()
        except Exception as error:
            await self.discard_prepared(stream)
            if isinstance(error, UserError):
                raise
            raise UserError("Home Assistant camera preparation could not finish") from None
        except BaseException:
            await self.discard_prepared(stream)
            raise

    async def _release_candidate(self, candidate):
        if candidate.task and not candidate.task.done():
            candidate.task.cancel()
        if candidate.task:
            await asyncio.gather(candidate.task, return_exceptions=True)
        if candidate.borrowed:
            return
        if candidate.spawn:
            with suppress(Exception):
                candidate.transcoder = await asyncio.shield(candidate.spawn)
        await stop_process(candidate.transcoder)
        if candidate.owned and candidate.directory:
            shutil.rmtree(candidate.directory, ignore_errors=True)

    async def discard_prepared(self, stream):
        candidate = self.prepared.pop(id(stream), None)
        if candidate is None or candidate.owner is not stream:
            return
        if candidate.cleanup is None:
            candidate.cleanup = self._track_cleanup(self._release_candidate(candidate))
        await asyncio.shield(candidate.cleanup)

    def _track_cleanup(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.cleanup_tasks.add(task)
        def completed(done):
            self.cleanup_tasks.discard(done)
            # Observe failures without leaking process or filesystem details
            # through an unhandled-task warning.
            with suppress(asyncio.CancelledError, Exception):
                done.exception()
        task.add_done_callback(completed)
        return task

    async def _release_old(self, transcoder, directory, owned, snapshot, draining):
        await stop_process(snapshot)
        await stop_process(transcoder)
        if draining is not None:
            await draining.close()
        if owned and directory:
            shutil.rmtree(directory, ignore_errors=True)

    async def activate(self, stream):
        """Commit a ready candidate without suspending after the visible swap."""
        async with self.selection_lock:
            candidate = self.prepared.get(id(stream))
            check(candidate is not None and candidate.owner is stream and candidate.ready,
                  "The Home Assistant camera is not prepared")
            self._check_pending(candidate)
            check(self._playlist_ready(candidate.directory), "The Home Assistant camera is not ready")
            if candidate.borrowed:
                self.owner = stream
                del self.prepared[id(stream)]
                return
            # Persist the new timeline before changing any visible state.
            identity = dict(self.identity)
            identity["epoch"] += 1
            identity["sequence_base"] = max(self.last_sequence + 1, int(time.time() * 1_000_000))
            atomic_json(self.identity_path, identity)
            old = (self.transcoder, self.directory, self.owned_directory, self.snapshot_process, self.draining)
            self.identity = identity
            self.last_sequence = identity["sequence_base"]
            self.owner, self.directory, self.transcoder = stream, candidate.directory, candidate.transcoder
            self.selected_producer, self.owned_directory = candidate.producer, candidate.owned
            self.snapshot_process = self.snapshot = self.draining = None
            self.snapshot_at = self.drain_deadline = 0
            self.error = ""
            del self.prepared[id(stream)]
            self._track_cleanup(self._release_old(*old))

    def retain_completed(self, stream):
        """Own a completed capture's final HLS files, never its live producer."""
        if stream is not self.owner or not self.ready or self.selection_lock.locked():
            return False
        self.draining = stream
        self.drain_deadline = time.monotonic() + _DRAIN_SECONDS
        return True

    async def cancel_drain(self):
        """Global Stop discards a retained tail without waiting for its timer."""
        async with self.selection_lock:
            if self.draining is not None:
                await self._select(None)

    @property
    def base_url(self):
        return f"http://{self.bind_address}:{self.identity['port']}/{self.identity['token']}"

    async def start(self):
        app = web.Application(client_max_size=1024)
        app.router.add_get("/{token}/{name}", self.handle)
        app.router.add_get("/{token}/{generation}/{name}", self.handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        try:
            site = web.TCPSite(self.runner, self.bind_address, self.identity["port"])
            await site.start()
            self.identity["port"] = site._server.sockets[0].getsockname()[1]
            atomic_json(self.identity_path, self.identity)
            # A successful bind establishes that the previous app is gone.
            # Remove only our exact generated-directory prefix and owner.
            run = self.store.root / "run"
            if run.is_dir():
                for path in run.iterdir():
                    if path == self.directory or not re.fullmatch(r"camera-[a-z0-9_]{8}", path.name):
                        continue
                    with suppress(FileNotFoundError):
                        info = path.lstat()
                        if stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid():
                            shutil.rmtree(path)
        except BaseException:
            await self.runner.cleanup()
            raise
        self.task = asyncio.create_task(self.monitor())

    async def reset(self):
        async with self.selection_lock:
            await self._reset()

    async def _reset(self):
        draining, directory, owned_directory = self.draining, self.directory, self.owned_directory
        self.draining = None
        self.drain_deadline = 0
        # Stop serving the old generation before awaiting process cleanup.
        self.directory = self.owner = self.snapshot = None
        self.selected_producer = None
        self.owned_directory = False
        self.snapshot_at = 0
        self.error = ""
        await stop_process(self.snapshot_process)
        await stop_process(self.transcoder)
        self.transcoder = None
        if draining is not None:
            await draining.close()
        if owned_directory and directory:
            shutil.rmtree(directory, ignore_errors=True)

    async def select(self, stream):
        async with self.selection_lock:
            await self._select(stream)

    async def _select(self, stream):
        producer = getattr(stream, "producer", None)
        if producer is not None and producer is self.selected_producer and self.directory:
            # Stopping a native TV can transfer its producer to the camera.
            # Its media, timeline and any HEVC conversion keep running.
            self.owner = stream
            return
        await self._reset()
        self.owner = stream
        self.selected_producer = producer
        if stream is None:
            return
        self.identity["epoch"] += 1
        self.identity["sequence_base"] = max(self.last_sequence + 1, int(time.time() * 1_000_000))
        self.last_sequence = self.identity["sequence_base"]
        atomic_json(self.identity_path, self.identity)
        directory = getattr(stream, "config", {}).get("camera_directory")
        if directory:
            self.directory = Path(directory)
            return
        if not producer or not producer.directory:
            return
        if producer.plan["target"]["video_codec"] == "h264":
            self.directory = producer.directory
            return
        # Native 4K/HEVC remains intact for its TV; the HA camera gets H.264.
        self.directory = self.allocate()
        self.owned_directory = True
        args = self._conversion_arguments(producer, self.directory)
        self.transcoder = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, env=child_environment())

    async def monitor(self):
        registration_task = None
        attempted_at = 0
        try:
            while True:
                stream = self.controller.stream if self.enabled else None
                if self.enabled and self.owner is not None and getattr(self.controller, "pending", None) is self.owner:
                    # Controller has committed this camera but is still waiting
                    # for the old receiver's teardown before promoting media.
                    stream = self.owner
                if stream is None and self.draining is not None:
                    if self.enabled and time.monotonic() < self.drain_deadline:
                        stream = self.draining
                    else:
                        await self.cancel_drain()
                if stream is not self.owner and not self.has_prepared(stream):
                    # Registration owns a persistent HA config-flow identity.
                    # Finish an in-flight creation across source changes; all
                    # sources use the same stable authenticated camera URLs.
                    await self.select(stream)
                    attempted_at = 0
                if self.transcoder and self.transcoder.returncode not in (None, 0):
                    self.error = "The Home Assistant camera conversion stopped"
                if getattr(self.owner, "camera_error", None):
                    self.error = "The Home Assistant camera output stopped"
                if self.directory and (self.owned_directory or getattr(self.owner, "config", {}).get("camera_directory")):
                    # Fail this output independently if a faulty muxer stops rolling.
                    size = 0
                    with suppress(FileNotFoundError):
                        for path in self.directory.iterdir():
                            # The rolling muxer may delete a segment between
                            # enumeration and stat, or owner cleanup may finish.
                            with suppress(FileNotFoundError):
                                if path.is_file() and not path.is_symlink():
                                    size += path.stat().st_size
                    if size > 128 * 1024 * 1024:
                        await stop_process(self.transcoder)
                        self.error = "The Home Assistant camera reached its buffer limit"
                if registration_task and registration_task.done():
                    with suppress(Exception):
                        registration_task.result()
                    registration_task = None
                if self.ready and self.registration and not registration_task and time.monotonic() - attempted_at > 30:
                    attempted_at = time.monotonic()
                    registration_task = asyncio.create_task(self.registration.ensure(self.base_url + "/stream.m3u8", self.base_url + "/snapshot.png"))
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        finally:
            if registration_task:
                registration_task.cancel()
                await asyncio.gather(registration_task, return_exceptions=True)

    def read(self, name):
        if not self.directory:
            raise web.HTTPServiceUnavailable(text="No selected video")
        try:
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError:
            raise web.HTTPNotFound() from None
        try:
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > 32 * 1024 * 1024:
                    raise web.HTTPNotFound()
                return source.read()
        except OSError:
            raise web.HTTPNotFound() from None
        finally:
            os.close(fd)

    async def image(self):
        async with self.snapshot_lock:
            if self.snapshot and time.monotonic() - self.snapshot_at < 5:
                return self.snapshot
            owner = self.owner
            args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                    "-protocol_whitelist", "file,crypto,data", "-live_start_index", "-1",
                    "-i", str(self.directory / "index.m3u8"), "-frames:v", "1", "-an",
                    "-c:v", "png", "-f", "image2pipe", "pipe:1"]
            process = self.snapshot_process = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_environment())
            try:
                async def read_image():
                    # StreamReader.read(n) can return a partial PNG before EOF.
                    # Drain all chunks while enforcing one total byte limit.
                    chunks, size = [], 0
                    while block := await process.stdout.read(min(65536, _SNAPSHOT_LIMIT - size + 1)):
                        size += len(block)
                        if size > _SNAPSHOT_LIMIT:
                            raise web.HTTPServiceUnavailable(text="Camera image is not ready")
                        chunks.append(block)
                    return b"".join(chunks)
                data = await asyncio.wait_for(read_image(), 15)
                await asyncio.wait_for(process.wait(), 5)
                if process.returncode != 0 or not data.startswith(b"\x89PNG\r\n\x1a\n") or owner is not self.owner:
                    raise web.HTTPServiceUnavailable(text="Camera image is not ready")
                self.snapshot, self.snapshot_at = data, time.monotonic()
                return data
            except asyncio.TimeoutError:
                raise web.HTTPServiceUnavailable(text="Camera image is not ready") from None
            finally:
                await stop_process(process)
                if self.snapshot_process is process:
                    self.snapshot_process = None

    async def handle(self, request):
        token = request.match_info["token"]
        if not token.isascii() or not hmac.compare_digest(token, self.identity["token"]):
            raise web.HTTPNotFound()
        if not self.ready:
            raise web.HTTPServiceUnavailable(text="No selected video")
        name = request.match_info["name"]
        generation = self.directory.name
        if "generation" in request.match_info and request.match_info["generation"] != generation:
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        if name == "stream.m3u8" and "generation" not in request.match_info:
            data = self.read("index.m3u8")
            try:
                playlist = data.decode("utf-8")
            except UnicodeDecodeError:
                raise web.HTTPServiceUnavailable(text="Camera playlist is not ready") from None
            if len(data) > 65536 or not playlist.startswith("#EXTM3U\n"):
                raise web.HTTPServiceUnavailable(text="Camera playlist is not ready")
            prefix = f"/{self.identity['token']}/{generation}/"
            source_lines = playlist.splitlines()
            sequences = [line for line in source_lines if line.startswith("#EXT-X-MEDIA-SEQUENCE:")]
            sequence = re.fullmatch(r"#EXT-X-MEDIA-SEQUENCE:([0-9]{1,19})", sequences[0]) if len(sequences) == 1 else None
            if sequences and sequence is None:
                raise web.HTTPServiceUnavailable(text="Camera playlist is not ready")
            local_sequence = int(sequence[1]) if sequence else 0
            output_sequence = self.identity["sequence_base"] + local_sequence
            self.last_sequence = max(self.last_sequence, output_sequence)
            # HA keeps reading one stable URL across source replacements. A
            # global media sequence prevents it waiting for the old sequence;
            # discontinuities allow the new source's timestamps to start over.
            lines = ["#EXTM3U", f"#EXT-X-MEDIA-SEQUENCE:{output_sequence}",
                     f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.identity['epoch'] + bool(local_sequence)}"]
            if local_sequence == 0:
                lines.append("#EXT-X-DISCONTINUITY")
            for line in source_lines[1:]:
                if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                    continue
                if line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
                    raise web.HTTPServiceUnavailable(text="Camera playlist is not ready")
                if line == '#EXT-X-MAP:URI="init.mp4"':
                    line = line.replace('URI="init.mp4"', f'URI="{prefix}init.mp4"')
                elif "URI=" in line:
                    # Every media reference must remain on this exact origin.
                    raise web.HTTPServiceUnavailable(text="Camera playlist is not ready")
                elif line and not line.startswith("#"):
                    if not _FILE.fullmatch(line):
                        raise web.HTTPServiceUnavailable(text="Camera playlist is not ready")
                    line = prefix + line
                lines.append(line)
            return web.Response(text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl", headers=headers)
        if name == "snapshot.png" and "generation" not in request.match_info:
            return web.Response(body=await self.image(), content_type="image/png", headers=headers)
        if not _FILE.fullmatch(name) or "generation" not in request.match_info:
            raise web.HTTPNotFound()
        return web.Response(body=self.read(name), content_type="video/mp2t" if name.endswith(".ts") else "video/mp4", headers=headers)

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        for candidate in list(self.prepared.values()):
            await self.discard_prepared(candidate.owner)
        await stop_process(self.snapshot_process)
        await self.reset()
        if self.cleanup_tasks:
            await asyncio.gather(*list(self.cleanup_tasks), return_exceptions=True)
        if self.runner:
            await self.runner.cleanup()
