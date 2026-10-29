"""Verify production lifecycle and Docker --init stdin without network or TVs."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from service.main import Application
from service.model import Store, default_setup
from service.supervisor_control import PREFIX


async def main():
    with tempfile.TemporaryDirectory(prefix="airplayvideo-stdin-smoke-") as directory:
        store = Store(Path(directory))
        settings = default_setup()
        settings["modes"]["hdhomerun"] = False
        settings["home_assistant"]["enabled"] = False
        store.setup(settings)
        received, snapshots = asyncio.Event(), []
        async with aiohttp.ClientSession() as session:
            application = Application(store, session)

            def observe(line):
                assert line.startswith(PREFIX)
                value = json.loads(line[len(PREFIX):])
                snapshots.append(value)
                received.set()

            application.supervisor_control.output = observe
            try:
                await application.start()
                await asyncio.wait_for(received.wait(), 20)
                assert len(snapshots) == 1, snapshots
                snapshot = snapshots[0]
                base = {"request_id": "container-smoke", "result": "ok", "generation": 0,
                        "targets": [], "receivers": {}, "capitals_status": "idle"}
                assert set(snapshot) == set(base) | {"monotonic_ms", "video", "browser_graphics", "metrics"}
                assert {key: snapshot[key] for key in base} == base
                assert type(snapshot["monotonic_ms"]) is int and snapshot["monotonic_ms"] >= 0
                assert snapshot["video"] == {
                    "configured_encoder": settings["video"]["encoder"],
                    "resolution": settings["video"]["resolution"], "fps": settings["video"]["fps"],
                    "rate_control": settings["video"]["rate_control"],
                    "bitrate_mbps": settings["video"]["bitrate_mbps"],
                    "max_bitrate_mbps": settings["video"]["max_bitrate_mbps"],
                    "stream_encoder": None, "stream_fps": None,
                    "sportsnet_rendering": settings["browser"]["sportsnet_rendering"]}
                assert snapshot["browser_graphics"] == {
                    "chrome_alive": False, "active_profile": None, "launch_rendering": None,
                    "gpu_flags_applied": None, "render_node": None,
                    "gpu_process_count": None, "gpu_render_device_open": None}
                assert snapshot["metrics"] == {"source": {}, "receivers": {}}
                assert not application.browser.running
                assert not application.controller.targets
                assert not application.controller.stream
            finally:
                await application.close()
            assert not application.supervisor_control.requests
            assert application.supervisor_control.reader_task.done()
    print("Production Supervisor stdin lifecycle passed: no network, browser, or TV playback")


asyncio.run(main())
