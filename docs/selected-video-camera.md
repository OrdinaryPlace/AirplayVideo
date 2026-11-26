# Selected video as a Home Assistant camera

AirplayVideo exposes its playing source as one virtual **AirplayVideo selected
video** camera. Select a source through the normal Playback controls, including
**Capitals game**, then press Play. Select TVs to send the same source to those
receivers, or leave TVs unselected to watch through the HA camera alone. Opening
the camera does not change the selected source or start a TV.

Enable native Home Assistant controls in Setup. The app uses Home Assistant's
supported **Generic IP Camera** setup flow to create a streaming camera and
virtual device, normally `camera.airplayvideo_selected_video`. An existing
customized entity name or ID is preserved on subsequent starts. The camera
appears after the first selected video produces both a playable stream and a
snapshot. Registration status and a bounded error are shown in the app.

The camera supports HA's camera cards, snapshots, camera media source and native
HLS viewing helpers, using the same HA viewer path as other streaming cameras.
MQTT is used for the existing TV controls; MQTT's still-image camera discovery
does not provide the native streaming camera needed here. The app requires the
Home Assistant API permission for camera registration. No custom integration or
manual Core configuration edit is required.

## Video and audio

| Selected source | Camera video | Camera audio |
| --- | --- | --- |
| Browser, including Capitals and browser YouTube | Existing shared H.264 encode, copied into HLS | Shared stereo PCM encoded as AAC |
| HDHomeRun | Existing shared H.264 encode, copied into HLS | Shared stereo PCM encoded as AAC |
| Generated video | Existing shared H.264 encode, copied into HLS | Silent |
| Direct YouTube producing H.264 | Existing H.264 HLS output | Existing AAC |
| Direct YouTube producing HEVC for a TV | Separate H.264 camera conversion, up to 1080p | Existing AAC copied |
| Direct YouTube with no TV | H.264 output, up to 1080p | AAC |

The browser, tuner and generated camera output does not run a second video
encoder. It subscribes to the same immutable encoded packets used by the
AirPlay receivers. Its queue is bounded independently, so a slow camera output
cannot block a TV. AAC conversion belongs only to the camera output; AirPlay's
lossless ALAC audio path is unchanged. When a TV receives native HEVC, its
original source quality stays intact while the camera gets H.264.

The camera HLS output uses two-second segments, six visible segments and rolling
deletion. A 128 MiB buffer guard stops a failed camera output independently of
TV playback. Native direct-video preparation retains its existing separate
session and buffer limits. Video is buffered by HA and its viewer, so camera
latency differs from AirPlay latency.

Generated video with TVs still starts its duration when the first TV subscribes;
the passive camera tap does not consume an announcement during connection setup.
For camera-only playback, the camera owns that presentation start. Stopping or
replacing the selected source ends its camera output and releases its tuner or
capture process. A source change does not replay old camera segments under the
new source's identity.

## Access and private state

The camera producer listener is separate from the authenticated ingress UI. In
Home Assistant it binds only the Supervisor bridge address and requires a
persisted private bearer on every playlist, segment and snapshot request. HA's
Generic camera stores that producer URL privately and serves authorized viewers
through HA's own camera pipeline. The listener is not a new LAN-wide or public
stream endpoint.

The app's public state contains readiness and registration status, not producer
URLs, bearer tokens or on-disk paths. Private camera identity and registration
files stay in the app's owner-only data directory. Old source-generation media
URLs cannot read the replacement source. Do not put producer URLs in logs,
automations, tickets or public dashboards; use the HA camera entity instead.

Disabling native Home Assistant controls stops the producer endpoint from
serving selected media. Existing HA entity names and user settings are preserved.

Registration records only its owned Generic Camera entry ID, endpoint digest
and entity ID in `camera-registration.json`; it never edits `.storage`. Endpoint
changes update that one camera. A device disabled in HA stays disabled. If the
initial configuration confirmation response is lost, automatic creation stops
to prevent duplicate devices; inspect HA's Generic Camera devices and the
private registration metadata before recovering that exceptional case.

The supported configuration flow and streaming entity behavior are documented
by [Generic Camera](https://www.home-assistant.io/integrations/generic/) and HA
Core's [Generic setup flow](https://github.com/home-assistant/core/blob/2026.11.1/homeassistant/components/generic/config_flow.py).
[MQTT Camera](https://www.home-assistant.io/integrations/camera.mqtt/) supports
still-image messages rather than a native stream source.

## Verification

The engine camera test produces actual synthetic H.264/AAC media, opens the HLS
playlist through FFmpeg and decodes both tracks. It checks keyframe boundaries,
monotonic timestamps, rolling file retention and an independent receiver
subscriber. It also forces the camera's disk limit and checks that receiver
packets continue, plus generated-video duration with passive and camera-only
subscriptions. These are media-path tests without physical TVs or account data.

Python checks cover private listener access, source replacement, registration
idempotence and the supported Generic camera flow. Installed HA verification
must separately confirm registration, snapshots, playable native HLS and source
replacement. A passing offline test or returned viewing URL does not by itself
prove installed video, sound or a physical TV picture.
