"""The private gateway must not introduce a direct browser fallback."""
import pytest

from service.browser_proxy import chrome_proxy_arguments
from service.model import UserError


def test_legacy_direct_browser_has_no_proxy_or_privacy_overrides():
    assert chrome_proxy_arguments("") == []


def test_gateway_dns_is_the_only_local_resolution_exception():
    args = chrome_proxy_arguments("http://016c10d5-surfshark-gateway:8888")
    assert "--proxy-server=http://016c10d5-surfshark-gateway:8888" in args
    assert "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 016c10d5-surfshark-gateway" in args
    # Chromium otherwise implicitly bypasses loopback, even for a fixed proxy.
    assert "--proxy-bypass-list=<-loopback>" in args
    assert "--disable-quic" in args
    assert "--force-webrtc-ip-handling-policy=disable_non_proxied_udp" in args
    assert not any("direct://" in arg or "sandbox" in arg or "debugging" in arg for arg in args)


@pytest.mark.parametrize("proxy", ["http://172.30.33.10:8888", "http://[fd00::10]:8888"])
def test_literal_gateway_needs_no_local_hostname_exception(proxy):
    assert "--host-resolver-rules=MAP * ~NOTFOUND" in chrome_proxy_arguments(proxy)


def test_invalid_proxy_cannot_inject_a_chrome_fallback_or_reveal_credentials():
    for proxy in ("http://user:secret@vpn-gateway:8888", "http://vpn:8888,direct://",
                  "http://vpn:8888 --no-sandbox"):
        with pytest.raises(UserError) as failure:
            chrome_proxy_arguments(proxy)
        assert proxy not in str(failure.value)
        assert "secret" not in str(failure.value)
