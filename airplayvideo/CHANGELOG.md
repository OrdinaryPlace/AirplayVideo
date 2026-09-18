# Changelog

## 0.2.3

- Keep a quiet AirPlay event connection alive. The sender previously treated
  90 seconds without an unsolicited event as failure and stopped playback.
- Preserve bounded parsing/authentication after an event begins, and still
  fail on actual closure, malformed events or a stalled partial message.
- Test idle periods, fragmented encrypted events, pipelined messages and real
  disconnects. Retain normal feedback requests and prompt Stop behavior.

## 0.2.2

- Recover when a live MPEG-2 broadcast begins between sequence headers, instead
  of aborting on its first incomplete frame. Stop on sustained unusable data.
- Demux live TV into bounded audio/video packet queues with independently paced
  decoders. Waiting for video no longer prevents reading and delivering audio.
  Both tracks retain the same source timestamps and presentation clock.
- Allow the tuner's five-second tuning response and explain unavailable-channel
  failures in terms of reception or occupied tuners.
- Add a generated 1080i MPEG-2/AC-3 mid-stream fixture, timing and cancellation
  regressions, and real HTTP input/connection-release tests. No codec or buffer
  setting changes are required.

## 0.2.1

- Advertise the selected video latency instead of a fixed 100 ms; identify audio
  as screen audio, with a zero minimum and the selected maximum playout lead.
- Add bounded container recordings of the actual pre-AirPlay H.264/PCM packets,
  preserving relative timestamps, plus downloadable timing reports and CSVs.
- Record an active stream without replacing it, or record an open browser/live
  channel without starting a TV. Keep recordings behind authenticated ingress.
- Expose receiver timing and sender packet age without recording credentials.
- Make newly joined subscribers wait for a decodable keyframe.
- Add real Chrome flash/beep capture measurement. Physical TV lip-sync still
  needs receiver observation; aligned packet clocks alone are insufficient.

## 0.2.0

- Add generated video as a third source: title, tagline, length and time/countdown/neither.
- Render preview and animated output directly in C++ with Pango/Cairo and the shared FFmpeg encoder.
- Start duration on the first TV connection and stop cleanly after its presentation deadline.
- Add saved defaults, a native HA Play generated video button and custom multi-TV MQTT messages with copyable automation YAML.
- Preserve existing settings, pairing identities and browser profiles through an additive upgrade.

## 0.1.6

- Request the selected playback buffer in AirPlay audio SETUP as well as the
  shared audio/video timing. The previous audio request allowed a different
  receiver latency regardless of the selected buffer. Physical lip-sync
  acceptance remains receiver-dependent and needs a listening check.
- Remove the scrollbar when filling YouTube video; restore normal scrolling
  for navigation and consent dialogs. Hide the app scrollbar in expanded preview.
- Keep the first-run wizard and add directly accessible Settings pages afterward,
  with isolated field saves, retained drafts, per-page reset and conflict checks.
- Preserve open browser sessions and avoid tuner/MQTT reconnects for unrelated
  settings changes. Resolution changes still close the browser.
- Put Play below TV selection and show the selected TV names beside the action.

## 0.1.5

- Run ordinary sandboxed Chrome without remote debugging, restoring the browser
  mode previously used for Google/YouTube sign-in. Keep the existing profile.
- Use a signed local extension for navigation and YouTube controls, with native
  Unicode paste and no permission to inspect account pages.
- Add the interactive preview to Setup before setup is complete, with full-screen
  expansion, page zoom, and a return button that preserves the sign-in session.
- Keep Paste visible in fullscreen and fall back to an expanded app view when
  native fullscreen is unavailable.

## 0.1.4

- Restore TV and tuner LAN discovery by using Home Assistant host networking.
- Bind the UI to Supervisor's internal ingress interface and assigned port.
- Allocate private engine/browser ports and the virtual display dynamically so
  other browser apps can remain running on the same host.

## 0.1.3

- Return to playback controls when an expanded browser preview closes.

## 0.1.2

- Preserve unchanged source-mode buttons and TV controls during status polling,
  keeping keyboard focus and in-progress clicks stable.
- Clarify that native Home Assistant controls are created for paired TVs.

## 0.1.1

- Move AirPlay timing and audio recovery to UDP 18200–18215, below the usual
  Linux ephemeral range, after a first-install collision on a busy HA host.

## 0.1.0

Initial experimental release:

- Independent C++ AirPlay mirroring and shared direct-FFmpeg capture/encoding.
- Container browser and HDHomeRun channel modes.
- Separate five-step setup wizard for modes, tuner address, PIN pairing,
  output format/rate, encoder, bitrate, audio and playback buffer.
- Daily playback and authenticated browser preview.
- Standard MQTT discovery for native Home Assistant buttons, selectors and status.
- Explicit Play/Stop lifecycle, bounded receiver queues, preserved pairing/profile,
  and idle restart behavior.
