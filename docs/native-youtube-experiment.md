# Direct YouTube video

Version 0.2.15 makes **Direct video** the default for the everyday **YouTube
video** source. It prepares the public video's media tracks and lets one Apple
TV fetch rolling HLS, without capturing Chrome. Installed short trials passed
at 1080p60 with both tracks copied and at 4K60 with video converted to HEVC and
AAC audio copied. The user confirmed smooth video and audio for both. This
remains experimental; long playback and measured synchronization need validation.

## Everyday controls

Paste one public YouTube video link, choose exactly one paired TV and press
**Play on selected TVs**. Playback starts from the beginning; links with a start
time are rejected. Use **Stop** or **Stop all** to end the session. Seeking and
pause controls are not supported; a long pause on the TV can outlast the rolling
buffer. Start again with Play when needed.

**Best available for the TV** selects the highest available resolution and then
frame rate within the known receiver model's limits. **Up to 1080p** and **Up to
720p** are explicit source-selection limits. The selected source frame rate is
preserved, including 60 fps when available. Browser capture resolution, FPS,
bitrate/VBR and browser YouTube quality preferences do not govern Direct video.
Direct video currently requires sound to be enabled.

Choose **Browser playback** explicitly for the existing Chrome playback path,
including browser sign-in and multiple TVs. **Watch Later** continues to use
YouTube's own queue in the signed-in browser. Direct video never imports browser
cookies, sign-ins or Watch Later data. It accepts completed public videos and
SDR; account-only, live, DRM, HDR and unverified receiver/media properties fail
explicitly. It never silently switches to screen capture or reduces quality to
make a missing encoder work.

## Track selection and preparation

Video and audio are planned independently. At the chosen dimensions and frame
rate, compatible H.264/HEVC and AAC-LC tracks are copied. Only an incompatible
track is decoded and encoded; joining separate tracks into fragmented MP4 is
repackaging rather than video transcoding.

| Source | Prepared media |
| --- | --- |
| Compatible H.264 1080p60 + AAC-LC | Copy both tracks |
| VP9 2160p60 + AAC-LC | Convert video to HEVC 2160p60; copy AAC |
| Compatible HEVC + Opus | Copy video; convert audio to AAC-LC |

The 0.2.15 image recipe packages pinned yt-dlp, its matching EJS solver and a
supported Node runtime in the service's Python environment. FFmpeg keeps GPL
and nonfree features disabled while adding HTTPS/HLS, VP9/HEVC/Opus decoding,
dav1d AV1 decoding and HEVC VAAPI encoding. See [dependency
provenance](provenance.md) for versions, hashes, sources and notices.

HEVC conversion requires a working host VAAPI encoder. Before playback, an
actual bounded encode checks the requested profile, dimensions and frame rate;
opening a render node or listing an encoder is insufficient. Source tracks,
effective A/V starting timestamps and the first generated segment are also
probed. These checks do not establish sustained 4K60 throughput: that still
requires the installed host and actual TV. Missing capabilities produce an
error while retaining the requested quality.

Apple's published model limits allow 4K60 SDR on supported Apple TV 4K models;
they are planning ceilings, not proof of the sender, display or HDMI chain.
See Apple's specifications for [HD](https://support.apple.com/en-us/111928),
[4K generation 1](https://support.apple.com/en-us/111929),
[4K generation 2](https://support.apple.com/en-us/111922), and
[4K generation 3](https://support.apple.com/en-us/111839).

## Session ownership and limits

`NativeSession` owns `NativeHLSStream`, `NativeHLSOrigin` and
`NativeEnginePlayer`. Preparation begins before replacing the active source.
The engine uses the selected TV's existing saved pairing in place; pairing keys
do not leave the engine. Stop, replacement, failure and normal completion close
the player and origin before removing generated media.

Preparation is progressive rather than a full-video download. The playlist has
12 rolling segments, with a four-second target per segment and six additional
segments retained for in-flight requests. Actual segment lengths can follow the
source keyframes. The default session has a 512 MiB periodic disk limit and a
four-hour total lifetime, including startup and final buffering; videos must
fit inside that budget. The disk check is not a filesystem quota.

The origin serves only finalized playlist, initialization and segment files
from its session directory to the selected receiver and a separate local
preflight client. Temporary files and unrelated app data are not served.
The playback report separates protocol progress from receiver fetches and
shows selected quality and track-copy decisions. Fetch counters prove delivery,
not physical picture, sound or lip-sync.

## Verification and remaining acceptance

- A public YouTube H.264 1080p60/AAC sample retained all 1,201 video and 862 audio
  packet hashes through repackaging. A 10-second VP9 4K60 sample converted to
  HEVC while retaining dimensions, frame rate and all 431 AAC packet hashes.
- Offline progressive HLS preserved the compressed H.264/AAC and HEVC/AAC
  packets. Public YouTube resolution, HTTPS probing and first-segment validation
  reached readiness at 1080p60 without encoding, then cancelled and cleaned up.
  Those tests used the experiment runtime and did not contact a TV.
- Installed 0.2.12 native MP4 control, receiver fetches and cleanup passed on
  two saved receivers. The user confirmed the beep was audible on one receiver.
- Installed 0.2.15 played public YouTube HLS on a saved Apple TV 4K in two
  bounded trials: H.264/AAC 1920×1080 at 60 fps with both tracks copied, then
  HEVC/AAC 3840×2160 at 60 fps with video converted and audio copied. The user
  confirmed smooth video and audio in both trials; Chrome stayed closed.
- Complete-video behavior, long-run conversion stability, other receivers and
  measured lip sync remain unverified by those short playback trials.

The troubleshooting **Test direct video** action remains a bounded generated
MP4 picture-and-sound test for one TV. It is separate from everyday YouTube
playback. `service.native_prepare` and `service.native_probe` remain optional
operator experiments; they are not required for the everyday controls.
