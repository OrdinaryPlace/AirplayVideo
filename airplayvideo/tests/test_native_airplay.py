"""Saved identity conversion and native playback lifecycle, without receivers."""
import asyncio
import copy
import signal
import sys
import uuid

import pytest

pytest.importorskip("pytest_asyncio", reason="Optional native playback experiment test dependency")

from service import native_airplay
from service.native_airplay import NativeAirPlayError, NativePlayer, credentials_to_pyatv, _PyAtvBackend, _WorkerBackend


RECEIVER = {"name": "Fixture TV", "address": "192.168.250.12", "port": 7000, "device_id": "fixture-device"}
MEDIA_URL = "http://192.168.250.10:8124/session-fixture/index.m3u8"


@pytest.fixture
def saved():
    crypto = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ed25519")
    from cryptography.hazmat.primitives import serialization
    sender, receiver = crypto.Ed25519PrivateKey.generate(), crypto.Ed25519PrivateKey.generate()
    seed = sender.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
    public = sender.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    peer = receiver.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return {"schema": 1, "sender_id": str(uuid.uuid4()).upper(), "receiver_id": "fixture-receiver-identity",
            "keys": (seed + public + peer).hex(), "receiver": copy.deepcopy(RECEIVER)}


def test_mapping_preserves_identity_bytes_and_uses_seed_not_sodium_secret(saved):
    before = copy.deepcopy(saved)
    fields = [bytes.fromhex(field) for field in credentials_to_pyatv(saved).split(":")]
    raw = bytes.fromhex(saved["keys"])
    assert fields == [raw[64:], raw[:32], saved["receiver_id"].encode(), saved["sender_id"].encode()]
    assert saved == before


@pytest.mark.parametrize("change", [
    {"schema": True}, {"schema": 2}, {"keys": "00" * 96}, {"keys": "gg" * 96},
    {"keys": "00" * 64}, {"sender_id": "x" * 36}, {"receiver_id": ""},
    {"receiver_id": "x" * 64}, {"receiver_id": "bad\nidentity"}, {"receiver_id": "\ud800"},
])
def test_invalid_pairings_fail_without_exposing_material(saved, change):
    saved.update(change)
    with pytest.raises(NativeAirPlayError, match="Invalid saved AirPlay pairing") as error:
        credentials_to_pyatv(saved)
    assert saved["keys"] not in str(error.value)


class Backend:
    def __init__(self, *, error=None, wait=False, connect_wait=False):
        self.error, self.wait, self.connect_wait = error, wait, connect_wait
        self.connected = asyncio.Event()
        self.playing = asyncio.Event()
        self.cancelled = False
        self.closed = 0
        self.calls = 0

    async def connect(self, receiver, credentials):
        assert receiver == RECEIVER
        assert len(credentials.split(":")) == 4
        self.connected.set()
        if self.connect_wait:
            await asyncio.Future()

    async def play_url(self, url):
        assert url == MEDIA_URL
        self.calls += 1
        self.playing.set()
        if self.error:
            raise self.error
        if self.wait:
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    async def close(self):
        self.closed += 1


def player(saved, backend, **kwargs):
    events = []
    instance = NativePlayer(RECEIVER, saved, media_url=MEDIA_URL,
                            on_event=lambda event, data: events.append((event, data)),
                            backend_factory=lambda: backend, **kwargs)
    return instance, events


@pytest.mark.asyncio
async def test_normal_completion_closes_once_and_never_claims_picture_readiness(saved):
    backend = Backend()
    instance, events = player(saved, backend)
    await instance.play()
    await instance.close()
    assert backend.calls == backend.closed == 1
    assert sum(event == "native_closed" for event, _ in events) == 1
    assert [event for event, _ in events][:3] == ["native_connecting", "native_play_requested", "native_finished"]
    assert all("playing" not in event for event, _ in events)
    assert MEDIA_URL not in repr(events) and saved["keys"] not in repr(events)
    with pytest.raises(NativeAirPlayError, match="cannot be started again"):
        await instance.play()


@pytest.mark.asyncio
async def test_stop_cancels_only_its_playback_and_waits_for_close(saved):
    backend = Backend(wait=True)
    instance, events = player(saved, backend)
    task = asyncio.create_task(instance.play())
    await backend.playing.wait()
    await instance.close()
    assert task.done() and task.cancelled()
    assert backend.cancelled and backend.closed == 1
    assert any(event == "native_cancelled" for event, _ in events)


@pytest.mark.asyncio
async def test_caller_cancellation_closes_backend(saved):
    backend = Backend(wait=True)
    instance, _ = player(saved, backend)
    task = asyncio.create_task(instance.play())
    await backend.playing.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert backend.cancelled and backend.closed == 1


@pytest.mark.asyncio
async def test_http_500_fails_once_without_repair_or_silent_retry(saved):
    class HttpError(Exception):
        status_code = 500
    backend = Backend(error=HttpError("secret-url-and-private-response"))
    instance, events = player(saved, backend)
    with pytest.raises(NativeAirPlayError, match="HTTP 500") as error:
        await instance.play()
    assert error.value.status_code == 500
    assert backend.calls == backend.closed == 1
    assert "secret-url" not in str(error.value) + repr(events)
    assert ("native_failed", {"kind": "http", "status_code": 500}) in events


@pytest.mark.asyncio
@pytest.mark.parametrize("connect_wait", [False, True])
async def test_time_limits_cancel_pending_work_and_close(saved, connect_wait):
    backend = Backend(wait=True, connect_wait=connect_wait)
    instance, _ = player(saved, backend, connect_timeout=0.02, playback_timeout=0.04)
    with pytest.raises(NativeAirPlayError, match="timed out"):
        await instance.play()
    assert backend.closed == 1
    assert instance._operation.done()


@pytest.mark.asyncio
async def test_close_before_start_never_creates_backend(saved):
    backend = Backend()
    instance, _ = player(saved, backend)
    await instance.close()
    with pytest.raises(NativeAirPlayError):
        await instance.play()
    assert not backend.connected.is_set()


@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=fixture", "http://127.0.0.1:8124/session/file.mp4",
    "http://192.168.250.10:8124/", "http://user:secret@192.168.250.10:8124/file.mp4",
    "http://192.168.250.10/file.mp4", "http://192.168.250.10:8124/file.mp4#fragment",
])
def test_only_assigned_lan_media_urls_are_accepted(saved, url):
    with pytest.raises(NativeAirPlayError, match="assigned local media session URL"):
        NativePlayer(RECEIVER, saved, media_url=url)


def test_mismatched_target_is_rejected_before_backend(saved):
    target = dict(RECEIVER, address="192.168.250.13", device_id="different-device")
    with pytest.raises(NativeAirPlayError, match="does not match"):
        NativePlayer(target, saved, media_url=MEDIA_URL)


@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan"), True, "5"])
def test_invalid_time_bounds_are_rejected(saved, value):
    with pytest.raises(NativeAirPlayError, match="timeout"):
        NativePlayer(RECEIVER, saved, media_url=MEDIA_URL, playback_timeout=value)


@pytest.mark.asyncio
async def test_public_pyatv_adapter_uses_only_supplied_service_and_memory_settings(saved, monkeypatch):
    pyatv = pytest.importorskip("pyatv")
    from pyatv.const import Protocol
    from pyatv.protocols.airplay.utils import AirPlayFlags, parse_features
    from pyatv.settings import AirPlayVersion, MrpTunnel
    from pyatv.storage.memory_storage import MemoryStorage

    class Device:
        def __init__(self):
            self.closed = 0
            self.stream = self
            self.urls = []
        async def play_url(self, url):
            self.urls.append(url)
        def close(self):
            self.closed += 1
            return set()

    device = Device()
    async def connect(config, loop, *, storage):
        assert str(config.address) == RECEIVER["address"]
        assert len(config.services) == 1
        service = config.get_service(Protocol.AirPlay)
        assert service.port == RECEIVER["port"]
        assert service.credentials == credentials_to_pyatv(saved)
        assert AirPlayFlags.SupportsAirPlayVideoV2 in parse_features(service.properties["features"])
        assert isinstance(storage, MemoryStorage)
        settings = await storage.get_settings(config)
        assert settings.protocols.airplay.mrp_tunnel == MrpTunnel.Disable
        assert settings.protocols.raop.protocol_version == AirPlayVersion.V2
        return device
    monkeypatch.setattr(pyatv, "connect", connect)
    backend = _PyAtvBackend()
    await backend.connect(RECEIVER, credentials_to_pyatv(saved))
    await backend.play_url(MEDIA_URL)
    await backend.close()
    await backend.close()
    assert device.urls == [MEDIA_URL] and device.closed == 1


@pytest.mark.asyncio
async def test_stuck_close_is_bounded_and_reported_as_incomplete(saved):
    class StuckClose(Backend):
        async def close(self):
            self.closed += 1
            await asyncio.Future()
    instance, events = player(saved, StuckClose(), close_timeout=0.01)
    await asyncio.wait_for(instance.play(), 0.5)
    assert ("native_close_failed", {"kind": "timeout"}) in events
    assert not any(event == "native_closed" for event, _ in events)
    await asyncio.sleep(0)  # Consume the cancelled cleanup task.


def fake_worker(monkeypatch, *, response='{"event":"finished"}', ignore_close=False):
    """Exercise actual process/pipe ownership without importing or calling pyatv."""
    code = '''
import json, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print('{"event":"ready"}', flush=True)
request = json.loads(sys.stdin.readline())
if request['action'] != 'play' or len(request['credentials'].split(':')) != 4:
    raise SystemExit(20)
if request['receiver']['address'] != '192.168.250.12':
    raise SystemExit(21)
if request['media_url'] != 'http://192.168.250.10:8124/session-fixture/index.m3u8':
    raise SystemExit(22)
'''
    if response is not None:
        code += f"print({response!r}, flush=True)\n"
    code += "time.sleep(60)\n" if ignore_close else "sys.stdin.readline()\n"
    command = [sys.executable, "-c", code]
    monkeypatch.setattr(native_airplay, "_worker_command", lambda: command)
    return command


@pytest.mark.asyncio
async def test_worker_transfers_private_material_only_in_pipe_and_reaps(saved, monkeypatch):
    command = fake_worker(monkeypatch)
    events = []
    instance = NativePlayer(RECEIVER, saved, media_url=MEDIA_URL,
                            on_event=lambda event, data: events.append((event, data)), close_timeout=1)
    await asyncio.wait_for(instance.play(), 3)
    assert instance._backend.process.returncode == 0
    assert saved["keys"] not in repr(command) + repr(events)
    assert credentials_to_pyatv(saved) not in repr(command) + repr(events)
    assert events[-1] == ("native_closed", {})


@pytest.mark.asyncio
async def test_uncooperative_worker_is_killed_and_reaped(saved, monkeypatch):
    fake_worker(monkeypatch, ignore_close=True)
    instance = NativePlayer(RECEIVER, saved, media_url=MEDIA_URL, close_timeout=0.5)
    await asyncio.wait_for(instance.play(), 3)
    assert instance._backend.process.returncode == -signal.SIGKILL


@pytest.mark.asyncio
async def test_cancelled_playback_reaps_worker(saved, monkeypatch):
    fake_worker(monkeypatch, response=None, ignore_close=True)
    requested = asyncio.Event()
    instance = NativePlayer(RECEIVER, saved, media_url=MEDIA_URL, close_timeout=0.5,
                            on_event=lambda event, _: requested.set() if event == "native_play_requested" else None)
    task = asyncio.create_task(instance.play())
    await asyncio.wait_for(requested.wait(), 2)
    await asyncio.wait_for(instance.close(), 2)
    assert task.done() and task.cancelled()
    assert instance._backend.process.returncode == -signal.SIGKILL


@pytest.mark.asyncio
@pytest.mark.parametrize("response,expected", [
    ('{"event":"failed","kind":"http","status_code":500,"message":"private-upstream-details"}', "HTTP 500"),
    ("private-malformed-output", "worker stopped unexpectedly"),
])
async def test_worker_errors_are_sanitized_and_process_is_reaped(saved, monkeypatch, response, expected):
    fake_worker(monkeypatch, response=response)
    instance = NativePlayer(RECEIVER, saved, media_url=MEDIA_URL, close_timeout=1)
    with pytest.raises(NativeAirPlayError, match=expected) as error:
        await asyncio.wait_for(instance.play(), 3)
    assert "private" not in str(error.value)
    assert instance._backend.process.returncode == 0


@pytest.mark.asyncio
async def test_real_worker_can_close_without_media_or_credentials_sent():
    # Only the ready/close protocol is exercised. No play request, optional
    # dependency import, credential load, discovery, or device traffic occurs.
    backend = _WorkerBackend(connect_timeout=2, close_timeout=1)
    try:
        await asyncio.wait_for(backend.connect(RECEIVER, "never-sent"), 2)
    finally:
        await asyncio.wait_for(backend.close(), 2)
    assert backend.process.returncode == 0
