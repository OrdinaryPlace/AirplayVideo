"""Narrow JSON control on Supervisor-owned stdin; no listener or authentication changes."""
from __future__ import annotations
import asyncio
import contextlib
import copy
import itertools
import json
import os
import re
import stat
import sys
import time

from .model import UserError
from .controller import Stream
from .frame_probe import FrameProbe, validate_report


MAX_INPUT = 8192
MAX_REQUESTS = 4
PARTIAL_SECONDS = 2
REQUEST_SECONDS = 95
FRAME_REQUEST_SECONDS = 50
PREFIX = "AIRPLAYVIDEO_CONTROL "
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
CONNECTIONS = frozenset({"idle", "preparing", "connecting", "streaming", "playing", "stopped", "error"})
CAPITALS_STATUSES = frozenset({"idle", "checking", "started", "expired", "error"})
ENCODERS = frozenset({"libopenh264", "h264_vaapi"})
RENDER_NODE = re.compile(r"renderD[0-9]{1,6}\Z")
MAX_NUMBER = 2**63 - 1


def integer(value, minimum=0, maximum=MAX_NUMBER):
    return value if type(value) is int and minimum <= value <= maximum else None


def choice(value, allowed):
    return value if isinstance(value, str) and value in allowed else None


def pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("Duplicate key")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("Invalid JSON constant")


class SupervisorControl:
    def __init__(self, controller, capitals, *, output=None, frame_probe=None):
        self.controller, self.capitals = controller, capitals
        self.frame_probe = frame_probe
        self.output = output or (lambda line: print(line, flush=True))
        self.reader_task = self.transport = None
        self.requests = set()
        self.stop_requests = set()
        self.closing = False
        self.buffer = bytearray()
        self.stack = []
        self.quoted = self.escaped = self.draining = False
        self.partial_started = None
        self.decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=invalid_constant)

    def reset(self):
        self.buffer.clear()
        self.stack.clear()
        self.quoted = self.escaped = self.draining = False
        self.partial_started = None

    @staticmethod
    def request_id(value):
        candidate = value.get("request_id") if isinstance(value, dict) else None
        return candidate if isinstance(candidate, str) and REQUEST_ID.fullmatch(candidate) else None

    def telemetry(self, runtime, targets):
        # These are coarse encoded/delivery counters, never picture or TV fps.
        result = {"monotonic_ms": time.monotonic_ns() // 1_000_000,
                  "video": {key: None for key in (
                      "configured_encoder", "resolution", "fps", "rate_control", "bitrate_mbps",
                      "max_bitrate_mbps", "stream_encoder", "stream_fps", "sportsnet_rendering")},
                  "browser_graphics": {key: None for key in (
                      "chrome_alive", "active_profile", "launch_rendering", "gpu_flags_applied",
                      "render_node", "gpu_process_count", "gpu_render_device_open")},
                  "metrics": {"source": {}, "receivers": {}}}
        live_stream = False
        try:
            setup = self.controller.store.data.get("setup", {})
            saved = setup.get("video", {}) if isinstance(setup, dict) else {}
            browser = setup.get("browser", {}) if isinstance(setup, dict) else {}
            if isinstance(saved, dict):
                result["video"].update(
                    configured_encoder=choice(saved.get("encoder"), ENCODERS | {"auto"}),
                    resolution=choice(saved.get("resolution"), {"1080p", "720p"}),
                    fps=integer(saved.get("fps"), 30, 60) if saved.get("fps") in (30, 60) else None,
                    rate_control=choice(saved.get("rate_control"), {"auto", "vbr"}),
                    bitrate_mbps=integer(saved.get("bitrate_mbps"), 2, 20),
                    max_bitrate_mbps=integer(saved.get("max_bitrate_mbps"), 2, 40))
            if isinstance(browser, dict):
                result["video"]["sportsnet_rendering"] = choice(browser.get("sportsnet_rendering"), {"vulkan", "opengl"})
        except Exception:
            pass
        try:
            stream = self.controller.stream
            if (isinstance(stream, Stream) and stream.process is not None and
                    stream.process.returncode is None and not stream.stopping and isinstance(stream.config, dict)):
                live_stream = True
                result["video"]["stream_encoder"] = choice(stream.config.get("encoder"), ENCODERS)
                fps = stream.config.get("fps")
                result["video"]["stream_fps"] = integer(fps, 30, 60) if fps in (30, 60) else None
        except Exception:
            pass
        try:
            graphics = self.controller.browser.graphics_status()
            if isinstance(graphics, dict):
                for key in ("chrome_alive", "gpu_flags_applied", "gpu_render_device_open"):
                    value = graphics.get(key)
                    result["browser_graphics"][key] = value if type(value) is bool else None
                result["browser_graphics"]["active_profile"] = choice(graphics.get("active_profile"), {"default", "sportsnet"})
                result["browser_graphics"]["launch_rendering"] = choice(graphics.get("launch_rendering"), {"vulkan", "opengl"})
                result["browser_graphics"]["gpu_process_count"] = integer(graphics.get("gpu_process_count"), 0, 128)
                node = graphics.get("render_node")
                result["browser_graphics"]["render_node"] = node if isinstance(node, str) and RENDER_NODE.fullmatch(node) else None
        except Exception:
            pass
        if live_stream and isinstance(runtime, dict):
            metrics = runtime.get("metrics")
            source_frames = integer(metrics.get("video_frames")) if isinstance(metrics, dict) else None
            if source_frames is not None:
                result["metrics"]["source"]["video_frames"] = source_frames
            receivers = runtime.get("receivers")
            if isinstance(receivers, dict):
                for target in targets:
                    detail, projected = receivers.get(target), {}
                    if isinstance(detail, dict):
                        for key in ("video_frames", "wire_bytes", "video_age_us", "audio_age_us", "video_queue_us", "audio_queue_us"):
                            value = integer(detail.get(key), 0 if key in {"video_frames", "wire_bytes"} else -MAX_NUMBER)
                            if value is not None:
                                projected[key] = value
                    result["metrics"]["receivers"][target] = projected
        return result

    def emit(self, result, request_id=None, *, telemetry=False, frame_probe=None):
        # Build a new object from fixed keys/enums; never serialize public state,
        # launch requests, exception text, source details, labels or authentication.
        snapshot = {"result": result, "generation": 0, "targets": [], "receivers": {}, "capitals_status": "unknown"}
        if request_id is not None:
            snapshot["request_id"] = request_id
        runtime = {}
        try:
            generation = self.controller.generation
            if type(generation) is int and 0 <= generation <= 2**63 - 1:
                snapshot["generation"] = generation
            runtime = self.controller.status()
            values = runtime.get("targets", [])
            if isinstance(values, (list, tuple, set, frozenset)):
                snapshot["targets"] = sorted({value for value in itertools.islice(iter(values), 8)
                                              if isinstance(value, str) and IDENTIFIER.fullmatch(value)})
            receivers = runtime.get("receivers", {})
            if isinstance(receivers, dict):
                for target in snapshot["targets"]:
                    detail = receivers.get(target, {})
                    state = detail.get("state") if isinstance(detail, dict) else None
                    snapshot["receivers"][target] = state if isinstance(state, str) and state in CONNECTIONS else "unknown"
            status = self.capitals.state().get("status")
            if isinstance(status, str) and status in CAPITALS_STATUSES:
                snapshot["capitals_status"] = status
        except Exception:
            # A damaged runtime snapshot cannot expose an exception or raw data.
            pass
        if telemetry:
            snapshot.update(self.telemetry(runtime, snapshot["targets"]))
        if frame_probe is not None:
            snapshot["frame_probe"] = frame_probe
        with contextlib.suppress(Exception):
            self.output(PREFIX + json.dumps(snapshot, separators=(",", ":"), allow_nan=False))

    def validate(self, value):
        if not isinstance(value, dict) or value.get("action") not in {"status", "capitals_play", "stop", "measure_frame_stages"}:
            return False
        if "request_id" in value and self.request_id(value) is None:
            return False
        allowed = {"action", "request_id"}
        if value["action"] == "capitals_play":
            allowed |= {"receivers", "join"}
            targets = value.get("receivers")
            if (not isinstance(targets, list) or not 0 < len(targets) <= 8 or
                    any(not isinstance(target, str) or not IDENTIFIER.fullmatch(target) for target in targets) or
                    len(set(targets)) != len(targets) or
                    "join" in value and type(value["join"]) is not bool):
                return False
        elif value["action"] == "stop":
            allowed.add("receiver")
            receiver = value.get("receiver")
            if not isinstance(receiver, str) or not IDENTIFIER.fullmatch(receiver):
                return False
        return value.keys() <= allowed

    def dispatch(self, value):
        request_id = self.request_id(value)
        if not self.validate(value):
            probe = {"status": "error", "report": None} if isinstance(value, dict) and value.get("action") == "measure_frame_stages" else None
            self.emit("rejected", request_id, frame_probe=probe)
            return
        if value["action"] == "status":
            # Status does not wait behind public catalog fetches or play locks.
            self.emit("ok", request_id, telemetry=True)
            return
        scoped_stop_slot = (value["action"] == "stop" and len(self.requests) == MAX_REQUESTS and not self.stop_requests)
        if self.closing or len(self.requests) >= MAX_REQUESTS and not scoped_stop_slot:
            probe = {"status": "error", "report": None} if value["action"] == "measure_frame_stages" else None
            self.emit("busy", request_id, frame_probe=probe)
            return
        if value["action"] == "stop":
            task = asyncio.create_task(self.stop_receiver(value["receiver"], request_id))
            self.stop_requests.add(task)
            task.add_done_callback(self.stop_requests.discard)
        elif value["action"] == "measure_frame_stages":
            task = asyncio.create_task(self.measure(request_id))
        else:
            request = {key: value[key] for key in ("receivers", "join") if key in value}
            task = asyncio.create_task(self.play(request, request_id))
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)

    async def play(self, request, request_id):
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                await self.capitals.play_current(request)
            self.emit("ok", request_id)
        except asyncio.CancelledError:
            self.emit("cancelled", request_id)
        except UserError:
            self.emit("rejected", request_id)
        except Exception:
            self.emit("error", request_id)

    async def stop_receiver(self, receiver, request_id):
        try:
            async with asyncio.timeout(REQUEST_SECONDS):
                self.controller.store.receiver(receiver)
                await self.capitals.stop_pending(receiver)
                await self.controller.stop(receiver)
            self.emit("ok", request_id)
        except asyncio.CancelledError:
            self.emit("cancelled", request_id)
        except UserError:
            self.emit("rejected", request_id)
        except Exception:
            self.emit("error", request_id)

    @staticmethod
    async def reap_owned(task):
        if task is None:
            return
        if not task.done():
            task.cancel()
        finishing = asyncio.gather(task, return_exceptions=True)
        while not finishing.done():
            try:
                await asyncio.shield(finishing)
            except asyncio.CancelledError:
                # A second shutdown/timeout cancellation must not abandon the
                # native spawn-adopt-kill-wait cleanup owned by this exact task.
                pass

    async def measure(self, request_id):
        owned, report, result, status = None, None, "error", "error"
        try:
            async with asyncio.timeout(FRAME_REQUEST_SECONDS):
                probe = self.frame_probe
                if not isinstance(probe, FrameProbe) or probe.controller is not self.controller:
                    raise UserError("Frame measurement unavailable")
                previous = probe.task
                await probe.start()  # Existing zero-media guard; never Stop or Play here.
                candidate = probe.task  # No await before capturing exact ownership.
                if not isinstance(candidate, asyncio.Task) or candidate is previous:
                    raise UserError("Frame measurement unavailable")
                owned = candidate
                requested_fps = self.controller.store.data["setup"]["video"]["fps"]
                if type(requested_fps) is not int or requested_fps not in (30, 60):
                    raise UserError("Frame measurement unavailable")
                await asyncio.shield(owned)
                async with probe.lock:
                    if probe.task is not owned or probe.status != "complete":
                        raise UserError("Frame measurement unavailable")
                    current_fps = self.controller.store.data["setup"]["video"]["fps"]
                    if type(current_fps) is not int or current_fps != requested_fps:
                        raise UserError("Frame measurement unavailable")
                    report = validate_report(copy.deepcopy(probe.report), requested_fps)
            result, status = "ok", "complete"
        except asyncio.CancelledError:
            result, status = "cancelled", "cancelled"
        except UserError:
            result = "rejected" if owned is None else "error"
        except Exception:
            pass
        finally:
            await self.reap_owned(owned)
            self.emit(result, request_id, frame_probe={"status": status, "report": report if result == "ok" else None})

    def reject(self):
        self.emit("rejected")
        self.reset()
        self.draining = True
        self.partial_started = time.monotonic()

    def feed(self, data):
        # Objects need no newline: HA serializes app_stdin mappings as JSON.
        # Lexical framing handles split UTF-8/strings/bools and adjacent objects;
        # raw_decode then performs strict JSON/schema validation on each object.
        for byte in data:
            if self.closing:
                return
            if self.draining:
                if byte == 10:
                    self.reset()
                continue
            if not self.buffer:
                if byte in b" \t\r\n":
                    continue
                if byte != 123:
                    self.reject()
                    if byte == 10:
                        self.reset()
                    continue
                self.partial_started = time.monotonic()
            if len(self.buffer) >= MAX_INPUT:
                self.reject()
                if byte == 10:
                    self.reset()
                continue
            self.buffer.append(byte)
            if self.quoted:
                if byte < 32:
                    self.reject()
                    if byte == 10:
                        self.reset()
                elif self.escaped:
                    self.escaped = False
                elif byte == 92:
                    self.escaped = True
                elif byte == 34:
                    self.quoted = False
                continue
            if byte == 34:
                self.quoted = True
            elif byte in (123, 91):
                if len(self.stack) >= 64:
                    self.reject()
                else:
                    self.stack.append(125 if byte == 123 else 93)
            elif byte in (125, 93):
                if not self.stack or self.stack.pop() != byte:
                    self.reject()
                    continue
                if not self.stack:
                    payload = bytes(self.buffer)
                    self.reset()
                    try:
                        text = payload.decode("utf-8")
                        value, end = self.decoder.raw_decode(text)
                        if end != len(text):
                            raise ValueError("Trailing JSON")
                        self.dispatch(value)
                    except (ValueError, TypeError, RecursionError):
                        self.emit("rejected")

    async def read(self, reader):
        try:
            while not self.closing:
                try:
                    if self.buffer or self.draining:
                        remaining = max(0, PARTIAL_SECONDS - (time.monotonic() - self.partial_started))
                        data = await asyncio.wait_for(reader.read(1024), remaining)
                    else:
                        data = await reader.read(1024)
                except asyncio.TimeoutError:
                    if self.buffer:
                        self.emit("rejected")
                    self.reset()
                    continue
                if not data:
                    if self.buffer:
                        self.emit("rejected")
                    self.reset()
                    return
                self.feed(data)
        except asyncio.CancelledError:
            pass
        except Exception:
            if not self.closing:
                self.emit("error")
        finally:
            if self.transport is not None:
                self.transport.close()

    async def start(self, source=None, *, enabled=True):
        if not enabled or self.closing or self.reader_task is not None:
            return
        pipe = None
        try:
            source = source if source is not None else sys.stdin.buffer
            descriptor = source.fileno()
            mode = os.fstat(descriptor).st_mode
            if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                return
            # Own only a duplicate; shutdown must not close another owner's stdin.
            pipe = os.fdopen(os.dup(descriptor), "rb", buffering=0)
            reader = asyncio.StreamReader(limit=MAX_INPUT)
            self.transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), pipe)
            self.reader_task = asyncio.create_task(self.read(reader))
        except (AttributeError, OSError, ValueError, NotImplementedError):
            if pipe is not None:
                pipe.close()

    async def close(self):
        self.closing = True
        if self.transport is not None:
            self.transport.close()
        tasks = list(self.requests)
        if self.reader_task is not None:
            tasks.append(self.reader_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.requests.clear()
        self.stop_requests.clear()
        self.reset()
