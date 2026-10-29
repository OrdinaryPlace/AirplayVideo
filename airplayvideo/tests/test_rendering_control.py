"""Read-only renderer/settings/counter projection never exposes raw app state."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service import supervisor_control as module
from service.controller import Stream
from service.model import default_setup
from service.supervisor_control import PREFIX, SupervisorControl


A, B, C = (letter * 32 for letter in "abc")
PRIVATE = "private-url-label-credential-sentinel"
GRAPHICS_KEYS = {"chrome_alive", "active_profile", "launch_rendering", "gpu_flags_applied",
                 "render_node", "gpu_process_count", "gpu_render_device_open"}
VIDEO_KEYS = {"configured_encoder", "resolution", "fps", "rate_control", "bitrate_mbps",
              "max_bitrate_mbps", "stream_encoder", "stream_fps", "sportsnet_rendering"}
BASE_KEYS = {"result", "generation", "targets", "receivers", "capitals_status"}


@pytest.fixture
def context():
    setup = default_setup()
    setup["browser"].update(sportsnet_rendering="opengl", http_proxy="http://" + PRIVATE + ":8888")
    setup["video"].update(encoder="auto", fps=30, bitrate_mbps=16, max_bitrate_mbps=30, rate_control="vbr")
    stream = Stream.__new__(Stream)
    stream.process, stream.stopping = SimpleNamespace(returncode=None), False
    stream.config = {"encoder": "h264_vaapi", "fps": 30, "source": {"url": PRIVATE}}
    graphics = {"chrome_alive": True, "active_profile": "sportsnet", "launch_rendering": "opengl",
                "gpu_flags_applied": True, "render_node": "renderD128", "gpu_process_count": 1,
                "gpu_render_device_open": True, "argv": [PRIVATE], "error": PRIVATE}
    runtime = {"targets": [A, B], "receivers": {
        A: {"state": "streaming", "video_frames": 100, "wire_bytes": 10000, "video_age_us": -200,
            "audio_age_us": 100, "video_queue_us": 300, "audio_queue_us": 400, "message": PRIVATE},
        B: {"state": "streaming", "video_frames": 90, "wire_bytes": 9000},
        C: {"state": "streaming", "video_frames": 1000, "wire_bytes": 50000}},
        "metrics": {"video_frames": 120, "url": PRIVATE, "native": {"decoder": PRIVATE}},
        "source": {"url": PRIVATE, "label": PRIVATE}, "error": PRIVATE}
    browser = SimpleNamespace(graphics_status=Mock(side_effect=lambda: graphics))
    control = SimpleNamespace(generation=3, store=SimpleNamespace(data={"setup": setup}), stream=stream,
                              browser=browser, status=Mock(side_effect=lambda: runtime),
                              play=AsyncMock(), stop=AsyncMock(), pending=None)
    capitals = SimpleNamespace(play_current=AsyncMock(), state=lambda: {"status": "started", "error": PRIVATE})
    output = []
    bridge = SupervisorControl(control, capitals, output=output.append)

    def status(request_id=None):
        bridge.dispatch({"action": "status", **({"request_id": request_id} if request_id else {})})
        assert output[-1].startswith(PREFIX) and "\n" not in output[-1] and PRIVATE not in output[-1]
        return json.loads(output[-1][len(PREFIX):])

    return SimpleNamespace(bridge=bridge, control=control, capitals=capitals, runtime=runtime,
                           setup=setup, stream=stream, browser=browser, graphics=graphics,
                           output=output, status=status)


def test_status_projects_only_typed_settings_actual_live_stream_and_browser_graphics(context):
    x = context
    value = x.status("readback")
    assert set(value) == BASE_KEYS | {"request_id", "monotonic_ms", "video", "browser_graphics", "metrics"}
    assert type(value["monotonic_ms"]) is int and value["monotonic_ms"] >= 0
    assert value["video"] == {"configured_encoder": "auto", "resolution": "1080p", "fps": 30,
                              "rate_control": "vbr", "bitrate_mbps": 16, "max_bitrate_mbps": 30,
                              "stream_encoder": "h264_vaapi", "stream_fps": 30, "sportsnet_rendering": "opengl"}
    assert value["browser_graphics"] == {key: x.graphics[key] for key in GRAPHICS_KEYS}
    assert value["metrics"] == {"source": {"video_frames": 120}, "receivers": {
        A: {"video_frames": 100, "wire_bytes": 10000, "video_age_us": -200, "audio_age_us": 100,
            "video_queue_us": 300, "audio_queue_us": 400}, B: {"video_frames": 90, "wire_bytes": 9000}}}
    x.control.play.assert_not_awaited()
    x.control.stop.assert_not_awaited()
    x.capitals.play_current.assert_not_awaited()


def test_status_timestamp_and_counter_snapshots_are_fresh_without_calculating_fps(context, monkeypatch):
    x = context
    samples = iter([1_000_000_000, 6_000_000_000])
    monkeypatch.setattr(module.time, "monotonic_ns", lambda: next(samples))
    first = x.status()
    x.runtime["metrics"]["video_frames"] = 270
    x.runtime["receivers"][A].update(video_frames=250, wire_bytes=20000)
    second = x.status()
    assert (first["monotonic_ms"], second["monotonic_ms"]) == (1000, 6000)
    assert first["generation"] == second["generation"] == 3
    assert first["targets"] == second["targets"] == [A, B]
    assert first["metrics"]["source"]["video_frames"] == 120
    assert second["metrics"]["source"]["video_frames"] == 270
    assert second["metrics"]["receivers"][A]["video_frames"] == 250
    assert "fps" not in second["metrics"]["source"]
    assert all("fps" not in detail for detail in second["metrics"]["receivers"].values())


@pytest.mark.parametrize("state", ["missing", "native", "pending_only", "exited", "stopping"])
def test_dead_missing_native_or_pending_stream_cannot_publish_stale_live_config_or_counters(context, state):
    x = context
    if state == "missing":
        x.control.stream = None
    elif state == "native":
        x.control.stream = SimpleNamespace(config=x.stream.config, process=x.stream.process, stopping=False)
    elif state == "pending_only":
        x.control.pending, x.control.stream = x.stream, None
    elif state == "exited":
        x.stream.process.returncode = 1
    else:
        x.stream.stopping = True
    value = x.status()
    assert value["video"]["stream_encoder"] is None and value["video"]["stream_fps"] is None
    assert value["video"]["configured_encoder"] == "auto"  # Saved preference stays separate.
    assert value["metrics"] == {"source": {}, "receivers": {}}


@pytest.mark.parametrize("invalid", [True, False, -1, 2**63, 1.5, float("nan"), float("inf"), PRIVATE, {}, []])
def test_counter_projection_rejects_booleans_nonintegers_nonfinite_and_out_of_bounds(context, invalid):
    x = context
    x.runtime["metrics"]["video_frames"] = invalid
    x.runtime["receivers"][A].update(video_frames=invalid, wire_bytes=invalid)
    value = x.status()
    assert value["metrics"]["source"] == {}
    assert "video_frames" not in value["metrics"]["receivers"][A]
    assert "wire_bytes" not in value["metrics"]["receivers"][A]


@pytest.mark.parametrize("invalid", [True, 2**63, -(2**63), .5, float("nan"), PRIVATE])
def test_age_and_queue_projection_is_integer_only_and_bounded(context, invalid):
    x = context
    keys = ("video_age_us", "audio_age_us", "video_queue_us", "audio_queue_us")
    x.runtime["receivers"][A].update({key: invalid for key in keys})
    assert not set(keys) & x.status()["metrics"]["receivers"][A].keys()


def test_settings_and_graphics_projection_never_adopts_raw_values_or_truthiness(context):
    x = context
    x.setup["video"].update(encoder=PRIVATE, resolution=PRIVATE, fps=30.0, rate_control=PRIVATE,
                            bitrate_mbps=True, max_bitrate_mbps=float("inf"))
    x.setup["browser"]["sportsnet_rendering"] = PRIVATE
    x.stream.config.update(encoder=PRIVATE, fps=True)
    x.graphics.update(chrome_alive=1, active_profile=PRIVATE, launch_rendering=PRIVATE,
                      gpu_flags_applied="true", render_node="/dev/dri/renderD128", gpu_process_count=129,
                      gpu_render_device_open=PRIVATE)
    value = x.status()
    assert set(value["video"]) == VIDEO_KEYS and all(value is None for value in value["video"].values())
    assert set(value["browser_graphics"]) == GRAPHICS_KEYS
    assert all(value is None for value in value["browser_graphics"].values())


@pytest.mark.parametrize("failure", ["absent", "raises", "not_mapping"])
def test_missing_or_failed_graphics_readback_is_unknown_while_base_status_remains_usable(context, failure):
    x = context
    if failure == "absent":
        del x.browser.graphics_status
    elif failure == "raises":
        x.browser.graphics_status = Mock(side_effect=RuntimeError(PRIVATE))
    else:
        x.browser.graphics_status = Mock(return_value=PRIVATE)
    value = x.status()
    assert value["result"] == "ok" and value["generation"] == 3
    assert value["metrics"]["source"]["video_frames"] == 120
    assert all(value is None for value in value["browser_graphics"].values())


def test_non_target_receiver_metrics_and_invalid_ids_never_escape(context):
    x = context
    x.runtime["targets"] = [A, "https://" + PRIVATE]
    value = x.status()
    assert value["targets"] == [A]
    assert set(value["metrics"]["receivers"]) == {A}
    assert B not in value["metrics"]["receivers"] and C not in value["metrics"]["receivers"]


@pytest.mark.asyncio
async def test_play_and_rejected_acknowledgements_keep_prior_schema_without_telemetry(context):
    x = context
    await x.bridge.play({"receivers": [A]}, "play")
    played = json.loads(x.output[-1][len(PREFIX):])
    assert set(played) == BASE_KEYS | {"request_id"}
    x.bridge.dispatch({"action": "ensure_sportsnet_opengl", "expected_rendering": "vulkan"})
    rejected = json.loads(x.output[-1][len(PREFIX):])
    assert set(rejected) == BASE_KEYS and rejected["result"] == "rejected"
    x.capitals.play_current.assert_awaited_once_with({"receivers": [A]})
