"""Explicit playback lifecycle: one source, independent receiver sessions."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import signal
import uuid
import aiohttp
from .browser import child_environment
from .model import UserError, check, atomic_json, browser_url, youtube_url, identifier, validate_generated


class PairingEngine:
    def __init__(self, root, session):
        self.root, self.session = Path(root), session
        self.process = None
        self.port = None

    async def start(self):
        self.process = await asyncio.create_subprocess_exec("airplayvideo-engine", "--data", str(self.root / "receivers"), env=child_environment(), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            event = json.loads(await asyncio.wait_for(self.process.stdout.readline(), 10))
            check(event.get("event") == "engine_ready", "The AirPlay engine could not start")
            port = event["details"]["port"]
            check(type(port) is int and 0 < port <= 65535, "The AirPlay engine returned an invalid endpoint")
            self.port = port
        except (asyncio.TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise UserError("The AirPlay engine did not become ready") from exc
        for _ in range(100):
            check(self.process.returncode is None, "The AirPlay engine could not start")
            try:
                async with self.session.get(f"http://127.0.0.1:{self.port}/health", timeout=aiohttp.ClientTimeout(total=1)) as response:
                    if response.status == 200:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass
            await asyncio.sleep(0.1)
        raise UserError("The AirPlay engine did not become ready")

    async def call(self, path, body=None):
        check(self.port is not None, "The AirPlay engine is not ready")
        try:
            async with self.session.post(f"http://127.0.0.1:{self.port}" + path, json=body or {}, headers={"X-AirplayVideo": "1"}, timeout=aiohttp.ClientTimeout(total=35)) as response:
                result = await response.json()
                if response.status != 200:
                    raise UserError(result.get("error", "AirPlay operation failed") if isinstance(result, dict) else "AirPlay operation failed")
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise UserError("The AirPlay engine did not respond") from exc

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 15)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()
        self.port = None


class Stream:
    def __init__(self, root, config, environment, callback):
        self.root, self.config, self.environment, self.callback = Path(root), config, environment, callback
        self.process = None
        self.reader = None
        self.ready = asyncio.get_running_loop().create_future()
        self.stopping = False
        self.completed = False
        self.write_lock = asyncio.Lock()
        self.close_lock = asyncio.Lock()

    async def start(self):
        check(not self.stopping, "Playback was cancelled")
        run = self.root / "run"
        run.mkdir(exist_ok=True, mode=0o700)
        path = run / (uuid.uuid4().hex + ".json")
        atomic_json(path, self.config)
        self.process = await asyncio.create_subprocess_exec("airplayvideo-engine", "--stream", str(path), "--data", str(self.root / "receivers"), env=self.environment, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.reader = asyncio.create_task(self.read_events())
        try:
            if self.stopping:
                await self.close()
                raise UserError("Playback was cancelled")
            await asyncio.wait_for(asyncio.shield(self.ready), 25)
            check(not self.stopping, "Playback was cancelled")
        except asyncio.TimeoutError as exc:
            raise UserError("The source did not produce video and audio in time") from exc
        finally:
            # Run configurations are temporary and never contain pairing keys.
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def read_events(self):
        try:
            async for line in self.process.stdout:
                try:
                    event = json.loads(line)
                    name, fields = event["event"], event["details"]
                    if name == "source_finished":
                        self.completed = True
                    if name == "media_ready" and not self.ready.done():
                        self.ready.set_result(fields)
                    if name in {"source_error", "fatal"} and not self.ready.done():
                        self.ready.set_exception(UserError(fields.get("message", "Media source failed")))
                    await self.callback(self, name, fields)
                except (ValueError, KeyError, TypeError):
                    continue
        finally:
            code = await self.process.wait()
            if not self.ready.done():
                self.ready.set_exception(UserError("Playback was cancelled" if self.stopping else "The media process exited during startup"))
            await self.callback(self, "process_exit", {"code": code, "requested": self.stopping, "completed": self.completed})

    async def command(self, command):
        check(self.process and self.process.returncode is None and not self.stopping, "Playback is no longer running")
        async with self.write_lock:
            try:
                self.process.stdin.write((json.dumps(command) + "\n").encode())
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise UserError("The playback connection closed") from exc

    async def close(self):
        async with self.close_lock:
            self.stopping = True
            if self.process and self.process.returncode is None:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), 20)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if self.reader and self.reader is not asyncio.current_task():
                await self.reader
            if self.ready.done() and not self.ready.cancelled():
                self.ready.exception()  # Observe a cancelled startup's exception.


class Controller:
    def __init__(self, store, pairing, browser, channels):
        self.store, self.pairing, self.browser, self.channels = store, pairing, browser, channels
        self.stream = None
        self.pending = None
        self.generation = 0
        self.lock = asyncio.Lock()
        self.targets = set()
        self.receivers = {}
        self.source = None
        self.phase = "idle"
        self.error = ""
        self.metrics = {}
        self.changed = lambda: None
        self.capabilities = {"encoders": ["libopenh264"]}
        self.cleanup_tasks = set()
        self.recordings = None
        self.diagnostics = None

    def require_no_diagnostic(self):
        check(not self.diagnostics or not self.diagnostics.active, "Wait for the sync measurement to finish or cancel it")

    def status(self):
        return {"phase": self.phase, "source": self.source, "targets": sorted(self.targets), "receivers": self.receivers, "error": self.error, "metrics": self.metrics, "browser_open": self.browser.running}

    def notify(self):
        self.changed()

    def require_mode(self, mode):
        setup = self.store.data["setup"]
        check(setup["complete"], "Finish Setup first")
        check(setup["modes"].get(mode), "Enable this mode in Setup first")

    def source_config(self, source):
        settings = self.store.data["setup"]
        video = settings["video"]
        encoder = video["encoder"]
        if encoder == "auto":
            encoder = "h264_vaapi" if "h264_vaapi" in self.capabilities["encoders"] else "libopenh264"
        check(encoder in self.capabilities["encoders"], "The configured encoder is unavailable; review Setup")
        width, height = (1920, 1080) if video["resolution"] == "1080p" else (1280, 720)
        return {"source": source, "width": width, "height": height, "fps": video["fps"], "encoder": encoder, "bitrate": video["bitrate_mbps"] * 1_000_000, "deinterlace": video["deinterlace"], "audio": source["kind"] != "generated" and settings["audio"]["enabled"], "latency_ms": settings["audio"]["latency_ms"]}

    async def event(self, stream, event, fields):
        if self.recordings:
            await self.recordings.event(stream, event, fields)
        if stream is not self.stream and stream is not self.pending:
            return
        receiver_id = fields.get("id")
        if receiver_id in self.targets:
            if event in {"connecting", "streaming", "stopped", "receiver_error"}:
                self.receivers[receiver_id] = {"state": "error" if event == "receiver_error" else event, **{k: v for k, v in fields.items() if k != "id"}}
            if event == "streaming":
                self.phase = "playing"
        if stream is self.stream:
            if event == "media":
                self.metrics = fields
            if event in {"source_error", "fatal"}:
                self.error = fields.get("message", "The source stopped")
                self.phase = "error"
            if event == "process_exit" and not fields["requested"]:
                complete = fields.get("completed") and fields.get("code") == 0
                self.error = "" if complete else self.error or "Playback ended; press Play to reconnect"
                self.phase = "idle" if complete else "error"
                self.targets.clear()
                self.receivers.clear()
                self.source = None
                self.stream = None
            if event == "receiver_error" and self.targets and all(self.receivers.get(r, {}).get("state") == "error" for r in self.targets):
                task = asyncio.create_task(self.stop_failed(stream))
                self.cleanup_tasks.add(task)
                task.add_done_callback(self.cleanup_tasks.discard)
        self.notify()

    async def stop_failed(self, stream):
        if self.stream is stream:
            self.error = "All receiver connections ended; press Play to reconnect"
            await stream.close()
            if self.stream is stream:
                self.stream = None
                self.targets.clear()
                self.phase = "error"
                self.notify()

    def requested_source(self, request):
        mode = request.get("mode", "browser")
        self.require_mode(mode)
        if mode == "generated":
            settings = validate_generated(request.get("generated", {}), self.store.data["setup"]["generated"])
            # Every Play starts a fresh duration, including identical automation messages.
            return {"kind": mode, "key": uuid.uuid4().hex, "label": settings["title"]}, {"kind": mode, "generated": settings}
        if mode == "hdhomerun":
            channel = self.channels.get(request.get("channel"))
            return {"kind": "hdhomerun", "key": channel["id"], "label": channel["label"]}, {"kind": "hdhomerun", "url": channel["_url"]}
        kind = request.get("browser_source", "page")
        if kind == "youtube":
            url, label = youtube_url(request.get("url")), "YouTube video"
        elif kind == "watch_later":
            url, label = "https://www.youtube.com/playlist?list=WL", "Watch Later"
        elif kind == "page":
            page = self.store.page(request.get("page"))
            url, label = page["url"], page["name"]
        elif kind == "url":
            url, label = browser_url(request.get("url")), "Web page"
        else:
            raise UserError("Choose a browser source")
        return {"kind": "browser", "key": "browser", "label": label, "url": url, "page_id": request.get("page"), "browser_source": kind}, {"kind": "browser", "pulse": "airplayvideo.monitor"}

    async def open_browser(self, request):
        async with self.lock:
            self.require_no_diagnostic()
            self.require_mode("browser")
            request = {**request, "mode": "browser"}
            source, _ = self.requested_source(request)
            await self.browser.navigate(source["url"], self.store.data["setup"], source["browser_source"] == "youtube", source["browser_source"] == "watch_later")
            if self.source and self.source["kind"] == "browser":
                self.source = source
            self.notify()

    async def play(self, request, add=False):
        async with self.lock:
            self.require_no_diagnostic()
            check(not self.recordings or not self.recordings.current or not self.recordings.owned,
                  "Wait for the diagnostic recording to finish")
            wanted = request.get("receivers", [])
            check(isinstance(wanted, list) and 0 < len(wanted) <= 8, "Choose one or more TVs")
            target = {self.store.receiver(r)["id"] for r in wanted}
            if add:
                target |= self.targets
            source, input_config = self.requested_source(request)
            self.generation += 1
            generation = self.generation
            self.error = ""
            same_source = self.stream and self.source and (self.source["kind"], self.source["key"]) == (source["kind"], source["key"])
            pending = None
            try:
                if same_source:
                    for receiver in self.targets - target:
                        await self.stream.command({"action": "remove", "id": receiver})
                        self.receivers.pop(receiver, None)
                    self.targets &= target
                if source["kind"] == "browser":
                    if not self.browser.running or not self.source or source.get("url") != self.source.get("url"):
                        await self.browser.navigate(source["url"], self.store.data["setup"], source["browser_source"] == "youtube", source["browser_source"] == "watch_later")
                    else:
                        await self.browser.start(self.store.data["setup"])
                    input_config["display"] = self.browser.environment["DISPLAY"]
                check(generation == self.generation, "Playback was cancelled")
                if not same_source:
                    self.phase = "preparing"
                    self.notify()
                    environment = dict(self.browser.environment) if source["kind"] == "browser" else child_environment()
                    pending = Stream(self.store.root, self.source_config(input_config), environment, self.event)
                    self.pending = pending
                    await pending.start()
                    check(generation == self.generation and self.pending is pending, "Playback was cancelled")
                    if self.stream:
                        await self.stream.close()
                    check(generation == self.generation, "Playback was cancelled")
                    self.stream, self.pending = pending, None
                    self.targets.clear()
                    self.receivers.clear()
                    self.metrics.clear()
                self.source = source
                self.phase = "connecting"
                for receiver_id in target:
                    receiver = self.store.receiver(receiver_id)
                    if receiver_id not in self.targets or self.receivers.get(receiver_id, {}).get("state") == "error":
                        self.targets.add(receiver_id)
                        self.receivers[receiver_id] = {"state": "connecting"}
                        await self.stream.command({"action": "add", "id": receiver_id, "slot": receiver["slot"]})
                self.notify()
            except BaseException:
                if pending and pending is not self.stream:
                    await pending.close()
                    if self.pending is pending:
                        self.pending = None
                if generation == self.generation:
                    self.phase = "playing" if self.stream and self.targets else "idle"
                self.notify()
                raise

    async def stop(self, receiver_id=None):
        # Deliberately does not acquire the start lock: Stop cancels decoder warmup.
        if self.diagnostics and self.diagnostics.active:
            if not receiver_id or receiver_id in self.diagnostics.targets:
                await self.diagnostics.close()
        if receiver_id:
            self.store.receiver(receiver_id)
            if receiver_id not in self.targets:
                return
        self.generation += 1
        pending, self.pending = self.pending, None
        if pending:
            await pending.close()
        if receiver_id:
            self.store.receiver(receiver_id)
            if receiver_id in self.targets and len(self.targets) > 1 and self.stream:
                await self.stream.command({"action": "remove", "id": receiver_id})
                self.targets.discard(receiver_id)
                self.receivers.pop(receiver_id, None)
                self.notify()
                return
        active, self.stream = self.stream, None
        self.targets.clear()
        if active:
            await active.close()
        self.receivers.clear()
        self.source = None
        self.phase = "idle"
        self.metrics.clear()
        self.notify()

    async def close_browser(self):
        if self.source and self.source["kind"] == "browser":
            await self.stop()
        await self.browser.close()
        self.notify()

    async def close(self):
        await self.stop()
        await self.browser.close()
        if self.cleanup_tasks:
            await asyncio.gather(*self.cleanup_tasks, return_exceptions=True)
