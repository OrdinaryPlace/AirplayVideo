"""Explicit browser route selection; gateway reachability is not VPN proof."""
from __future__ import annotations
import asyncio
import contextlib
import copy
from urllib.parse import urlsplit

from .model import browser_proxy_enabled, check, check_browser_connection, effective_browser_proxy


class BrowserVPN:
    def __init__(self, store, browser, controller, configuration_lock, changed=lambda: None):
        self.store, self.browser, self.controller = store, browser, controller
        self.configuration_lock, self.changed = configuration_lock, changed
        self.gateway_reachable = None
        self.probed_proxy = None
        self.task = None
        self.wake = asyncio.Event()

    def state(self):
        settings = self.store.data["setup"]
        proxy = settings["browser"].get("http_proxy", "")
        enabled = browser_proxy_enabled(settings)
        return {"name": settings["browser"].get("vpn_name", "Browser VPN"),
                "configured": bool(proxy), "enabled": enabled,
                "route": "gateway" if enabled else "direct",
                "gateway_reachable": self.gateway_reachable if self.probed_proxy == proxy and proxy else None}

    def require_idle(self):
        control = self.controller
        control.require_no_diagnostic()
        check(not (control.stream or control.pending or control.targets or control.pending_targets or control.tearing_down),
              "Stop playback before changing browser VPN")
        check(not control.recordings or not control.recordings.current,
              "Wait for the diagnostic recording to finish")

    async def set_enabled(self, request):
        async with self.configuration_lock, self.controller.lock:
            await self.set_enabled_locked(request)

    async def set_enabled_locked(self, request):
        """Change route while caller owns configuration_lock and controller.lock."""
        self.controller.require_no_diagnostic()
        check(isinstance(request, dict) and type(request.get("enabled")) is bool, "Choose whether browser VPN is enabled")
        current = self.store.data["setup"]
        check(current["complete"], "Finish Setup first")
        check_browser_connection(request, current)
        enabled = request["enabled"]
        check(not enabled or current["browser"].get("http_proxy", ""),
              "Set a VPN gateway before enabling browser VPN")
        if enabled == browser_proxy_enabled(current):
            return
        self.require_idle()
        settings = copy.deepcopy(current)
        settings["browser"]["http_proxy_enabled"] = enabled
        if effective_browser_proxy(current) != effective_browser_proxy(settings):
            await self.browser.close()
        self.store.setup(settings)
        self.refresh()

    def refresh(self):
        self.wake.set()
        self.changed()

    async def probe(self):
        proxy = self.store.data["setup"]["browser"].get("http_proxy", "")
        reachable, writer = None, None
        if proxy:
            endpoint = urlsplit(proxy)
            try:
                async with asyncio.timeout(3):
                    _, writer = await asyncio.open_connection(endpoint.hostname, endpoint.port)
                    writer.close()
                    await writer.wait_closed()
                reachable = True
            except (OSError, asyncio.TimeoutError):
                reachable = False
            finally:
                if writer:
                    writer.close()
        # A response from an old configured URL must not label a new gateway.
        if proxy != self.store.data["setup"]["browser"].get("http_proxy", ""):
            return
        changed = self.probed_proxy != proxy or self.gateway_reachable != reachable
        self.probed_proxy, self.gateway_reachable = proxy, reachable
        if changed:
            self.changed()

    async def monitor(self):
        while True:
            self.wake.clear()
            await self.probe()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self.wake.wait(), 30)

    def start(self):
        self.task = asyncio.create_task(self.monitor())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
