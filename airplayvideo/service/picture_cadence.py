"""Bounded local recording analysis; only fixed numeric aggregates leave the job."""
from __future__ import annotations
import asyncio
import contextlib
import json
import math
import os
import stat

from .browser import child_environment
from .model import UserError, atomic_json, check, identifier, private_json


MAX_BYTES = 128 * 1024 * 1024
MAX_OUTPUT = 16 * 1024
TIMEOUT_SECONDS = 45
SAFE_ERROR = "Picture analysis could not finish; the recording was preserved"


def number(value, maximum, *, integer=False):
    check(type(value) is int if integer else type(value) in (int, float), SAFE_ERROR)
    check(math.isfinite(value) and 0 <= value <= maximum, SAFE_ERROR)
    return value


def validate_result(value):
    """Do not propagate helper fields, text, paths or arbitrary JSON to the UI."""
    check(isinstance(value, dict) and set(value) == {
        "version", "video_frames", "duration_seconds", "encoded_fps", "pts", "regions"}, SAFE_ERROR)
    check(type(value["version"]) is int and value["version"] == 1, SAFE_ERROR)
    frames = number(value["video_frames"], 2000, integer=True)
    duration = number(value["duration_seconds"], 31)
    check(frames >= 2 and duration > 0, SAFE_ERROR)
    rate = number(value["encoded_fps"], 240)
    check(abs(rate - (frames - 1) / duration) <= 0.02, SAFE_ERROR)
    pts = value["pts"]
    check(isinstance(pts, dict) and set(pts) == {"median_ms", "p95_ms", "max_ms", "gaps_over_50ms"}, SAFE_ERROR)
    for key in ("median_ms", "p95_ms", "max_ms"):
        number(pts[key], 31000)
    check(pts["median_ms"] <= pts["p95_ms"] <= pts["max_ms"], SAFE_ERROR)
    number(pts["gaps_over_50ms"], frames - 1, integer=True)
    regions = value["regions"]
    check(isinstance(regions, list) and len(regions) == 3, SAFE_ERROR)
    names = set()
    expected = {(delta, fraction) for delta in (1, 2, 4) for fraction in (0.005, 0.02)}
    for region in regions:
        check(isinstance(region, dict) and set(region) == {"name", "thresholds"}, SAFE_ERROR)
        check(region["name"] in {"center", "left", "right"} and region["name"] not in names, SAFE_ERROR)
        names.add(region["name"])
        check(isinstance(region["thresholds"], list) and len(region["thresholds"]) == 6, SAFE_ERROR)
        seen = set()
        for row in region["thresholds"]:
            check(isinstance(row, dict) and set(row) == {"luma_delta", "changed_fraction", "updates", "updates_per_second", "longest_hold_ms"}, SAFE_ERROR)
            check(type(row["luma_delta"]) is int and type(row["changed_fraction"]) in (int, float), SAFE_ERROR)
            key = (row["luma_delta"], row["changed_fraction"])
            check(key in expected and key not in seen, SAFE_ERROR)
            seen.add(key)
            updates = number(row["updates"], frames - 1, integer=True)
            observed = number(row["updates_per_second"], 240)
            check(abs(observed - updates / duration) <= 0.02, SAFE_ERROR)
            number(row["longest_hold_ms"], duration * 1000 + 0.02)
    return value


def identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


class PictureAnalysis:
    def __init__(self, recordings):
        self.recordings, self.controller = recordings, recordings.controller
        self.task = self.process = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.id, self.status, self.error = None, "idle", ""

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def state(self):
        return {"id": self.id, "status": "running" if self.active else self.status, "error": self.error}

    def capture(self, capture_id):
        capture_id = identifier(capture_id)
        check(not self.recordings.current or self.recordings.current["id"] != capture_id,
              "Wait for recording to finish")
        path = self.recordings.file(capture_id, "mkv")
        report_path = self.recordings.file(capture_id, "json")
        report = private_json(report_path)
        check(isinstance(report, dict) and report.get("id") == capture_id and report.get("status") == "complete",
              "Choose a completed recording for picture analysis")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            check(stat.S_ISREG(info.st_mode) and 0 < info.st_size <= MAX_BYTES,
                  "Choose a completed recording within the analysis size limit")
            return path, report_path, identity(info)
        finally:
            os.close(fd)

    async def start(self, capture_id):
        async with self.lock, self.controller.lock:
            check(not self.closing, "Picture analysis is shutting down")
            check(not self.active, "Picture analysis is already running")
            control = self.controller
            control.require_no_diagnostic()
            check(not (control.stream or control.pending or control.targets or control.pending_targets or control.tearing_down),
                  "Stop playback before analyzing a recording")
            check(not self.recordings.current, "Wait for recording to finish")
            try:
                path, report_path, original = self.capture(capture_id)
            except (OSError, ValueError):
                raise UserError("Choose a completed local recording for picture analysis") from None
            self.id, self.status, self.error = capture_id, "running", ""
            self.task = asyncio.create_task(self.run(path, report_path, original))
            self.task.add_done_callback(lambda _: control.notify())
            control.notify()

    async def read_result(self, fd):
        launching = asyncio.create_task(asyncio.create_subprocess_exec(
            "airplayvideo-analyze-picture", f"/proc/self/fd/{fd}", pass_fds=(fd,),
            env=child_environment(), stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
        try:
            self.process = await asyncio.shield(launching)
        except asyncio.CancelledError:
            # Adopt a process created at the cancellation boundary before reaping it.
            self.process = await launching
            raise
        output = bytearray()
        while chunk := await self.process.stdout.read(4096):
            output.extend(chunk)
            check(len(output) <= MAX_OUTPUT, SAFE_ERROR)
        await self.process.wait()
        check(self.process.returncode == 0, SAFE_ERROR)
        return validate_result(json.loads(output))

    async def run(self, path, report_path, original):
        fd = None
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            check(identity(os.fstat(fd)) == original, SAFE_ERROR)
            async with asyncio.timeout(TIMEOUT_SECONDS):
                result = await self.read_result(fd)
            check(identity(path.lstat()) == original and not path.is_symlink(), SAFE_ERROR)
            report = private_json(report_path)
            check(report.get("id") == self.id and report.get("status") == "complete", SAFE_ERROR)
            report["picture_cadence"] = result
            atomic_json(report_path, report)
            self.status, self.error = "complete", ""
        except asyncio.CancelledError:
            self.status, self.error = "cancelled", ""
            raise
        except Exception:
            self.status, self.error = "error", SAFE_ERROR
        finally:
            process, self.process = self.process, None
            if process is not None and process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            if fd is not None:
                os.close(fd)
            self.controller.notify()

    async def stop(self):
        task = self.task
        if task and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.status, self.error = "cancelled", ""
            self.controller.notify()
        if self.task is task:
            self.task = None

    async def cancel(self, capture_id):
        async with self.lock:
            check(not self.closing, "Picture analysis is shutting down")
            check(self.active and identifier(capture_id) == self.id, "That sample is not being analyzed")
            await self.stop()

    async def close(self):
        self.closing = True
        async with self.lock:
            await self.stop()
