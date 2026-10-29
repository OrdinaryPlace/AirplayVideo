"""Narrow JSON control on Supervisor-owned stdin; no listener or authentication changes."""
from __future__ import annotations
import asyncio
import contextlib
import itertools
import json
import os
import re
import stat
import sys
import time

from .model import UserError


MAX_INPUT = 8192
MAX_REQUESTS = 4
PARTIAL_SECONDS = 2
REQUEST_SECONDS = 95
PREFIX = "AIRPLAYVIDEO_CONTROL "
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")
REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
CONNECTIONS = frozenset({"idle", "preparing", "connecting", "streaming", "playing", "stopped", "error"})
CAPITALS_STATUSES = frozenset({"idle", "checking", "started", "expired", "error"})


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
    def __init__(self, controller, capitals, *, output=None):
        self.controller, self.capitals = controller, capitals
        self.output = output or (lambda line: print(line, flush=True))
        self.reader_task = self.transport = None
        self.requests = set()
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

    def emit(self, result, request_id=None):
        # Build a new object from fixed keys/enums; never serialize public state,
        # launch requests, exception text, source details, labels or authentication.
        snapshot = {"result": result, "generation": 0, "targets": [], "receivers": {}, "capitals_status": "unknown"}
        if request_id is not None:
            snapshot["request_id"] = request_id
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
        with contextlib.suppress(Exception):
            self.output(PREFIX + json.dumps(snapshot, separators=(",", ":"), allow_nan=False))

    def validate(self, value):
        if not isinstance(value, dict) or value.get("action") not in {"status", "capitals_play"}:
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
        return value.keys() <= allowed

    def dispatch(self, value):
        request_id = self.request_id(value)
        if not self.validate(value):
            self.emit("rejected", request_id)
            return
        if value["action"] == "status":
            # Status does not wait behind public catalog fetches or play locks.
            self.emit("ok", request_id)
            return
        if self.closing or len(self.requests) >= MAX_REQUESTS:
            self.emit("busy", request_id)
            return
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
        self.reset()
