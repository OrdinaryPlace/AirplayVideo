"""Scoped stdin Stop and exact-owner numeric measurement never switch a TV."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service import supervisor_control as module
from service.model import UserError
from service.supervisor_control import MAX_REQUESTS, PREFIX, SupervisorControl
from test_frame_probe import Process, probe as frame_context, report
from test_lifecycle import A, B, setup


PRIVATE = "private-exception-url-credential-sentinel"


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


def bridge_for(context):
    output = []
    capitals = SimpleNamespace(play_current=AsyncMock(), stop_pending=AsyncMock(), state=lambda: {"status": "idle"})
    bridge = SupervisorControl(context.control, capitals, frame_probe=context.job, output=output.append)
    context.control.play, context.control.stop = AsyncMock(), AsyncMock()
    def last():
        assert output and output[-1].startswith(PREFIX) and PRIVATE not in "\n".join(output)
        return json.loads(output[-1][len(PREFIX):])
    return SimpleNamespace(bridge=bridge, capitals=capitals, output=output, last=last)


def test_exact_four_action_schemas_and_no_global_stop(frame_context):
    x = bridge_for(frame_context)
    for value in ({"action": "status"}, {"action": "capitals_play", "receivers": [A]},
                  {"action": "stop", "receiver": A}, {"action": "measure_frame_stages", "request_id": "frames"}):
        assert x.bridge.validate(value)
    for value in ({"action": "stop"}, {"action": "stop", "receiver": None},
                  {"action": "stop", "receivers": [A]}, {"action": "stop", "receiver": [A, B]},
                  {"action": "stop", "receiver": A, "all": True},
                  {"action": "measure_frame_stages", "receivers": [A]},
                  {"action": "measure_frame_stages", "seconds": 15},
                  {"action": "cancel_frame_stages"}, {"action": "ensure_sportsnet_opengl"}):
        assert not x.bridge.validate(value)


@pytest.mark.asyncio
async def test_scoped_stop_validates_known_receiver_then_stops_only_that_receiver(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    calls = []
    async def pending(receiver):
        calls.append(("pending", receiver))
    async def stop(receiver):
        calls.append(("stop", receiver))
    x.capitals.stop_pending.side_effect = pending
    c.control.stop.side_effect = stop
    await x.bridge.stop_receiver(A, "stop")
    assert calls == [("pending", A), ("stop", A)] and x.last()["result"] == "ok"
    assert "frame_probe" not in x.last()
    calls.clear()
    await x.bridge.stop_receiver("c" * 32, "unknown")
    assert calls == [] and x.last()["result"] == "rejected"
    c.control.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_returns_detached_revalidated_numeric_report_with_no_stop_play_or_profile_changes(frame_context, monkeypatch):
    c, x = frame_context, bridge_for(frame_context)
    before, generation = copy.deepcopy(c.store.data), c.control.generation
    validate = Mock(side_effect=module.validate_report)
    monkeypatch.setattr(module, "validate_report", validate)
    await x.bridge.measure("frames")
    value = x.last()
    assert value["result"] == "ok" and value["frame_probe"] == {"status": "complete", "report": report()}
    assert validate.call_args.args[0] is not c.job.report
    assert c.process.reaped and not c.job.active and not c.job.closing
    assert c.store.data == before and c.control.generation == generation and not c.control.targets
    c.control.stop.assert_not_awaited()
    c.control.play.assert_not_awaited()
    x.capitals.stop_pending.assert_not_awaited()
    c.control.browser.close.assert_not_awaited()
    c.control.browser.navigate.assert_not_awaited()
    x.bridge.dispatch({"action": "status"})
    assert "frame_probe" not in x.last()


@pytest.mark.asyncio
async def test_probe_preserves_other_tv_when_normal_idle_guard_rejects(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    active = object()
    c.control.stream, c.control.targets = active, {B}
    await x.bridge.measure("blocked")
    assert x.last()["result"] == "rejected" and x.last()["frame_probe"] == {"status": "error", "report": None}
    assert c.control.stream is active and c.control.targets == {B}
    c.spawn.assert_not_awaited()
    c.control.stop.assert_not_awaited()
    c.control.play.assert_not_awaited()
    x.capitals.stop_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_rejection_never_adopts_or_cancels_an_existing_ui_probe(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    gate = asyncio.Event()
    c.process.stdout.gate = gate
    await c.job.start()
    previous = c.job.task
    await until(lambda: c.job.process is not None)
    await x.bridge.measure("ui-owner")
    assert x.last()["result"] == "rejected" and c.job.task is previous and c.job.active
    assert not previous.cancelled() and not c.process.killed and not c.job.closing
    await c.job.cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["private", "nan", "wrong_fps", "wrong_region"])
async def test_probe_output_is_validated_again_and_never_exports_raw_report_or_errors(frame_context, mutation):
    c, x = frame_context, bridge_for(frame_context)
    value = report()
    if mutation == "private":
        value["error"] = PRIVATE
    elif mutation == "nan":
        value["capture"]["fps"] = float("nan")
    elif mutation == "wrong_fps":
        value["requested_fps"] = 60
    else:
        value["raw_picture"]["regions"][0]["name"] = PRIVATE
    c.job.read_report = AsyncMock(return_value=value)
    await x.bridge.measure("invalid-report")
    assert x.last()["result"] == "error" and x.last()["frame_probe"] == {"status": "error", "report": None}
    assert PRIVATE not in "\n".join(x.output)


@pytest.mark.asyncio
async def test_probe_admission_timeout_is_bounded_without_cancelling_foreign_work(frame_context, monkeypatch):
    c, x = frame_context, bridge_for(frame_context)
    monkeypatch.setattr(module, "FRAME_REQUEST_SECONDS", .02)
    async with c.control.lock:
        await x.bridge.measure("admission-timeout")
    assert x.last()["result"] == "error" and c.job.task is None
    c.spawn.assert_not_awaited()
    c.control.stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_probe_timeout_reaps_owned_process_and_allows_a_later_explicit_measurement(frame_context, monkeypatch):
    c, x = frame_context, bridge_for(frame_context)
    monkeypatch.setattr(module, "FRAME_REQUEST_SECONDS", .02)
    c.process.stdout.gate = asyncio.Event()
    await x.bridge.measure("timeout")
    assert x.last()["result"] == "error" and c.process.killed and c.process.reaped
    assert not c.job.active and not c.job.closing
    c.spawn.return_value = Process()
    await x.bridge.measure("retry")
    assert x.last()["result"] == "ok"
    c.control.play.assert_not_awaited()


@pytest.mark.asyncio
async def test_second_shutdown_cancellation_cannot_abandon_owned_spawn_adoption_and_reap(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    entered, release = asyncio.Event(), asyncio.Event()
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return c.process
    c.spawn.side_effect = spawn
    x.bridge.dispatch({"action": "measure_frame_stages", "request_id": "shutdown"})
    await entered.wait()
    request = next(iter(x.bridge.requests))
    closing = asyncio.create_task(x.bridge.close())
    await asyncio.sleep(0)
    request.cancel()
    await asyncio.sleep(0)
    assert not closing.done() and request in x.bridge.requests
    release.set()
    await asyncio.wait_for(closing, 2)
    assert c.process.killed and c.process.reaped and not x.bridge.requests
    assert x.last()["result"] == "cancelled" and not c.job.closing


@pytest.mark.asyncio
async def test_replaced_probe_task_report_is_never_exported_or_cancelled(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    foreign = asyncio.create_task(asyncio.Event().wait())
    start = c.job.start
    async def start_and_replace():
        await start()
        def replace(_completed):
            c.job.task, c.job.status, c.job.report = foreign, "complete", {"error": PRIVATE}
        c.job.task.add_done_callback(replace)
    c.job.start = start_and_replace
    try:
        await x.bridge.measure("replaced")
        assert x.last()["result"] == "error" and x.last()["frame_probe"]["report"] is None
        assert not foreign.cancelled() and not foreign.done()
    finally:
        foreign.cancel()
        await asyncio.gather(foreign, return_exceptions=True)


@pytest.mark.asyncio
async def test_reserved_stop_slot_and_immediate_status_work_when_four_play_requests_are_full(frame_context):
    c, x = frame_context, bridge_for(frame_context)
    release = asyncio.Event()
    async def held(_request):
        await release.wait()
    async def stopping(_receiver):
        await release.wait()
    x.capitals.play_current.side_effect = held
    x.capitals.stop_pending.side_effect = stopping
    for _ in range(MAX_REQUESTS):
        x.bridge.dispatch({"action": "capitals_play", "receivers": [A]})
    await until(lambda: x.capitals.play_current.await_count == MAX_REQUESTS)
    x.bridge.dispatch({"action": "stop", "receiver": A, "request_id": "reserved"})
    await until(lambda: bool(x.bridge.stop_requests))
    assert len(x.bridge.requests) == MAX_REQUESTS + 1
    x.bridge.dispatch({"action": "stop", "receiver": B, "request_id": "full"})
    assert x.last()["result"] == "busy"
    x.bridge.dispatch({"action": "status", "request_id": "during-stop"})
    assert x.last()["result"] == "ok"
    release.set()
    await until(lambda: not x.bridge.requests)
    c.control.stop.assert_awaited_once_with(A)
    await x.bridge.close()
