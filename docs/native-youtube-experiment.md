# Native YouTube media experiment

This is an isolated prototype, not an option in the installed app. Daily
YouTube playback still uses the existing browser capture path. No release or
production dependency change is included.

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
must succeed before the output is marked ready. Full-length playback will need
a separate streaming/cache design. Initial effective audio/video presentation
times are checked with audio priming applied. Differently offset source tracks
are rejected until offset preservation is implemented; output must retain the
source offset.

`service.native_origin.MediaOrigin` exposes an exact list of prepared files,
supports GET/HEAD byte ranges, restricts clients to explicit IPs, and expires
automatically. It is separate from ingress and must never serve app state or
browser profiles. `tests/native-origin-trial.py` is a finite standalone origin
for an explicitly authorized receiver; it also does not send playback commands.

`service.native_airplay.NativePlayer` accepts the selected receiver and its
existing saved pairing in memory. It converts identity fields without re-pairing
and contains pyatv in a bounded worker process. Do not put credentials in CLI
arguments, environment variables, reports, or logs. This adapter has synthetic
identity/lifecycle tests; its actual saved app identity has not been exercised
on a receiver.

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

All 153 native tests passed with the optional dependencies installed. The
complete Python suite also passed in the existing Linux test image; no C++
engine, browser capture, production controller, or UI code changed. The origin
supports both Debian's older aiohttp and the current optional version.

Keep this path experimental until the native startup failure is understood and
bounded trials establish picture, sound, sync, and 4K60 playback on actual TVs.
Production work still includes controller/UI integration, receiver-specific
variants, full-length buffering/seek/stop, audio-route behavior, dependency
packaging, and retained-pairing verification. Do not change the installed path
or claim native playback success based on a successful media conversion.
