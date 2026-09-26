# Development

Use an amd64 Linux Docker environment. From the repository root:

```sh
docker build --platform linux/amd64 -t airplayvideo:dev airplayvideo
docker run --rm --platform linux/amd64 airplayvideo:dev \
  python3 -m pytest tests -q -p no:cacheprovider
```

CMake/CTest runs during the image build. It exercises normal PIN pairing and
identity verification with generated keys, authenticated framing and tamper
rejection, media timestamps beyond two hours, shared subscriber delivery, stereo
PCM, independent FFmpeg decoding of encrypted ALAC wire packets, and a
generated MPEG-TS input through the actual source pipeline.
The frame-rate regression checks jitter and clock phase at 30 and 60 fps:
browser capture is already paced by X11, while tuner input still needs its
independent output-rate limit.
Python tests cover configuration persistence, guarded API access, mode/format
validation, source replacement, Stop during warmup, independent receivers,
MQTT discovery and stale command rejection. Host-network tests verify that
concurrent engines allocate separate loopback endpoints and that ingress fails
closed without a valid Supervisor assignment. The browser coexistence check
starts two private displays while conventional VNC/debugging ports are occupied.
It verifies ordinary `navigator.webdriver === false` from a synthetic page,
absence of debugging/automation flags, native Unicode paste without submitting,
Back/Forward/Reload, zoom, and retained profile/extension identity after restart.
The signed extension is installed with the production `umask 077`:

```sh
docker run --rm --cap-add SYS_ADMIN --shm-size=256m \
  -e PYTHONPATH=/opt/airplayvideo airplayvideo:dev \
  python3 tests/browser-network.py
```

To measure full-screen motion capture without an account, tuner, or TV, run the
bounded benchmark below. It creates a 1080p60 H.264 motion fixture, plays it in
ordinary sandboxed Chrome, and records three ten-second 1080p30 trials through
the production engine. Each trial reports actual capture timestamps, frame gaps,
capture age, and the browser's decoded/dropped frame counters. Keep host load
comparable between versions; emulation results do not establish HA hardware
performance. Encoded frame counts alone do not prove that every frame is fresh.

```sh
mkdir -m 700 browser-benchmark
docker run --rm --cap-add SYS_ADMIN --shm-size=256m --network none \
  -e PYTHONPATH=/opt/airplayvideo \
  -v "$PWD/browser-benchmark:/results" airplayvideo:dev \
  python3 tests/browser-framerate.py --output /results/run
```

The output directory must be new. Captures contain only the generated fixture;
the temporary Chrome profile is discarded. To test a Linux host's supported
VAAPI encoder, additionally pass `--device /dev/dri` to Docker and
`--encoder h264_vaapi` to the script. Do not mount production profiles or app
data. The installed app's existing diagnostic report also includes decoded
frame counts, measured fps and longest frame gap under `capture.video` and
`wire.video`; its flash reference measures throughput, not motion fidelity.

For an isolated local UI test, create an empty Docker volume. Never
mount production app data, an existing browser profile, or receiver pairings.
Use a Linux volume rather than a macOS bind mount: the browser's private Unix
sockets need permissions that Docker's macOS file sharing may not support.

```sh
docker volume create airplayvideo-dev-data
docker run --rm --name airplayvideo-dev --platform linux/amd64 \
  --cap-add SYS_ADMIN --shm-size=256m \
  -p 127.0.0.1:8099:8099 \
  -e AIRPLAYVIDEO_STANDALONE=1 \
  -v airplayvideo-dev-data:/data airplayvideo:dev
```

Open `http://127.0.0.1:8099/`. Standalone mode bypasses the HA ingress source-IP
check **only for development**. Bind it to loopback. This sample does not publish
AirPlay timing ports and is intended for setup/browser testing, not TV delivery.
Remove `AIRPLAYVIDEO_STANDALONE` for Home Assistant; never set it in a published
app configuration.

A hardware-enabled native Linux host is needed to test VAAPI. Docker on an
Apple Silicon Mac can run the amd64 software tests, but that is not a benchmark
of the HA host. Physical picture, audio, lip sync and multi-TV timing require
receiver observations; packet counters do not establish those results.

Do not enable `DEBUG_PAIR`, capture login pages, commit `.dev-data`, or copy
another application's source, pairing secrets or runtime history into this repo.
New fixes should add a test for the behavior that failed, rather than duplicating
the implementation. App changes after installation require a version bump.

The generated CTest covers text/Unicode, timer boundaries, daylight saving time,
changing pixels, delayed receiver arrival, the first decodable keyframe, and
natural completion after the video buffer drains. Python tests cover additive
settings migration, strict automation overrides, exact targets and retriggering
without stale completion events stopping the replacement.

For a container capture/measurement with no TV or account involved, mount an
empty private output directory at `/captures`, set `CAPTURE_OUTPUT=/captures`,
and run `python3 tests/capture-sync.py` with the browser test's capability and
shared-memory options. Run `airplayvideo-measure-sync /captures/captures/<id>.mkv`
in the same image to measure the fixture's decoded flash/beep offsets. This is
container capture, not a recording of the host desktop. See
[timing and recording details](audio-video-timing.md).
