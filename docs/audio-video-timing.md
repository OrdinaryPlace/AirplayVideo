# Audio/video timing and diagnostic captures

Playback → **Troubleshoot audio and video sync** → **Record 15-second sample**
saves the current stream in the app container. With no TV playing, open the
browser first, or choose a live channel; recording alone never starts a TV.
Browser capture uses its existing page and does not navigate. Active playback
uses the same encoded packets through an independent bounded subscriber.

Download the Matroska video, JSON report, and packet timing CSV. H.264 is copied
without re-encoding; stereo PCM is stored losslessly. Both keep their source
timestamps relative to the same first video keyframe. The file is taken **before
AirPlay**: it cannot show how long an Apple TV, HomePod, soundbar, or Bluetooth
output subsequently holds the audio. Do not capture account/login screens.

Recordings are private, explicit, and limited to 5–30 seconds through the API
(15 seconds in the UI), 128 MiB each, and eight retained samples. Download and
Remove control their lifetime. They are not uploaded anywhere. Settings and
pairing files are never included. An app backup may include retained captures.

## Where buffering occurs

| Stage | Behavior |
| --- | --- |
| Browser capture | X11 video and PulseAudio samples use their source wall-clock timestamps. The 4096-byte stereo 48 kHz audio fragment is about 21 ms. |
| Audio conversion | Resample to 44.1 kHz, then assemble 352-sample packets (about 8 ms). The FIFO is drained as soon as a packet is available; its size limit is not a prefill target. |
| Video processing | H.264 has no B frames. Deinterlacing, conversion, and encoding take processing time; they do not rewrite PTS to send time. |
| Per-TV queue | Packets are consumed immediately. A 600-packet/24 MiB ceiling and a source-age deadline stop a lagging receiver rather than accumulating unlimited delay. |
| Playback buffer | The chosen 500–2000 ms is a future presentation deadline. Video timestamps, video SETUP, audio SETUP maximum, and audio RTP/NTP mapping describe that same lead. There is no sender sleep equal to this buffer. |
| Audio recovery | A 512-packet ciphertext history services retransmissions. Keeping a history does not delay first transmission. |
| Receiver output | The TV/output device controls its own decoder, mixer, and hardware delay. Its optional arrival-to-render report is recorded separately, not added blindly as another offset. |

Version 0.2.0 had inconsistent negotiation: video SETUP always said 100 ms,
audio had a hard minimum equal to the selected buffer, and `isMedia` was true
despite screen mirroring. Version 0.2.1 makes video SETUP match the presentation
lead and requests screen audio with `isMedia=false`, `usingScreen=true`, a zero
minimum, and the selected maximum. This corrects inconsistent requests; it does
not by itself prove that a particular receiver's physical output is aligned.

## Reading the evidence

- `pts_us`: source media time. Audio and video share an origin.
- `available_us`: packet ready after capture/conversion/encoding.
- `observed_us`: recording subscriber receives the packet.
- `age_us`: source age, including capture/processing and subscriber queueing.
- `queue_us`: time spent between publication and recording consumption.
- `receiver_timing`: latest sender queue/source-age measurements and negotiated
  audio/video timing when a TV was streaming during this recording.

Watch the downloaded file independently. A visible offset already in the file
points upstream, toward the source/capture/timestamps. A file that is aligned
while the TV is delayed narrows the issue to AirPlay negotiation/delivery or
receiver output. A small packet age alone does not prove content alignment.

The development fixture schedules white flashes and 1 kHz beeps on one browser
audio clock, captures the real X11/Pulse pipeline at 1080p, and measures decoded
flash/beep PTS. It includes browser audio and display scheduling uncertainty,
so frame-scale jitter is expected; hundreds of milliseconds are not.

## Objective synchronization checks

Audio and video must retain one common presentation timeline for every receiver.
There is no per-TV audio offset to tune. The playback buffer changes both tracks'
presentation deadline; room-to-room playback calibration is a separate concern.

The native `sync-content` regression creates an account-free 1080p30 reference
with simultaneous white flashes and 1 kHz beeps. It independently decodes that
file, then measures the same events after the shared file/tuner processing path
and after decrypting and decoding the H.264 and ALAC AirPlay packet formats.
The packet check reconstructs presentation times from the video header and
audio RTP/NTP announcements, including RTP rollover and successive announcements.
It runs with 500 ms and 1500 ms buffers. A second fixture deliberately puts sound
75 ms late without changing track endpoints, to verify that content measurement
detects an error that timestamp-only comparisons cannot.

CI prints signed audio-minus-video measurements for each stage. The controlled
fixture allows 2 ms of additional content error and one audio sample of packet
scheduling error. These are test limits, not a claim of sub-frame browser capture
or physical TV accuracy. This test uses software H.264; installed VAAPI, the
browser's display/audio clocks, network arrival and the physical output still
need their own evidence. No TV, LAN connection, pairing or user media is needed.

To measure the final output, record a known flash/beep pattern with a camera and
microphone on a common recording clock and compare it against the source. Account
for that recording device's own A/V offset, frame interval and microphone
distance. AirPlay timing replies and packet counters cannot measure the instant
the panel emits light or a speaker emits sound. Physical measurements should
validate the common scheduling implementation, not create guessed per-TV delays.

## Codec choices and earlier implementation lessons

Version 0.2.4 sends encrypted ALAC stereo at 44.1 kHz, with 352 samples per RTP
packet. Its lossless escape frames preserve every PCM sample and add only four
bytes of framing, with no compression lookahead or additional buffer. The wire
packet is 1448 bytes (1476 with IPv4/UDP headers), within a 1500-byte MTU.
Recordings still contain lossless PCM before AirPlay framing.

Earlier versions sent bare PCM. A receiver advertising PCM and accepting SETUP
does not establish that its real-time screen-audio decoder accepts bare PCM.
The new path requires the receiver's advertised ALAC capability. An independent
FFmpeg decoder now checks decrypted RTP payloads sample by sample; encryption
round trips alone did not detect this compatibility gap. Physical TV sound and
lip sync still require an actual receiver trial.

The upstream Doubletake sender prefers advertised ALAC, supports AAC-ELD when
built for it, uses source timestamps, discards stale startup audio, and treats
screen audio separately from a primary media session. Our earlier HDHomeRun fork
also normalized GStreamer segment origins before mapping timestamps. The lesson
is to preserve one A/V timeline, not transplant a GStreamer fix into FFmpeg.
AirplayVideo's channel path instead normalizes one demuxer's shared start time.
No Doubletake implementation is incorporated in this change.

Primary references: [Doubletake](https://github.com/omarroth/doubletake),
[FFmpeg PulseAudio input](https://ffmpeg.org/ffmpeg-devices.html#pulse).

Version 0.2.5 also selects the advertised audio connection format (feature 59).
Modern RTP connections explicitly select the stream encryption key; the sender
reads RTP and RTCP ports from the returned connection dictionaries, with legacy
port fields supported for older receivers. Timing reports identify the selected
connection layout. This changes negotiation without adding a buffer or offset.

## Measure the installed container

In Playback, expand **Troubleshoot audio and video sync**. Stop playback and close
its browser, then choose **Measure sync without a TV**. The job uses the saved
encoder at 1080p30, tests 500/1500 ms playback buffers and a deliberately 75 ms late
control, and independently decodes both capture content and AirPlay packet formats.
It also plays an independently decoded H.264/AAC reference through an isolated,
sandboxed Chrome profile on the container's own X11/Pulse display. Browser results
are measurements rather than an assertion that browser rendering is sample exact.

**Measure sync and test selected TVs** adds a 15-second flash/beep trial after the
local stages. It captures the same source while sending, retains bounded receiver
telemetry, and stops automatically. The minimum send margin is the time remaining
until the requested presentation deadline after a sender socket write completes.
A positive margin does not prove network arrival or physical rendering. Audio and
video use the existing shared clock; this feature introduces no timing correction.

Only generated media is captured by this job. Temporary media/profile files are
removed at completion/cancellation; the last numeric JSON report remains private
through authenticated ingress. Existing recordings, pairings, browser sign-ins and
settings are preserved. Start is exclusive with playback, capture and setup changes;
Cancel measurement and Stop all terminate the job. An interrupted report remains
identified as interrupted after restart. The API is `POST api/actions/measure_sync`
with an optional `receivers` array of saved IDs; omit it for no TV. Download the last
report with `GET api/sync-report`. Requests use the ordinary ingress/header boundary.

The software tests do not observe photons or speaker output. To measure that final
boundary, record the known pattern with a camera and microphone sharing a timeline,
account for the recorder's own A/V offset and microphone distance, and state the
frame/sample resolution. Do not derive a per-TV offset from protocol acknowledgments.
