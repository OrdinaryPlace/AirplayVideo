"""Supervisor stdin accepts only bounded explicit intent and safe fresh status."""
import asyncio
import json
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from service import supervisor_control as module
from service.model import UserError
from service.supervisor_control import MAX_INPUT, MAX_REQUESTS, PREFIX, SupervisorControl


A, B = "a" * 32, "b" * 32
PRIVATE = "private-credential-sentinel"


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest_asyncio.fixture
async def pipe_control():
    reader, writer = os.pipe()
    source = os.fdopen(reader, "rb", buffering=0)
    os.set_blocking(writer, False)
    output = []
    control = SimpleNamespace(generation=7, play=AsyncMock(), stop=AsyncMock())
    runtime = {"targets": [A], "receivers": {A: {"state": "streaming", "message": PRIVATE}},
               "source": {"url": "https://private.invalid/" + PRIVATE, "label": PRIVATE}, "error": PRIVATE}
    control.status = Mock(side_effect=lambda: runtime)
    capitals = SimpleNamespace(play_current=AsyncMock(),
                               state=Mock(return_value={"status": "started", "last_watch_url": PRIVATE, "error": PRIVATE}))
    bridge = SupervisorControl(control, capitals, output=output.append)
    await bridge.start(source)
    assert bridge.reader_task is not None

    async def send(value):
        data = value if isinstance(value, bytes) else json.dumps(value).encode()
        for offset in range(0, len(data), 1024):
            portion = memoryview(data)[offset:offset + 1024]
            while portion:
                try:
                    written = os.write(writer, portion)
                    portion = portion[written:]
                except BlockingIOError:
                    await asyncio.sleep(.001)
            await asyncio.sleep(0)

    def records():
        assert all(line.startswith(PREFIX) and "\n" not in line for line in output)
        assert PRIVATE not in "\n".join(output)
        return [json.loads(line[len(PREFIX):]) for line in output]

    try:
        yield SimpleNamespace(bridge=bridge, controller=control, capitals=capitals, runtime=runtime,
                              output=output, records=records, send=send, source=source, writer=writer)
    finally:
        await bridge.close()
        source.close()
        os.close(writer)


@pytest.mark.asyncio
async def test_real_pipe_status_without_newline_or_eof_returns_only_fresh_safe_fields(pipe_control):
    x = pipe_control
    await x.send(b'{"action":"status","request_id":"first"}')
    await until(lambda: len(x.output) == 1)
    first = x.records()[0]
    telemetry = {key: first.pop(key) for key in ("monotonic_ms", "video", "browser_graphics", "metrics")}
    assert first == {"request_id": "first", "result": "ok", "generation": 7,
                     "targets": [A], "receivers": {A: "streaming"}, "capitals_status": "started"}
    assert type(telemetry["monotonic_ms"]) is int
    assert all(value is None for value in telemetry["video"].values())
    assert all(value is None for value in telemetry["browser_graphics"].values())
    assert telemetry["metrics"] == {"source": {}, "receivers": {}}
    x.controller.generation = 8
    x.runtime["targets"] = [B]
    x.runtime["receivers"] = {B: {"state": "error", "message": PRIVATE}}
    await x.send(b'{"action":"status"}{"action":"status","request_id":"adjacent"}')
    await until(lambda: len(x.output) == 3)
    assert all(value["generation"] == 8 and value["targets"] == [B] for value in x.records()[1:])
    x.capitals.play_current.assert_not_awaited()
    x.controller.play.assert_not_awaited()
    x.controller.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_split_object_with_bool_and_escaped_strings_dispatches_exact_explicit_receivers(pipe_control):
    x = pipe_control
    await x.send(b'{"action":"capitals_play","receivers":["' + A.encode() + b'"],"join":tr')
    await asyncio.sleep(.01)
    assert not x.output
    x.capitals.play_current.assert_not_awaited()
    await x.send(b'ue,"request_id":"split"}')
    await until(lambda: len(x.output) == 1)
    x.capitals.play_current.assert_awaited_once_with({"receivers": [A], "join": True})
    assert x.records()[0]["request_id"] == "split"
    await x.send(b'{"action":"sta\\u0074us","request_id":"escaped"}')
    await until(lambda: len(x.output) == 2)
    assert x.records()[1]["result"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"action": "open_browser", "url": PRIVATE}, {"action": "stop"},
    {"action": "capitals_live", "receivers": [A]}, {"action": "capitals_play"},
    {"action": "capitals_play", "receiver": A}, {"action": "capitals_play", "receivers": []},
    {"action": "capitals_play", "receivers": [A, A]},
    {"action": "capitals_play", "receivers": [A], "join": 1},
    {"action": "capitals_play", "receivers": [A], "url": PRIVATE},
    {"action": "status", "password": PRIVATE}, {"action": "status", "request_id": "bad\n" + PRIVATE},
    {"action": "status", "request_id": "x" * 65}, {"action": "status", "receivers": [A]},
])
async def test_invalid_action_schemas_are_rejected(pipe_control, payload):
    x = pipe_control
    await x.send(payload)
    await until(lambda: len(x.output) == 1)
    assert x.records()[0]["result"] == "rejected"
    x.capitals.play_current.assert_not_awaited()
    x.controller.play.assert_not_awaited()
    x.controller.stop.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [
    b'{"action":"status",}', b'{"action":"status","action":"capitals_play"}',
    b'{"action":"status","request_id":NaN}', b'{"action":"status","request_id":"\xff"}',
    b'[]\n', b'echo ' + PRIVATE.encode() + b'\n',
])
async def test_malformed_and_nonobject_input_never_logs_raw_data_or_executes(pipe_control, data, capsys):
    x = pipe_control
    await x.send(data)
    await until(lambda: len(x.output) == 1)
    assert x.records()[0]["result"] == "rejected"
    assert capsys.readouterr() == ("", "")
    x.capitals.play_current.assert_not_awaited()


@pytest.mark.asyncio
async def test_oversize_request_is_drained_once_and_next_object_is_accepted(pipe_control):
    x = pipe_control
    await x.send(b'{"action":"status","credential":"' + b'x' * (MAX_INPUT + 500) + b'"}\n')
    await x.send({"action": "status", "request_id": "after-oversize"})
    await until(lambda: len(x.output) == 2)
    assert [value["result"] for value in x.records()] == ["rejected", "ok"]
    assert x.records()[1]["request_id"] == "after-oversize"
    assert len(x.bridge.buffer) <= MAX_INPUT


@pytest.mark.asyncio
async def test_partial_input_times_out_without_thread_and_recovers(pipe_control, monkeypatch):
    monkeypatch.setattr(module, "PARTIAL_SECONDS", .03)
    x = pipe_control
    await x.send(b'{"action":')
    await until(lambda: len(x.output) == 1)
    assert x.records()[0]["result"] == "rejected" and not x.bridge.buffer
    await x.send({"action": "status"})
    await until(lambda: len(x.output) == 2)
    assert x.records()[1]["result"] == "ok"


@pytest.mark.asyncio
async def test_partial_timeout_cannot_be_extended_by_trickling_bytes(pipe_control, monkeypatch):
    monkeypatch.setattr(module, "PARTIAL_SECONDS", .04)
    x = pipe_control
    await x.send(b'{"action":')
    async with asyncio.timeout(.2):
        while not x.output:
            await x.send(b' ')
            await asyncio.sleep(.005)
    assert x.records()[0]["result"] == "rejected"


@pytest.mark.asyncio
async def test_status_remains_available_when_all_play_slots_are_busy(pipe_control):
    x = pipe_control
    release = asyncio.Event()
    async def held(_request):
        await release.wait()
    x.capitals.play_current.side_effect = held
    for index in range(MAX_REQUESTS + 1):
        await x.send({"action": "capitals_play", "receivers": [A], "request_id": str(index)})
    await until(lambda: len(x.output) == 1 and x.capitals.play_current.await_count == MAX_REQUESTS)
    assert x.records()[0]["result"] == "busy"
    await x.send({"action": "status", "request_id": "during-play"})
    await until(lambda: len(x.output) == 2)
    assert x.records()[1]["result"] == "ok" and len(x.bridge.requests) == MAX_REQUESTS
    release.set()
    await until(lambda: not x.bridge.requests)
    assert len(x.output) == MAX_REQUESTS + 2


@pytest.mark.asyncio
@pytest.mark.parametrize("exception,result", [(UserError(PRIVATE), "rejected"), (RuntimeError(PRIVATE), "error")])
async def test_launcher_errors_have_fixed_results_without_exception_strings(pipe_control, exception, result):
    x = pipe_control
    x.capitals.play_current.side_effect = exception
    await x.send({"action": "capitals_play", "receivers": [B]})
    await until(lambda: len(x.output) == 1)
    assert x.records()[0]["result"] == result


@pytest.mark.asyncio
async def test_shutdown_cancels_reader_and_play_without_closing_original_stdin(pipe_control):
    x = pipe_control
    entered, cancelled = asyncio.Event(), asyncio.Event()
    async def held(_request):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
    x.capitals.play_current.side_effect = held
    await x.send({"action": "capitals_play", "receivers": [A], "request_id": "shutdown"})
    await entered.wait()
    await x.bridge.close()
    assert cancelled.is_set() and not x.bridge.requests and x.bridge.reader_task.done()
    assert x.bridge.transport.is_closing()
    assert os.fstat(x.source.fileno())  # The bridge owned only a duplicate.
    assert x.records()[0]["result"] == "cancelled"
    await x.bridge.close()


@pytest.mark.asyncio
async def test_nonpipe_or_standalone_stdin_is_gracefully_disabled(tmp_path):
    with (tmp_path / "stdin-fixture").open("w+b") as source:
        bridge = SupervisorControl(None, None, output=Mock())
        await bridge.start(source)
        assert bridge.reader_task is None
        await bridge.close()
    reader, writer = os.pipe()
    with os.fdopen(reader, "rb", buffering=0) as source:
        bridge = SupervisorControl(None, None, output=Mock())
        await bridge.start(source, enabled=False)
        assert bridge.reader_task is None
        await bridge.close()
    os.close(writer)


@pytest.mark.asyncio
async def test_existing_socket_stdin_uses_same_async_framing_without_new_listener():
    receiving, sending = socket.socketpair()
    source = receiving.makefile("rb", buffering=0)
    output = []
    bridge = SupervisorControl(SimpleNamespace(generation=1, status=lambda: {"targets": [], "receivers": {}}),
                               SimpleNamespace(state=lambda: {"status": "idle"}), output=output.append)
    try:
        await bridge.start(source)
        assert bridge.reader_task is not None
        sending.sendall(b'{"action":"status","request_id":"socket"}')
        await until(lambda: len(output) == 1)
        assert json.loads(output[0][len(PREFIX):])["request_id"] == "socket"
        await bridge.close()
        assert bridge.reader_task.done() and os.fstat(source.fileno())
    finally:
        await bridge.close()
        source.close()
        receiving.close()
        sending.close()
