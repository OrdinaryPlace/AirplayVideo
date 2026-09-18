# Changelog

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
