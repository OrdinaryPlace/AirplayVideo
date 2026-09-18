"""Private listeners when discovery shares Home Assistant's host network."""
import os
import socket
import aiohttp
from .model import UserError, check


def unused_loopback_port():
    # x11vnc needs a port argument rather than an inherited listening socket.
    # Its private password makes a bind race fail authentication, not attach to
    # another application's preview.
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


async def ingress_listener():
    if os.environ.get("AIRPLAYVIDEO_STANDALONE") == "1":
        return "0.0.0.0", 8099  # Development: publish this on host loopback only.
    token = os.environ.get("SUPERVISOR_TOKEN")
    check(token, "Home Assistant ingress configuration is unavailable")
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as session:
            async with session.get("http://supervisor/addons/self/info", headers={"Authorization": "Bearer " + token}) as response:
                check(response.status == 200, "Home Assistant ingress configuration is unavailable")
                info = await response.json()
        check(info.get("result") == "ok", "Home Assistant ingress configuration is unavailable")
        port = info["data"]["ingress_port"]
        check(type(port) is int and 0 < port <= 65535, "Home Assistant did not assign an ingress port")
        # Bind only to Supervisor's host-side bridge, never the LAN interface.
        # The request boundary additionally requires source 172.30.32.2.
        return "172.30.32.1", port
    except (aiohttp.ClientError, TimeoutError, KeyError, TypeError, ValueError) as exc:
        raise UserError("Home Assistant ingress configuration is unavailable") from exc
