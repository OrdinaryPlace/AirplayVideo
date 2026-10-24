# Surfshark Gateway

This experimental Home Assistant app gives AirplayVideo's Chrome an internal
HTTP CONNECT proxy through Surfshark. Its tunnel, routes, DNS changes, and
firewall belong to a separate bridged container. AirplayVideo's existing LAN
AirPlay discovery and receiver traffic remain outside that container.

Do not change this app to host networking. Keep Protection mode and AppArmor
enabled. It requests `NET_ADMIN` and `/dev/net/tun` for its own network namespace;
it requests no Home Assistant API, Docker socket, host PID, shared folders, or
published host ports. Verify TUN access on the installed HA host before use.
It does not install or configure a VPN on Home Assistant's host.

## Setup

1. Install this app from the same repository as AirplayVideo. It starts manually.
   Preserve unrelated playback, including a TV already streaming Sportsnet with
   its own Surfshark app; this gateway does not manage that device or tunnel.
2. For `openvpn`, enter the **manual setup service username and password** from
   Surfshark's account portal, not the ordinary account password. The default
   is Canada, Montreal, OpenVPN UDP; TCP is also available. No subscription is
   purchased and no new account is created.
3. Alternatively, select `wireguard` and enter a registered Surfshark manual
   WireGuard private key and the IPv4 `Address` including its CIDR prefix from
   the downloaded configuration. Do not use the public key. IPv6 address input
   is not supported in this initial version. Keep unused credential fields
   empty, or clear them after a deliberate protocol change.
4. Start the app. Credentials remain in Supervisor's private options and
   root-owned mode-0600 files under `/data/credentials`; the temporary validated
   options snapshot is removed before Gluetun starts. Treat the app's native
   backup as private, because its data includes VPN credentials.
5. Find its installed app ID in Supervisor. Internal DNS replaces underscores
   with hyphens, so local installation `local_surfshark_gateway` becomes
   `local-surfshark-gateway`. A repository installation uses that repository's
   ID prefix instead of `local`. Verify resolution from AirplayVideo; do not
   save a container IP that could change after recreation.
6. Configure the AirplayVideo browser connection to
   `http://<gateway-dns-name>:8888`, with no direct fallback. Browser integration
   is separate from this app. Disable the Surfshark Chrome extension during
   this comparison to avoid competing proxy/privacy settings. No gateway
   credentials need to enter Chrome.

All web browsing in a Chrome process configured with this proxy uses the same
connection. This app does not implement a per-site allowlist. A tunnel can
connect successfully while Sportsnet rejects its public exit IP, account,
location, or playback environment; changing protocol is not proof of access.
Compare against the known-working full Surfshark app and record the actual
endpoint/protocol and observed result without credentials or authentication URLs.

## Network boundary and limits

- TCP 8888 binds the concrete bridge IPv4 discovered inside the gateway before
  VPN startup. It does not use Go's potentially dual-stack wildcard listener.
  No port is published on the host or LAN. The trust boundary is
  that internal network: **other apps on it can also reach the proxy**. Gluetun's
  built-in firewall allows local bridge traffic; `FIREWALL_INPUT_PORTS=8888` is
  not a client-source restriction. Do not expose or route this bridge to an
  untrusted network.
- No guessed source-IP ACL is installed. A future restriction to host-network
  Chrome must first verify its actual stable bridge source address and apply
  first-position rules inside this gateway, ahead of Gluetun's local accepts.
  It must not change Home Assistant's host firewall.
- The control server is loopback-only at `127.0.0.1:8000`; health is loopback-only
  at `127.0.0.1:9999`. The inherited Docker healthcheck uses that health endpoint.
  No external HA watchdog URL is configured for a loopback-only listener.
- Gluetun's firewall stays enabled. Its own DNS resolver uses DNS over TLS
  through the tunnel. DNS-based small health checks avoid requesting `NET_RAW`.
  Ad, surveillance, and malicious-domain blocking are disabled for this scoped
  streaming comparison. Proxy request logging is disabled; ordinary logs are
  restricted to warnings/errors. Do not publish raw options or logs.
- The wrapper starts Gluetun with a complete environment allowlist. Supervisor
  tokens, inherited raw VPN secrets, and ambient proxy/firewall overrides are
  not forwarded. Gluetun receives private file paths through its supported
  `_SECRETFILE` variables, never secret command arguments.
- This isolates the gateway; the existing Chrome process still uses the host
  network. Chrome proxy, DNS, WebRTC, QUIC, IPv4/IPv6, and startup/disconnection
  behavior must be checked separately before claiming fail-closed browser
  protection. A healthy gateway or a successful proxy request proves neither
  Sportsnet playback nor physical TV picture/sound.

## Local validation

The official v3.41.3 image index was checked on 2026-10-24:

```text
qmcgaw/gluetun:v3.41.3
sha256:fa19cc76b2af13d57a8d3dc3066f2ada061b1c761b8aecf989b3877c0486e027
amd64 manifest: sha256:1baa76576dea1dfbafbd62bb635daf40fd87171f63273c2fdfe630418783011e
```

From the repository root, build only this small gateway image and run its
startup boundary tests. They use synthetic credentials, replace Gluetun with
a probe, drop network access, and do not connect a VPN or operate Home Assistant:

```sh
docker build --platform linux/amd64 -t surfshark-gateway:test surfshark_gateway
python3 -m unittest discover -s surfshark_gateway/tests -v
sh -n surfshark_gateway/run.sh
git diff --check
```

The credential tests supply synthetic bridge facts because their containers
have no network. They check private-file ownership/modes, literal credential
handling, environment isolation, invalid-input refusal, missing-bridge refusal,
symlink refusal, stale generated credential replacement, and absence of
credential output. They do not verify
HA deployment, TUN access, a real handshake, tunnel failure, browser leaks,
Sportsnet authentication/DRM, or receiver playback. These require scoped live
checks after configuration, while preserving unrelated viewing.

For rollback, stop only this gateway and return the AirplayVideo browser to its
previous connection after stopping its own trial. Preserve private app data
and the existing Chrome profile. No host route, DNS, firewall, or Core change
needs to be undone.

## Sources

- [Official Gluetun v3.41.3 release](https://github.com/passteque/gluetun/releases/tag/v3.41.3)
- [Pinned release Dockerfile and environment options](https://github.com/passteque/gluetun/blob/v3.41.3/Dockerfile)
- [Surfshark manual credentials and protocol setup](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/surfshark.md)
- [Gluetun HTTP proxy options](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/http-proxy.md)
- [Gluetun firewall behavior](https://github.com/qdm12/gluetun-wiki/blob/main/setup/options/firewall.md)
- [HA app configuration](https://contributors.home-assistant.io/apps/reference/configuration/)
- [HA hardware access and Protection mode](https://contributors.home-assistant.io/apps/guides/hardware/)
- [HA internal app DNS](https://contributors.home-assistant.io/apps/guides/communication/)
