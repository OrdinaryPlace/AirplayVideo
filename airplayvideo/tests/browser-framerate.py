"""Opt-in 1080p motion capture benchmark, using an isolated container browser."""

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
import tempfile
import statistics
import time
import uuid
import aiohttp
from aiohttp import web
from service.diagnostics import command
from service.browser import Browser
from service.controller import Stream
from service.model import default_setup

PAGE = """<!doctype html><title>1080p motion benchmark</title><style>
html,body{margin:0;width:100%;height:100%;overflow:hidden;background:black}
video{width:100%;height:100%;object-fit:fill}
</style><video autoplay loop playsinline src="/motion.mp4"></video><script>
const video=document.querySelector('video');
setInterval(()=>{
 const q=video.getVideoPlaybackQuality();fetch('/stats',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({time:performance.now(),total:q.totalVideoFrames,dropped:q.droppedVideoFrames,width:innerWidth,height:innerHeight,videoWidth:video.videoWidth,videoHeight:video.videoHeight})});
},500);
video.addEventListener('playing',()=>fetch('/ready',{method:'POST'}),{once:true});
</script>"""


async def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New directory for generated test captures",
    )
    parser.add_argument(
        "--encoder", choices=("libopenh264", "h264_vaapi"), default="libopenh264"
    )
    args = parser.parse_args()
    output = args.output
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    await command(
        "ffmpeg",
        "-v",
        "error",
        "-nostdin",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=1920x1080:rate=60",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000",
        "-t",
        "12",
        "-c:v",
        "libopenh264",
        "-b:v",
        "8M",
        "-g",
        "60",
        "-c:a",
        "aac",
        "-movflags",
        "+faststart",
        output / "motion-1080p60.mp4",
        timeout=60,
    )
    ready = asyncio.Event()
    samples = []

    async def loaded(_):
        ready.set()
        return web.Response(text="ok")

    async def stats(request):
        samples.append(await request.json())
        return web.Response(text="ok")

    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text=PAGE, content_type="text/html"))
    app.router.add_get(
        "/motion.mp4", lambda _: web.FileResponse(output / "motion-1080p60.mp4")
    )
    app.router.add_post("/ready", loaded)
    app.router.add_post("/stats", stats)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    async with aiohttp.ClientSession() as session:
        with tempfile.TemporaryDirectory() as temporary:
            Path(temporary).chmod(0o755)
            browser = Browser(Path(temporary) / "browser", session)
            try:
                await browser.navigate(
                    "http://127.0.0.1:"
                    + str(site._server.sockets[0].getsockname()[1])
                    + "/",
                    default_setup(),
                )
                await asyncio.wait_for(ready.wait(), 25)
                await asyncio.sleep(3)
                for trial in range(1, 4):
                    label = "trial-" + str(trial)
                    root = output / label
                    root.mkdir(exist_ok=True)
                    finished = asyncio.get_running_loop().create_future()
                    media = []

                    async def event(_, name, fields):
                        if name == "recording_finished" and not finished.done():
                            finished.set_result(fields)
                        if name == "media":
                            media.append({"wall": time.monotonic(), **fields})
                        if name in ("source_error", "fatal") and not finished.done():
                            finished.set_exception(RuntimeError(fields["message"]))

                    env = dict(browser.environment)
                    config = {
                        "source": {
                            "kind": "browser",
                            "display": env["DISPLAY"],
                            "pulse": "airplayvideo.monitor",
                        },
                        "width": 1920,
                        "height": 1080,
                        "fps": 30,
                        "bitrate": 8000000,
                        "encoder": args.encoder,
                        "audio": True,
                        "latency_ms": 500,
                    }
                    stream = Stream(root, config, env, event)
                    try:
                        await stream.start()
                        await asyncio.sleep(1)
                        first = len(samples)
                        ident = uuid.uuid4().hex
                        await stream.command(
                            {"action": "record", "id": ident, "seconds": 10}
                        )
                        result = await asyncio.wait_for(finished, 25)
                        ss = samples[first:]
                        a, b = ss[0], ss[-1]
                        seconds = (b["time"] - a["time"]) / 1000
                        rows = list(
                            csv.DictReader(
                                (root / "captures" / (ident + ".csv")).open()
                            )
                        )
                        frames = [r for r in rows if r["track"] == "video"]
                        pts = [int(r["pts_us"]) for r in frames]
                        gaps = [b - a for a, b in zip(pts, pts[1:])]
                        report = {
                            "capture": result,
                            "browser_samples": ss,
                            "media": media,
                            "browser_decoded_fps": (b["total"] - a["total"]) / seconds,
                            "browser_dropped": b["dropped"] - a["dropped"],
                            "capture_fps": (len(pts) - 1)
                            * 1000000
                            / (pts[-1] - pts[0]),
                            "max_capture_gap_ms": max(gaps) / 1000,
                            "gaps_over_50_ms": sum(g > 50000 for g in gaps),
                            "median_capture_age_ms": statistics.median(
                                int(r["age_us"]) for r in frames
                            )
                            / 1000,
                        }
                        (root / "report.json").write_text(json.dumps(report, indent=2))
                        print(
                            json.dumps(
                                {
                                    "case": label,
                                    **{
                                        k: v
                                        for k, v in report.items()
                                        if k
                                        not in ("capture", "browser_samples", "media")
                                    },
                                    "capture_status": result.get("status"),
                                    "video_frames": result.get("video_frames"),
                                    "id": ident,
                                }
                            ),
                            flush=True,
                        )
                    finally:
                        await stream.close()
                    await asyncio.sleep(1)
            finally:
                await browser.close()
    await runner.cleanup()


asyncio.run(asyncio.wait_for(main(), 210))
