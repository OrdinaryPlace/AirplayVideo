"""Route Chrome through one private gateway without a direct fallback."""
from __future__ import annotations

from urllib.parse import urlsplit

from .model import validate_browser_proxy


def chrome_proxy_arguments(value):
    proxy = validate_browser_proxy(value)
    if not proxy:
        return []
    host = urlsplit(proxy).hostname
    # CONNECT carries origin hostnames to the proxy. Block speculative/local
    # origin DNS too. Chromium applies mappings to literal addresses as well,
    # so exclude the gateway whether it is a DNS name or a private IP.
    resolver = "MAP * ~NOTFOUND, EXCLUDE " + host
    return [
        "--proxy-server=" + proxy,
        "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=" + resolver,
        "--disable-quic",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
