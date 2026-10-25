"""Exclusive, numeric-only sampling of an open browser with zero receivers."""
from __future__ import annotations
import asyncio
import contextlib
import copy
import json
import math
import re

from .browser import child_environment
from .model import check


TIMEOUT_SECONDS = 45
MAX_OUTPUT = 32 * 1024
SAFE_ERROR = "Frame measurement could not finish; the browser and saved recordings were preserved"
REGIONS = {"center", "left", "right"}
THRESHOLDS = {(delta, fraction) for delta in (1, 2, 4) for fraction in (0.005, 0.02)}


def keys(value, expected):
    check(isinstance(value, dict) and set(value) == set(expected), SAFE_ERROR)


def number(value, maximum, *, integer=False):
    check(type(value) is int if integer else type(value) in (int, float), SAFE_ERROR)
    try:
        valid = math.isfinite(value) and 0 <= value <= maximum
    except OverflowError:
        valid = False
    check(valid, SAFE_ERROR)
    return value


def rate(value, frames):
    number(value, 2000 / 15)
    check(abs(value - frames / 15) <= 0.001, SAFE_ERROR)


def validate_report(value, requested_fps):
    keys(value, ("version", "warmup_seconds", "duration_seconds", "requested_fps", "capture", "gate",
                 "converted", "encoded", "raw_picture", "timings_ms"))
    check(type(value["version"]) is int and value["version"] == 1, SAFE_ERROR)
    check(type(value["warmup_seconds"]) in (int, float) and value["warmup_seconds"] == 2, SAFE_ERROR)
    check(type(value["duration_seconds"]) in (int, float) and value["duration_seconds"] == 15, SAFE_ERROR)
    check(type(value["requested_fps"]) is int and value["requested_fps"] == requested_fps, SAFE_ERROR)
    capture = value["capture"]
    keys(capture, ("frames", "fps", "nonmonotonic_pts", "pts"))
    frames = number(capture["frames"], 2000, integer=True)
    rate(capture["fps"], frames)
    number(capture["nonmonotonic_pts"], frames, integer=True)
    pts = capture["pts"]
    keys(pts, ("median_ms", "p95_ms", "max_ms", "gaps_over_50ms"))
    for name in ("median_ms", "p95_ms", "max_ms"):
        number(pts[name], 60000)
    check(pts["median_ms"] <= pts["p95_ms"] <= pts["max_ms"], SAFE_ERROR)
    number(pts["gaps_over_50ms"], frames, integer=True)
    keys(value["gate"], ("accepted", "rejected"))
    for count in value["gate"].values():
        number(count, 2000, integer=True)
    for stage in ("converted", "encoded"):
        keys(value[stage], ("frames", "fps"))
        count = number(value[stage]["frames"], 2000, integer=True)
        rate(value[stage]["fps"], count)
    raw = value["raw_picture"]
    keys(raw, ("frames", "regions"))
    raw_frames = number(raw["frames"], 2000, integer=True)
    check(isinstance(raw["regions"], list) and len(raw["regions"]) == 3, SAFE_ERROR)
    names = set()
    for region in raw["regions"]:
        keys(region, ("name", "exact_updates", "exact_updates_per_second", "longest_exact_hold_ms", "thresholds"))
        name = region["name"]
        check(isinstance(name, str) and name in REGIONS and name not in names, SAFE_ERROR)
        names.add(name)
        count = number(region["exact_updates"], raw_frames, integer=True)
        rate(region["exact_updates_per_second"], count)
        number(region["longest_exact_hold_ms"], 15000)
        check(isinstance(region["thresholds"], list) and len(region["thresholds"]) == 6, SAFE_ERROR)
        seen = set()
        for threshold in region["thresholds"]:
            keys(threshold, ("luma_delta", "changed_fraction", "updates", "updates_per_second", "longest_hold_ms"))
            check(type(threshold["luma_delta"]) is int and type(threshold["changed_fraction"]) in (int, float), SAFE_ERROR)
            pair = threshold["luma_delta"], threshold["changed_fraction"]
            check(pair in THRESHOLDS and pair not in seen, SAFE_ERROR)
            seen.add(pair)
            count = number(threshold["updates"], raw_frames, integer=True)
            rate(threshold["updates_per_second"], count)
            number(threshold["longest_hold_ms"], 15000)
    keys(value["timings_ms"], ("conversion", "upload", "encode", "observation"))
    for timing in value["timings_ms"].values():
        keys(timing, ("count", "total", "mean", "p95", "max"))
        count = number(timing["count"], 2000, integer=True)
        for name in ("total", "mean", "p95", "max"):
            number(timing[name], 60000)
        check(timing["mean"] <= timing["max"] and timing["p95"] <= timing["max"], SAFE_ERROR)
        check(abs(timing["mean"] - timing["total"] / count) <= 0.001 if count else
              all(timing[name] == 0 for name in ("total", "mean", "p95", "max")), SAFE_ERROR)
    return value


class FrameProbe:
    def __init__(self, controller):
        self.controller = controller
        self.task = self.process = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.status, self.error, self.report = "idle", "", None

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def state(self):
        return {"status": "running" if self.active else self.status, "error": self.error,
                "report": copy.deepcopy(self.report)}

    async def start(self):
        control = self.controller
        async with self.lock, control.lock:
            check(not self.closing, "Frame measurement is shutting down")
            check(not self.active, "Frame measurement is already running")
            control.require_no_diagnostic()
            control.require_mode("browser")
            check(control.browser.running, "Open the browser on the video before measuring frames")
            check(not (control.stream or control.pending or control.targets or control.pending_targets or control.tearing_down),
                  "Stop playback before measuring browser frames")
            check(not control.recordings or not control.recordings.current, "Wait for recording to finish")
            display = control.browser.environment.get("DISPLAY")
            check(isinstance(display, str) and re.fullmatch(r":[0-9]{1,5}\.0", display), "The browser display is unavailable")
            config = control.source_config({"kind": "browser", "display": display})
            keys(config, ("source", "width", "height", "fps", "encoder", "bitrate", "rate_control", "max_bitrate",
                          "deinterlace", "audio", "latency_ms"))
            payload = json.dumps(config, allow_nan=False).encode()
            check(len(payload) <= 16384 and config["source"] == {"kind": "browser", "display": display}, SAFE_ERROR)
            environment = child_environment()
            for name in ("DISPLAY", "XAUTHORITY", "XDG_RUNTIME_DIR", "PULSE_SERVER", "PULSE_COOKIE", "PULSE_SINK"):
                if name in control.browser.environment:
                    environment[name] = control.browser.environment[name]
            self.status, self.error, self.report = "running", "", None
            self.task = asyncio.create_task(self.run(payload, environment, config["fps"]))
            self.task.add_done_callback(lambda _: control.notify())
            control.notify()

    async def read_report(self, payload, environment, requested_fps):
        launching = asyncio.create_task(asyncio.create_subprocess_exec(
            "airplayvideo-frame-probe", env=environment, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
        try:
            self.process = await asyncio.shield(launching)
        except asyncio.CancelledError:
            self.process = await launching
            raise
        self.process.stdin.write(payload)
        await self.process.stdin.drain()
        self.process.stdin.close()
        output = bytearray()
        while chunk := await self.process.stdout.read(4096):
            output.extend(chunk)
            check(len(output) <= MAX_OUTPUT, SAFE_ERROR)
        await self.process.wait()
        check(self.process.returncode == 0, SAFE_ERROR)
        return validate_report(json.loads(output), requested_fps)

    async def run(self, payload, environment, requested_fps):
        try:
            async with asyncio.timeout(TIMEOUT_SECONDS):
                result = await self.read_report(payload, environment, requested_fps)
            self.report, self.status, self.error = result, "complete", ""
        except asyncio.CancelledError:
            self.report, self.status, self.error = None, "cancelled", ""
            raise
        except Exception:
            self.report, self.status, self.error = None, "error", SAFE_ERROR
        finally:
            process, self.process = self.process, None
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            self.controller.notify()

    async def stop(self):
        task = self.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.report, self.status, self.error = None, "cancelled", ""
        if self.task is task:
            self.task = None
        self.controller.notify()

    async def cancel(self):
        async with self.lock:
            check(not self.closing, "Frame measurement is shutting down")
            check(self.active, "No frame measurement is running")
            await self.stop()

    async def close(self):
        self.closing = True
        async with self.lock:
            await self.stop()
