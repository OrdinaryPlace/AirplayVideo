"""Exercise the installed native HDHomeRun HTTP path without a physical tuner."""
import asyncio
import gzip
from pathlib import Path
import shutil

from aiohttp import web
import pytest

from service.browser import child_environment
from service.controller import Stream
from service.model import UserError

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(
    not shutil.which("airplayvideo-engine"), reason="Run in the Linux app image")]


async def serve(handler):
    app = web.Application()
    app.router.add_get("/auto/v1.1", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    return runner, f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/auto/v1.1"


def config(url):
    return {"source": {"kind": "hdhomerun", "url": url}, "width": 1920, "height": 1080,
            "fps": 30, "bitrate": 4000000, "encoder": "libopenh264", "audio": True,
            "deinterlace": True, "latency_ms": 500}


async def test_broadcast_http_recovers_uses_one_connection_and_releases_it(tmp_path):
    data = gzip.decompress((Path(__file__).parent / "fixtures/broadcast-mid-gop.ts.gz").read_bytes())
    released = asyncio.Event()
    requests = 0

    async def channel(request):
        nonlocal requests
        requests += 1
        response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await response.prepare(request)
        try:
            await response.write(data)
            while request.transport is not None and not request.transport.is_closing():
                await asyncio.sleep(.05)
        finally:
            released.set()
        return response

    runner, url = await serve(channel)
    events = []

    async def note(_, event, fields):
        events.append((event, fields))

    stream = Stream(tmp_path, config(url), child_environment(), note)
    try:
        await stream.start()
        await asyncio.sleep(2.5)
        assert any(e == "source_recovered" for e, _ in events)
        assert any(e == "media" and f["video_frames"] >= 60 and f["audio_packets"] >= 200 for e, f in events)
        assert not any(e in {"source_error", "fatal"} for e, _ in events)
        await stream.close()
        await asyncio.wait_for(released.wait(), 2)
        assert requests == 1  # No second tuner reservation to probe playback.
    finally:
        await stream.close()
        await runner.cleanup()


async def test_tuning_failure_waits_for_device_and_reports_a_useful_error(tmp_path):
    async def channel(_):
        # The hardware can wait five seconds before returning its tune error.
        await asyncio.sleep(5.2)
        return web.Response(status=503, headers={"X-HDHomeRun-Error": "807"})

    runner, url = await serve(channel)

    async def note(*_):
        pass

    stream = Stream(tmp_path, config(url), child_environment(), note)
    try:
        with pytest.raises(UserError, match="HDHomeRun could not provide this channel") as error:
            await stream.start()
        assert "http" not in str(error.value)
        assert "reception" in str(error.value)
    finally:
        await stream.close()
        await runner.cleanup()
