# Native YouTube media experiment

This is an experimental native path. Daily YouTube playback still uses the
existing browser capture path. The candidate adds an explicit direct-video
diagnostic; the YouTube resolver and progressive preparer remain separate
experiments. No production dependency change is included.

## Delivery and quality

Native AirPlay URL playback lets Apple TV fetch a prepared MP4. It differs from
the app's type-110 screen mirroring protocol. Compatible compressed video and
audio can remain unchanged, including video with B-frames and ordinary AAC.
YouTube often supplies separate tracks; combining them into MP4 requires
repackaging, not necessarily transcoding.

`service.native_plan` selects the highest resolution and frame rate within the
receiver's published limits, then prefers copying over conversion at that
quality. Video and audio decisions are independent. For example:

| Source | Prepared media |
| --- | --- |
| Compatible H.264 1080p60 + AAC-LC | Copy both tracks into MP4 |
| VP9 2160p60 + AAC-LC | Convert video to HEVC 2160p60; copy AAC |
| Compatible HEVC + Opus | Copy video; convert audio to AAC-LC |

The prototype supports completed public videos and SDR. HDR, DRM, unknown
receiver models, and unverified track properties fail explicitly. It does not
silently fall back to a lower resolution. It does not read browser cookies or
provide Watch Later/account access.

Apple documents 4K60 SDR decoding for the first Apple TV 4K, and higher frame
rates than 30 fps are therefore possible; that does not prove acceptance by a
particular AirPlay sender, display, or HDMI chain. See Apple's specifications
for [HD](https://support.apple.com/en-us/111928),
[4K generation 1](https://support.apple.com/en-us/111929),
[4K generation 2](https://support.apple.com/en-us/111922), and
[4K generation 3](https://support.apple.com/en-us/111839).

## Prepare a bounded sample

Use a separate Python environment with `airplayvideo/requirements-native-experiment.txt`,
a supported Node runtime for yt-dlp, and external FFmpeg/ffprobe executables
with the required decoders and encoders. The production image's minimal FFmpeg
does not include the VP9/AV1 and HEVC conversion path. A distribution change and
its dependency/license review are required before that path can ship.

From the repository root:

```sh
PYTHONPATH=airplayvideo python -m service.native_prepare \
  --url 'https://www.youtube.com/watch?v=aqz-KE-bpKQ' \
  --receiver-model 'AppleTV6,2' --seconds 10 \
  --output-dir /path/to/private/trial \
  --ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe
```

The CLI prepares media only. It does not connect to or play on a receiver.
It bounds extraction, download bytes, and subprocess time; failures retain
partial evidence. Download requests use clip-sized bitrate estimates within
hard byte caps. Direct HTTPS variants must match the best available dimensions
and frame rate. Prefix downloads are an experiment shortcut and can fail
when required media/index data is unavailable within the bound. Final probing
must succeed before the output is marked ready. Initial effective audio/video presentation
times are checked with audio priming applied. Differently offset source tracks
are rejected until offset preservation is implemented; output must retain the
source offset.

`service.native_origin.MediaOrigin` exposes an exact list of prepared files,
supports GET/HEAD byte ranges, restricts clients to explicit IPs, and expires
automatically. It is separate from ingress and must never serve app state or
browser profiles. `tests/native-origin-trial.py` is a finite standalone origin
for an explicitly authorized receiver; it also does not send playback commands.
Receiver request/byte counters are separate from local preflight traffic. These
counters measure sender writes, not TV rendering or audible output.

`service.native_airplay.NativePlayer` accepts the selected receiver and its
existing saved pairing in memory. It converts identity fields without re-pairing
and contains pyatv in a bounded worker process. Do not put credentials in CLI
arguments, environment variables, reports, or logs. This adapter has synthetic
identity/lifecycle tests; its actual saved app identity has not been exercised
on a receiver.

## Modern native control candidate

The C++ engine now has a separate `--native` worker. It reads its existing
receiver pairing in place, verifies it for this connection, and tries a modern
RCS queue session with type-130 control. The legacy mirroring path keeps its
RTSP defaults. Python supplies only a selected receiver ID, the scoped local
media URL, and the trial duration through a private temporary request file.
An expected receiver address is matched against the saved pairing before
network access. No pairing keys leave the engine or appear in child arguments.

The queue protocol is experimental. It requires a queue identity returned by
authenticated receiver information; it does not assume a pairing identifier
is interchangeable. PTP session metadata is negotiated, but this candidate does
not implement a general IEEE-1588 clock. An acknowledgement is not playback.
Safe stage/status events identify where startup fails, while finite HTTP and
worker lifetimes bound each trial. Cancellation waits for child creation,
teardown, resource closure and reaping, including a forced-stop fallback.

The candidate's authenticated troubleshooting panel offers **Test direct
video** for exactly one selected TV. It prepares the existing generated
H.264/AAC reference, uses the saved pairing, and stops within a bounded trial.
It reports receiver fetches separately from protocol responses and leaves
physical picture, sound and synchronization for observation. This diagnostic
does not select native YouTube playback as the default.

`python -m service.native_probe --help` describes the equivalent manual
bounded sample trial for an operator already inside the owning app environment.
Do not export pairing files or weaken Home Assistant protection to run it.
Use the normal tested app update and authenticated diagnostic for Home Assistant.

## Progressive media preparation candidate

`service.native_stream.NativeHLSStream` prepares rolling fragmented-MP4 HLS
without waiting for a full video download. Compatible video and AAC tracks
remain compressed; other tracks need an explicitly available compatible
encoder. The selected resolution and frame rate stay fixed. An actual bounded
encode and the first generated segment are checked before readiness is reported.

`service.native_hls_origin.NativeHLSOrigin` serves only finalized playlist,
initialization and segment filenames from one pinned session directory. It
supports atomic playlist replacement, byte ranges, rolling segment deletion,
per-receiver byte counters and a separate preflight role. Temporary files,
symlinks, hardlinks and unrelated directory contents are not exposed.

The playlist retains a rolling buffer, so seeking is limited to that buffer.
Session time and disk usage are bounded; the disk check is a periodic limit,
not a filesystem quota. A production release still needs full playback/seek
integration, mixed-receiver variants and throughput testing on the actual host.
HEVC VAAPI support must be proven by an encode, not merely opening a render node.
GPL software encoders require an explicit experiment opt-in and are not added
to the production image.

## Evidence and limits, 2026-09-26

- A public YouTube sample was repackaged as 20 seconds of H.264 1920x1080 at
  60 fps and AAC-LC. All 1,201 video and 862 audio packet hashes matched the source.
- A 10-second VP9 3840x2160 at 60 fps sample was converted to HEVC Main (`hvc1`),
  retaining dimensions, 60 fps, BT.709 metadata, and all 431 AAC packet hashes.
  These were offline checks; they do not establish real-time HA encoding speed.
- A scoped LAN origin returned HTTP 206 and the requested bytes from a separate
  LAN host. Its listener was closed at the end of each bounded trial.
- The preparation CLI passed a complete public YouTube test: resolve, bounded
  download, source probe, stream-copy, and final verification produced a
  2-second 1080p60 H.264/AAC sample. Effective A/V start offset remained zero.
- Progressive HLS from those prepared local samples became ready after 6.02
  seconds while generation continued. SHA-256 packet comparisons retained all
  1,201 H.264 video / 862 AAC packets at 1080p60 and all 600 HEVC video / 431 AAC
  packets at 4K60. Both streams reached their expected end and cleaned up their
  generated directories. This proves repackaging, not receiver playback or
  real-time VP9/AV1 conversion on the Home Assistant host.
- The actual public YouTube progressive path also passed without injected
  metadata or local inputs: resolution, HTTPS source probes and first-segment
  validation reached readiness in 11.77 seconds at 1080p60 H.264/AAC with no
  encoding. It continued five seconds, then cancelled and cleaned up. Full
  upstream video completion and receiver playback were not part of that test.
- Native Home Assistant playback on a tvOS 26.6 Apple TV failed before any
  receiver media fetch. A disconnected HA control session was first refreshed;
  the connected retry then reported a native streaming failure. The cause is
  not established. Neither visible native playback nor 4K delivery is verified.

Home Assistant's `remote` entity reports connection state, not physical TV
power. A disconnected remote can make media actions return without doing
anything. Connect it with `remote.turn_on` and verify it is on before a trial;
`media_player.turn_on` cannot repair a missing connection. For native media use
`media_content_type: video`: `url` selects an app-launch path in that integration.

## Tests and promotion gate

```sh
PYTHONPATH=airplayvideo python -m pytest airplayvideo/tests/test_native_*.py -q
```

The first bounded preparation prototype passed 153 native Python tests. The
modern control, progressive HLS, process cleanup and diagnostic integration add
separate regressions, including fake-receiver failures at each protocol stage.
Both media origins are checked against Debian's older aiohttp and the current
optional version. Current candidate validation is recorded with its commit.

The 0.2.11 candidate passes 222 optional-native Python tests, the complete Linux
Python suite, all 10 C++ suites, sandboxed browser/profile/coexistence checks,
and the installed-style local sync diagnostic. The authenticated diagnostic UI
was inspected at desktop and 390-pixel widths. These are local checks; the
candidate has not established native playback on a physical receiver.

Keep this path experimental until the native startup failure is understood and
bounded trials establish picture, sound, sync, and 4K60 playback on actual TVs.
Production work still includes daily playback integration, receiver-specific
variants, full-length seek/stop, audio-route behavior, dependency
packaging, and retained-pairing verification. Do not change the installed path
or claim native playback success based on a successful media conversion.
