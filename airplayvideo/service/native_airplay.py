"""Native URL playback with one explicitly selected receiver and saved pairing.

Experimental backend, not yet integrated into the product playback flow.

This module never discovers, pairs, reads credentials, or serves media. The caller
supplies its already-authorized local media URL and pairing dictionary. Importing
it does not require pyatv; that optional backend is loaded when playback begins.
Each session uses a dedicated worker because upstream public close currently
leaves some AirPlay 2 resources unaccounted for. Worker exit bounds their lifetime.
"""
from __future__ import annotations

import asyncio
import hmac
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

from .model import UserError, local_address


class NativeAirPlayError(UserError):
    """Safe failure details, with no upstream URL, credentials, or response body."""

    def __init__(self, message, *, kind="playback", status_code=None):
        super().__init__(message)
        self.kind = kind
        self.status_code = status_code


def credentials_to_pyatv(saved):
    """Convert pair_ap's seed + sender public + receiver public into HAP fields.

    The returned string is private authentication material; never log it. Identity
    strings retain their exact spelling and are encoded as UTF-8, not UUID bytes.
    """
    invalid = "Invalid saved AirPlay pairing; existing pairing was preserved"
    if not isinstance(saved, dict) or type(saved.get("schema")) is not int or saved["schema"] != 1:
        raise NativeAirPlayError(invalid, kind="credentials")
    sender, receiver, keys = (saved.get(key) for key in ("sender_id", "receiver_id", "keys"))
    if not isinstance(sender, str) or not re.fullmatch(
        r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", sender
    ):
        raise NativeAirPlayError(invalid, kind="credentials")
    if not isinstance(receiver, str) or not receiver or any(ord(c) < 32 or ord(c) == 127 for c in receiver):
        raise NativeAirPlayError(invalid, kind="credentials")
    try:
        receiver_bytes = receiver.encode("utf-8")
    except UnicodeError:
        raise NativeAirPlayError(invalid, kind="credentials") from None
    if len(receiver_bytes) >= 64 or not isinstance(keys, str) or not re.fullmatch(r"[0-9a-fA-F]{192}", keys):
        raise NativeAirPlayError(invalid, kind="credentials")
    raw = bytes.fromhex(keys)
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        raise NativeAirPlayError("Native AirPlay dependencies are unavailable", kind="dependency") from None
    public = Ed25519PrivateKey.from_private_bytes(raw[:32]).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    if not hmac.compare_digest(public, raw[32:64]):
        raise NativeAirPlayError(invalid, kind="credentials")
    return ":".join((raw[64:96].hex(), raw[:32].hex(), receiver_bytes.hex(), sender.encode("utf-8").hex()))


def _target(receiver):
    if not isinstance(receiver, dict):
        raise NativeAirPlayError("Choose a valid AirPlay receiver", kind="target")
    name, port = receiver.get("name"), receiver.get("port")
    if not isinstance(name, str) or not 0 < len(name) < 128 or any(ord(c) < 32 for c in name):
        raise NativeAirPlayError("Choose a valid AirPlay receiver", kind="target")
    if type(port) is not int or not 1 <= port <= 65535:
        raise NativeAirPlayError("Invalid AirPlay receiver port", kind="target")
    device_id = receiver.get("device_id", "")
    if not isinstance(device_id, str) or len(device_id) >= 128 or any(ord(c) < 32 or ord(c) == 127 for c in device_id):
        raise NativeAirPlayError("Invalid AirPlay receiver identity", kind="target")
    try:
        address = local_address(receiver.get("address"))
    except UserError:
        raise NativeAirPlayError("Choose a receiver on the private LAN", kind="target") from None
    return {"address": address, "name": name, "port": port, "device_id": device_id}


def _media_url(url):
    # The media origin is supplied by our session server, never a user watch URL.
    try:
        if not isinstance(url, str) or not 0 < len(url) <= 4096 or any(ord(c) <= 32 for c in url):
            raise ValueError
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.username is not None or parsed.password is not None or parsed.fragment:
            raise ValueError
        local_address(parsed.hostname)
        if parsed.port is None or not 1 <= parsed.port <= 65535 or not parsed.path.startswith("/") or parsed.path == "/":
            raise ValueError
    except (ValueError, UserError):
        raise NativeAirPlayError("Use the assigned local media session URL", kind="media") from None
    return url


def _timeout(value, label, optional=False):
    if optional and value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise NativeAirPlayError(f"Invalid native AirPlay {label} timeout", kind="configuration")
    return float(value)


class _PyAtvBackend:
    """Worker-only public API adapter; never run in the long-lived service."""

    def __init__(self):
        self.device = None

    async def connect(self, receiver, credentials):
        try:
            import pyatv
            from pyatv.conf import AppleTV, ManualService
            from pyatv.const import Protocol
            from pyatv.settings import AirPlayVersion, MrpTunnel
            from pyatv.storage.memory_storage import MemoryStorage
        except ImportError:
            raise NativeAirPlayError("Native AirPlay dependencies are unavailable", kind="dependency") from None
        config = AppleTV(ipaddress.IPv4Address(receiver["address"]), receiver["name"])
        # Only the supplied endpoint is used. V2 is explicitly selected because
        # these saved pair_ap credentials are normal HomeKit AirPlay 2 pairings.
        properties = {"features": "0x00000000,0x20000"}  # SupportsAirPlayVideoV2
        config.add_service(ManualService(
            receiver.get("device_id") or receiver["address"], Protocol.AirPlay,
            receiver["port"], properties, credentials=credentials,
        ))
        storage = MemoryStorage()
        settings = await storage.get_settings(config)
        settings.protocols.airplay.mrp_tunnel = MrpTunnel.Disable
        settings.protocols.raop.protocol_version = AirPlayVersion.V2
        self.device = await pyatv.connect(config, asyncio.get_running_loop(), storage=storage)

    async def play_url(self, url):
        await self.device.stream.play_url(url)

    async def close(self):
        device, self.device = self.device, None
        if device is not None:
            tasks = device.close()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)


def _worker_command():
    return [sys.executable, "-m", "service.native_airplay", "--worker"]


def _wire_failure(error):
    """Reduce library failures to an allowlisted, non-sensitive protocol."""
    code = getattr(error, "status_code", None)
    code = code if type(code) is int and 100 <= code <= 599 else None
    kind = error.kind if isinstance(error, NativeAirPlayError) else "http" if code else "playback"
    if isinstance(error, asyncio.TimeoutError):
        kind = "timeout"
    return {"event": "failed", "kind": kind, "status_code": code}


def _worker_error(reply):
    code = reply.get("status_code")
    code = code if type(code) is int and 100 <= code <= 599 else None
    kind = reply.get("kind")
    if kind not in {"http", "playback", "dependency", "timeout", "worker"}:
        kind = "worker"
    if kind == "dependency":
        message = "Native AirPlay dependencies are unavailable"
    elif kind == "timeout":
        message = "Native AirPlay timed out; playback was not confirmed"
    elif code:
        message = f"Native AirPlay returned HTTP {code}; playback was not confirmed"
    else:
        message = "Native AirPlay playback failed"
    return NativeAirPlayError(message, kind=kind, status_code=code)


class _WorkerBackend:
    """Private pipes carry pairing material; process arguments/status never do."""

    def __init__(self, connect_timeout=15, close_timeout=5):
        self.process = None
        self._request = None
        self._connect_timeout = connect_timeout
        self._close_budget = close_timeout * 0.8

    async def connect(self, receiver, credentials):
        self._request = {"action": "play", "receiver": receiver, "credentials": credentials,
                         "connect_timeout": self._connect_timeout}
        env = dict(os.environ)
        source_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = source_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        self.process = await asyncio.create_subprocess_exec(
            *_worker_command(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, limit=16384, start_new_session=True,
        )
        if (await self._reply()).get("event") != "ready":
            raise NativeAirPlayError("Native AirPlay worker could not start", kind="worker")

    async def _reply(self):
        try:
            raw = await self.process.stdout.readline()
            if not raw or len(raw) > 16384:
                raise ValueError
            reply = json.loads(raw)
            if not isinstance(reply, dict):
                raise ValueError
            return reply
        except (ValueError, OSError):
            raise NativeAirPlayError("Native AirPlay worker stopped unexpectedly", kind="worker") from None

    async def play_url(self, url):
        request, self._request = self._request, None
        request["media_url"] = url
        self.process.stdin.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        await self.process.stdin.drain()
        del request
        reply = await self._reply()
        if reply.get("event") != "finished":
            raise _worker_error(reply)

    async def close(self):
        self._request = None
        process = self.process
        if process is None:
            return
        if process.returncode is not None:
            if process.stdin is not None:
                process.stdin.close()
            return
        try:
            if process.stdin is not None:
                try:
                    process.stdin.write(b'{"action":"close"}\n')
                    process.stdin.close()
                except (BrokenPipeError, ConnectionResetError):
                    pass
            for fraction, action in ((0.5, None), (0.25, process.terminate), (0.25, process.kill)):
                if action is not None:
                    try:
                        action()
                    except ProcessLookupError:
                        pass
                try:
                    await asyncio.wait_for(process.wait(), self._close_budget * fraction)
                    return
                except asyncio.TimeoutError:
                    continue
            raise NativeAirPlayError("Native AirPlay worker did not exit", kind="worker")
        finally:
            # Even if our caller is cancelled during cleanup, leave no worker
            # running. Reaping a SIGKILLed local process does not depend on a TV.
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await asyncio.shield(process.wait())


def _worker_emit(value):
    sys.stdout.write(json.dumps(value, separators=(",", ":")) + "\n")
    sys.stdout.flush()


async def _worker_main():
    """One session per process. No discovery, pairing, files, or raw diagnostics."""
    reader = asyncio.StreamReader(limit=16384)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer
    )
    backend = _PyAtvBackend()
    playback = stop = None
    try:
        _worker_emit({"event": "ready"})
        raw = await reader.readline()
        if not raw:
            return
        request = json.loads(raw)
        if request.get("action") == "close":
            return
        if request.get("action") != "play":
            raise ValueError
        receiver = _target(request["receiver"])
        url = _media_url(request["media_url"])
        credentials = request["credentials"]
        if not isinstance(credentials, str) or len(credentials) > 512 or not re.fullmatch(r"[0-9a-fA-F]+(?::[0-9a-fA-F]+){3}", credentials):
            raise ValueError
        timeout = _timeout(request["connect_timeout"], "connect")
        del request, raw

        async def run():
            await asyncio.wait_for(backend.connect(receiver, credentials), timeout)
            await backend.play_url(url)

        playback = asyncio.create_task(run())
        stop = asyncio.create_task(reader.readline())
        done, _ = await asyncio.wait({playback, stop}, return_when=asyncio.FIRST_COMPLETED)
        if playback in done:
            await playback
            _worker_emit({"event": "finished"})
        # Any second input or EOF stops this one-shot worker; it cannot retarget.
    except Exception as error:
        _worker_emit(_wire_failure(error))
    finally:
        pending = []
        for task in (playback, stop):
            if task is not None:
                if not task.done():
                    task.cancel()
                pending.append(task)
        try:
            await asyncio.wait_for(backend.close(), 2)
        except (Exception, asyncio.CancelledError):
            pass
        # Retrieve completed task failures without trusting third-party tasks to
        # honor cancellation. The owning process supplies the final hard bound.
        if pending:
            done, _ = await asyncio.wait(pending, timeout=0.1)
            for task in done:
                _consume_result(task)
        transport.close()
        # asyncio.run cancels remaining feedback tasks. Process exit also closes
        # upstream event/timing sockets that public pyatv.close did not expose.


class NativePlayer:
    """One-shot native playback, closed explicitly or when play() exits.

    ``play()`` blocks until completion. ``connect_timeout`` bounds worker startup
    and pyatv's connect call separately. AirPlay URL authentication/startup is lazy
    and happens inside play_url. ``playback_timeout`` optionally bounds the entire
    connect/play trial. Close is bounded separately, including worker termination.
    Events report requests and lifecycle, never verified pictures or sound.

    ``backend_factory`` is a test seam returning an object with async connect,
    play_url, and close methods. ``on_event(event, details)`` may be sync or async;
    progress callbacks are best effort and bounded to one second.
    """

    def __init__(self, receiver, credentials, *, media_url, on_event=None,
                 connect_timeout=15, playback_timeout=None, close_timeout=5,
                 backend_factory=None):
        self.receiver = _target(receiver)
        saved_target = _target(credentials.get("receiver") if isinstance(credentials, dict) else None)
        current_id, saved_id = self.receiver.get("device_id"), saved_target.get("device_id")
        same_device = isinstance(current_id, str) and bool(current_id) and current_id == saved_id
        same_endpoint = (self.receiver["address"], self.receiver["port"]) == (saved_target["address"], saved_target["port"])
        if (current_id and saved_id and not same_device) or not (same_device or same_endpoint):
            raise NativeAirPlayError("Saved pairing does not match the selected receiver", kind="target")
        self._credentials = credentials_to_pyatv(credentials)
        self._url = _media_url(media_url)
        self._on_event = on_event
        self.connect_timeout = _timeout(connect_timeout, "connect")
        self.playback_timeout = _timeout(playback_timeout, "playback", optional=True)
        self.close_timeout = _timeout(close_timeout, "close")
        self._factory = backend_factory or (lambda: _WorkerBackend(self.connect_timeout, self.close_timeout))
        self._backend = None
        self._runner = None
        self._operation = None
        self._close_task = None
        self._close_reported = False
        self._closed = False

    async def _emit(self, event, **details):
        if self._on_event is not None:
            try:
                result = self._on_event(event, details)
                if inspect.isawaitable(result):
                    await asyncio.wait_for(result, 1)
            except Exception:
                pass  # A UI callback must not abandon playback or its cleanup.

    async def _run(self):
        self._backend = self._factory()
        await asyncio.wait_for(self._backend.connect(self.receiver, self._credentials), self.connect_timeout)
        await self._emit("native_play_requested")
        await self._backend.play_url(self._url)

    async def play(self):
        if self._closed or self._runner is not None:
            raise NativeAirPlayError("This native playback session cannot be started again", kind="state")
        self._runner = asyncio.current_task()
        try:
            await self._emit("native_connecting")
            self._operation = asyncio.create_task(self._run())
            done, _ = await asyncio.wait({self._operation}, timeout=self.playback_timeout)
            if not done:
                raise asyncio.TimeoutError
            await self._operation
            await self._emit("native_finished")
        except asyncio.CancelledError:
            await self._emit("native_cancelled")
            raise
        except Exception as error:
            if isinstance(error, NativeAirPlayError):
                safe = error
            elif isinstance(error, asyncio.TimeoutError):
                safe = NativeAirPlayError("Native AirPlay timed out; playback was not confirmed", kind="timeout")
            else:
                code = getattr(error, "status_code", None)
                code = code if type(code) is int and 100 <= code <= 599 else None
                message = f"Native AirPlay returned HTTP {code}; playback was not confirmed" if code else "Native AirPlay playback failed"
                safe = NativeAirPlayError(message, kind="http" if code else "playback", status_code=code)
            await self._emit("native_failed", kind=safe.kind, status_code=safe.status_code)
            raise safe from None
        finally:
            await self._shutdown()
            self._runner = None

    async def _cleanup(self):
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()
        if self._backend is not None:
            await self._backend.close()
        if self._operation is not None:
            await asyncio.gather(self._operation, return_exceptions=True)

    async def _shutdown(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._cleanup())
            self._credentials = ""
            self._url = ""
        done, _ = await asyncio.wait({self._close_task}, timeout=self.close_timeout)
        if not done:
            self._close_task.cancel()
            self._close_task.add_done_callback(_consume_result)
            if not self._close_reported:
                self._close_reported = True
                await self._emit("native_close_failed", kind="timeout")
        else:
            try:
                self._close_task.result()
            except (Exception, asyncio.CancelledError):
                if not self._close_reported:
                    self._close_reported = True
                    await self._emit("native_close_failed", kind="cleanup")
            else:
                if not self._close_reported:
                    self._close_reported = True
                    await self._emit("native_closed")

    async def close(self):
        """Cancel this session only and wait a bounded time for its resources."""
        runner = self._runner
        if runner is not None and runner is not asyncio.current_task() and not runner.done():
            runner.cancel()
        await self._shutdown()
        if runner is not None and runner is not asyncio.current_task():
            done, _ = await asyncio.wait({runner}, timeout=self.close_timeout)
            if done:
                _consume_result(runner)
            else:
                runner.add_done_callback(_consume_result)


def _consume_result(task):
    try:
        task.result()
    except (Exception, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This module is an internal native playback worker")
    asyncio.run(_worker_main())
