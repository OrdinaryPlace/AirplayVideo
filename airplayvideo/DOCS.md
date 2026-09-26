# AirplayVideo

Send browser pages, HDHomeRun channels, or generated videos to your Apple TVs from Home Assistant.
One source and one C++/FFmpeg encoder supply the same content to the selected TVs.
Each TV has its own AirPlay pairing and connection.

**Experimental, version 0.2.10.** This is an independent implementation in a new
repository. It builds on our C++ mirroring and container capture experiments;
it does not contain Double Take source or its Git history.

## Install

Requires Home Assistant OS or Supervised on **amd64/x86-64**, and an Apple TV
that supports the implemented AirPlay 2 mirroring path. Browser rendering and
encoding run inside the app's Linux container.

[![Add this app repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2FOrdinaryPlace%2FAirplayVideo)

Or open **Settings → Apps → App store → Repositories**, add
`https://github.com/OrdinaryPlace/AirplayVideo`, and install **AirplayVideo**.
The initial installation compiles FFmpeg and the C++ engine; allow several
minutes. Start the app and open its web UI. Keep Protection mode enabled.

## Set up once

The separate five-step **Setup** wizard handles:

1. Enable **Web browser**, **HDHomeRun live TV**, **Generated video**, or any combination.
2. Set the browser home page and YouTube quality preference; find a tuner or
   enter its LAN IPv4 address. The app uses host networking for
   LAN discovery. Across VLANs, multicast/broadcast forwarding may still be
   needed; both tuner and TV discovery also have a manual address fallback.
3. Pair each TV using the fresh four-digit PIN shown on that TV. Pairings and
   browser sign-ins are private app data and survive restarts.
4. Choose resolution, frame rate, encoder, bitrate, stereo audio, and buffer.
   Defaults are **1080p, 30 fps, H.264, 8 Mbps** with a 1500 ms playback buffer.
   Hardware encoding is offered when VAAPI is available; OpenH264 provides
   software encoding. H.264 is the supported video codec in this release.
5. Enable native Home Assistant controls. An available Home Assistant MQTT
   service and the MQTT integration are required for these entities. The
   authenticated app controls also work without MQTT.

Setup changes require playback to be stopped. Settings do not start playback.
Use **Manage pages** on the playback page to add named browser shortcuts.

In **Settings → Picture & sound**, supported hardware offers **Variable** bitrate
with separate target (2–20 Mbps) and maximum (up to 40 Mbps). For 1080p30, try
**16 Mbps target / 30 Mbps maximum**. Simple scenes can use less; detailed motion
can use more, within the encoder's rate-control budget. These are starting
points, not a guarantee of invisible compression. A higher output bitrate cannot
restore detail already missing from YouTube or another source.

The maximum bounds encoded bitrate over the encoder's buffering window, not
every individual network write. Existing installations retain **Automatic** and
their saved bitrate on upgrade. OpenH264 retains automatic rate control; explicit
VBR requires a VAAPI driver advertising support. Unsupported selections are
rejected rather than silently changing encoder or rate mode. These controls apply
to the shared browser, tuner and generated-video encoder and do not change the
playback buffer or audio timing.

After setup, **Settings** has direct navigation between pages. **Save this page**
saves only edited fields; drafts on other pages remain available while you move
around the app. A dot marks unsaved pages. **Reset this page** restores its saved
values. Picture, sound and browser-default edits keep the browser session open;
changing output resolution or disabling browser mode closes it. Settings are
applied to the next playback session.

## Use every day

Choose a saved page, YouTube video, Watch Later, web address, channel, or generated video.
Select the TVs and press **Play on selected TVs**. Use **Stop** for one TV or
**Stop all**. A TV joining the same source shares the existing encoder. Stopping
the last TV closes the source and releases its tuner.

The browser preview lets you navigate, sign in, paste text, and operate the
container browser. In **Settings → Source defaults** (or **Setup → Configure**
on first use), choose **Open browser to sign in**,
then **Full screen preview**. Use **A+ / A−** to enlarge or reduce the page;
**100%** resets its zoom. **Back to Settings** keeps the same browser session.
The expanded layout also works when the HA browser cannot enter native fullscreen.
Preview audio is sent to the TVs, not the preview tab.

Chrome runs normally with its sandbox and persistent profile, without a remote
debugging or browser-automation channel. Sign in yourself through the preview.
The local controls extension uses normal navigation APIs; its page permission
is limited to YouTube playback. It cannot inspect Google account pages. Paste
uses a temporary native clipboard on the private display, inserts one line
without submitting it, then clears that clipboard. Fill video is available for
YouTube; on other sites use the player’s own fullscreen control.
Sign-ins and consent remain under your control. Watch Later uses YouTube's
native queue and requires signing in inside this app's browser.

Home Assistant creates an **AirplayVideo** device for every paired TV, with:

- Saved-page shortcuts, Page selection, and Play selected page.
- Play YouTube URL and Play Watch Later, when browser mode is enabled.
- Play generated video using saved defaults, when generated mode is enabled.
- Channel selection and Play selected channel, when HDHomeRun mode is enabled.
- Stop, connection status, current source, and diagnostic error.

Put these native entities on any dashboard or use them in automations. Selecting
a page/channel only saves the choice; its **Play** button starts playback.
Changing the source changes it for all currently selected TVs. There are no
frame-rate, codec, bitrate, or tuner-address controls in the everyday entities.
The app never resumes playback automatically after a restart.

## Generated videos and automations
Choose **Generated video**, enter a title, optional tagline and length in seconds,
and select **Time**, **Countdown**, or **Neither**. Preview the first frame before
selecting TVs. The app renders plain Unicode text, an animated background/accent,
and the chosen timer directly in C++, then encodes it through the shared FFmpeg
pipeline. It does not open Chrome or use a tuner. Generated videos are silent.

Length accepts **1–86400 seconds**. Countdown starts when the first TV connects;
connection setup does not consume its duration. All selected TVs share that
countdown. Playback ends automatically after the final frame has reached its
presentation deadline. Every new Play, including identical automation messages,
starts a fresh duration. Stop cancels it immediately. Clock mode uses the configured
IANA time zone (including daylight saving time), initially the app's `TZ` or UTC,
and displays 24-hour time. Neither hides the timer but retains animation.

Save starting values in **Settings → Source defaults**. Everyday edits and
custom automation messages do not overwrite those defaults. Each TV's native
**Play generated video** button uses the saved values.

For custom automations, choose the desired TVs in Playback, expand **Use in a
Home Assistant automation**, and **Copy action**. This produces an `mqtt.publish`
action containing this installation's actual command topic and receiver IDs.
Keep `retain: false`; retained playback commands are ignored. Generated commands
use exactly the supplied TV set, replacing an existing shared source. One message
can target several TVs. Unpaired IDs and invalid text/settings are rejected
before playback changes.

The message format is:

```json
{
  "action": "generated",
  "receivers": ["PAIRED_TV_ID"],
  "generated": {
    "title": "Dinner is ready",
    "tagline": "See you downstairs",
    "duration_seconds": 60,
    "display": "countdown",
    "timezone": "America/New_York"
  }
}
```

`receiver` with one ID is also accepted. Missing generated settings use saved
defaults. Titles allow 160 characters and taglines 240; text is never interpreted
as HTML, markup, a filename, or a shell command.

For dynamic HA text, use `tojson` so quotes and line breaks stay valid JSON:

```yaml
action: mqtt.publish
data:
  topic: airplayvideo/INSTALLATION_ID/command
  retain: false
  payload: >-
    {{ dict(action='generated', receivers=['PAIRED_TV_ID'],
            generated=dict(title=message_title, tagline=message_detail,
                           duration_seconds=90, display='countdown')) | tojson }}
```

Define `message_title` and `message_detail` in the automation's variables, and
replace the topic/TV ID using the app's copied example. Messages and settings can
appear in Home Assistant traces and MQTT; avoid including secrets in video text.

## Current boundaries

For delayed sound, open **Playback → Troubleshoot audio and video sync → Record
15-second sample**. Download the video and timing reports to compare the actual
container capture before AirPlay with TV output. Recording alone never starts a
TV. See [audio/video timing](https://github.com/OrdinaryPlace/AirplayVideo/blob/main/docs/audio-video-timing.md)
for buffer details and interpreting captures.

- Up to eight saved TVs; one active browser page, channel, or generated video at a time. Receiver
  connections are independent, but this is not a claim of sample-accurate
  synchronization between TVs.
- Unprotected HDHomeRun MPEG-2/H.264 video with AC-3/AAC/MP2/MP3 audio. DRM,
  HEVC and AC-4 channels are marked unsupported. No ATSC 3.0 decryption.
- AirPlay H.264 video and encrypted lossless ALAC stereo audio. The earlier video-only
  experiment was physically confirmed on one Apple TV. This app's combined
  audio/video and additional receiver compatibility need physical acceptance.
- 720p/1080p at 30/60 fps. A 1080p output canvas does not improve a lower-quality
  source. YouTube may limit the actual quality it supplies.
- Watch Later is a native YouTube launch convenience; there is no independent
  playlist database, bulk playlist synchronization, or account migration.
- A failed source or receiver needs an explicit Play command to reconnect.
  No unattended reconnect loop or migration of another app's pairing/profile.

See [architecture](docs/architecture.md), [verification](docs/verification.md),
[development](docs/development.md), and [dependency provenance](docs/provenance.md).

## License

New AirplayVideo source is [MIT licensed](LICENSE). Third-party components retain
their own licenses. FFmpeg is dynamically linked with GPL/nonfree features
excluded; its exact source archive and build configuration are retained in the
image. See [dependency provenance](docs/provenance.md) before redistribution.
Apple, AirPlay, Home Assistant, YouTube, and HDHomeRun are their respective owners'
trademarks; this project is not affiliated with those vendors.
