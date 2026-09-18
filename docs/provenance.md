# Source and dependency provenance

AirplayVideo is a new repository with new application history. Its C++ transport
and capture foundation comes from our separately written mirroring and direct
FFmpeg experiments. Double Take is not a source dependency and no Double Take
implementation or private Git history is included. Upstream Double Take was
examined earlier to understand the problem; this is not a formal clean-room claim.

## Direct source dependencies

| Component | Version/source | License/use |
| --- | --- | --- |
| pair_ap | [ejurgensen/pair_ap](https://github.com/ejurgensen/pair_ap/tree/7f53a9c1369162d40c903e3d3df95083ef398d99) | MIT; vendored pairing/identity and encrypted control primitives, with its original notices |
| FFmpeg | [8.0.1 source](https://ffmpeg.org/releases/ffmpeg-8.0.1.tar.xz) | LGPL build; shared libraries, GPL and nonfree disabled |
| OpenH264 | [Cisco 2.6.0](https://github.com/cisco/openh264/tree/v2.6.0) | BSD 2-Clause; built from source and dynamically linked |

The Dockerfile verifies source archive SHA-256 values:

```
FFmpeg 8.0.1: 05ee0b03119b45c0bdb4df654b96802e909e0a752f72e4fe3794f487229e5a41
OpenH264 2.6.0: 558544ad358283a7ab2930d69a9ceddf913f4a51ee9bf1bfb9e377322af81a69
```

The runtime image retains the exact archives and FFmpeg configuration under
`/opt/dependency-sources/`, and license notices under `/opt/licenses/` and
`/usr/share/doc/`. The build recipe supports replacing the shared libraries.
If distributing image binaries, preserve corresponding sources and applicable
notices; see [FFmpeg's license guidance](https://ffmpeg.org/legal.html). Source
availability alone does not grant codec patent licenses. OpenH264 is compiled
here; this is not Cisco's separately distributed binary package.

Other dependencies are installed from Debian bookworm or Google's official
Chrome distribution. Their versions are recorded by `dpkg-query` in the image.
They retain their licenses, including OpenSSL (Apache-2.0), libsodium (ISC),
libplist (LGPL), nlohmann/json and cpp-httplib (MIT), aiohttp (Apache-2.0),
Paho MQTT (EPL-2.0/EDL-1.0), Python Xlib (LGPL-2.1), PyCryptodome (BSD/public-domain components), and
noVNC (MPL-2.0). Xvfb, Openbox, PulseAudio and x11vnc are separate system
processes under their respective licenses; noVNC is a separately served browser
module. Google Chrome is not covered by this repository's MIT license.

## Protocol and integration references

- [pair_ap's pairing notes](https://github.com/ejurgensen/pair_ap)
- [pyatv AirPlay 2 sender](https://github.com/postlund/pyatv/blob/master/pyatv/protocols/raop/protocols/airplayv2.py)
  and its packet/audio implementations (MIT): protocol reference for PCM setup,
  packet authentication, sync and resend behavior; not a runtime dependency.
- [OpenAirPlay protocol notes](https://openairplay.github.io/airplay-spec/)
- [Shairport Sync timing receiver](https://github.com/mikebrady/shairport-sync/blob/master/rtp.c)
  (MIT) and [AirPlay 2 protocol notes](https://github.com/SteeBono/airplayreceiver/wiki/AirPlay2-Protocol):
  references for RTP/NTP latency bounds and screen-audio metadata. No receiver
  implementation is included or linked into AirplayVideo.
- [SiliconDust HTTP development](https://www.silicondust.com/hdhomerun/hdhomerun_http_development.pdf)
- [Home Assistant app configuration](https://developers.home-assistant.io/docs/apps/configuration/)
- [Home Assistant MQTT discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery)
- [RFB specification](https://www.rfc-editor.org/rfc/rfc6143)
  for browser preview framing and VNC challenge-response.

The independently written browser companion follows Chrome's documented
[Linux external extension installation](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions),
[native messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging),
and [CRX3 format](https://chromium.googlesource.com/chromium/src/+/main/components/crx_file/crx3.proto).
The earlier app's operational notes identified the no-debugging sign-in behavior;
its browser-control implementation was not copied into this repository.

Protocol constants and interfaces are implemented in the app's own C++/Python
code. Dependency licenses apply to their components; the MIT license applies to
new AirplayVideo code only.
