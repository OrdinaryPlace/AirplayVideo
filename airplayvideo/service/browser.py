"""A persistent, sandboxed browser on a container-owned virtual display."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
from pathlib import Path
import secrets
import signal
import struct
import time
import aiohttp
from Cryptodome.Cipher import DES
from .model import UserError, check, browser_url


def child_environment():
    # In particular, never give a webpage/browser the Supervisor or MQTT secrets.
    return {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if key in os.environ}


def vnc_key(value):
    """RFB DES uses the bits of each key byte in reverse order."""
    return bytes(int(f"{byte:08b}"[::-1], 2) for byte in value)


class Browser:
    def __init__(self, root, session):
        self.root = Path(root) / "browser"
        self.session = session
        self.children = []
        self.running = False
        self.url = ""
        self.dimensions = (1920, 1080)
        self.lock = asyncio.Lock()
        self.cdp_lock = asyncio.Lock()
        self.vnc_password = None
        self.youtube_task = None
        self.environment = child_environment()
        self.environment.update(HOME=str(self.root), DISPLAY=":99.0", XAUTHORITY=str(self.root / "Xauthority"), XDG_RUNTIME_DIR=str(self.root / "runtime"), PULSE_SERVER="unix:" + str(self.root / "pulse.sock"), PULSE_COOKIE=str(self.root / "pulse-cookie"), PULSE_SINK="airplayvideo")

    def _private_file(self, name, data):
        path = self.root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
            os.fchown(fd, 1000, 1000)
        finally:
            os.close(fd)

    async def launch(self, *args, stdin=None):
        groups = {1000}
        for path in Path("/dev/dri").glob("*"):
            groups.add(path.stat().st_gid)
        process = await asyncio.create_subprocess_exec(*args, env=self.environment, user=1000, group=1000, extra_groups=sorted(groups), start_new_session=True, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        process.component = Path(args[0]).name
        process.diagnostic = ""
        async def discard_errors():
            # Retain only known generic startup conditions, never browser URLs,
            # page text, account details, or raw child logs.
            while line := await process.stderr.readline():
                lower = line.lower()
                if b"namespace" in lower and (b"permitted" in lower or b"failed" in lower):
                    process.diagnostic = "; the host denied a browser sandbox namespace"
                elif b"no usable sandbox" in lower:
                    process.diagnostic = "; a supported browser sandbox is required"
                elif b"permission denied" in lower:
                    process.diagnostic = "; check private browser directory permissions"
        process.error_reader = asyncio.create_task(discard_errors())
        if stdin is not None:
            process.stdin.write(stdin)
            await process.stdin.drain()
            process.stdin.close()
            await process.wait()
            await process.error_reader
            check(process.returncode == 0, "Browser display setup failed")
        else:
            self.children.append(process)
        return process

    async def wait_port(self, port, seconds=20):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            exited = next((child for child in self.children if child.returncode is not None), None)
            if exited:
                await exited.error_reader
                raise UserError(f"Browser component {exited.component} exited during startup (code {exited.returncode}){exited.diagnostic}")
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                writer.close()
                await writer.wait_closed()
                return
            except OSError:
                await asyncio.sleep(0.15)
        raise UserError("Browser startup timed out")

    async def start(self, setup):
        async with self.lock:
            if self.running:
                return
            try:
                self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
                # The browser can traverse its own app data mount but cannot list
                # it or read root-owned settings and pairing files.
                os.chown(self.root.parent, 0, 1000)
                os.chmod(self.root.parent, 0o710)
                os.chown(self.root, 1000, 1000)
                runtime = self.root / "runtime"
                runtime.mkdir(exist_ok=True, mode=0o700)
                os.chown(runtime, 1000, 1000)
                if not (self.root / "pulse-cookie").exists():
                    self._private_file("pulse-cookie", secrets.token_bytes(256))
                self.vnc_password = secrets.token_hex(4).encode()
                self._private_file("vnc-password", DES.new(vnc_key(bytes([23, 82, 107, 6, 35, 78, 88, 7])), DES.MODE_ECB).encrypt(self.vnc_password))
                self._private_file("Xauthority", b"")
                cookie = secrets.token_hex(16)
                await self.launch("xauth", "-f", self.environment["XAUTHORITY"], "source", "-", stdin=f"add :99 MIT-MAGIC-COOKIE-1 {cookie}\n".encode())
                self.dimensions = (1920, 1080) if setup["video"]["resolution"] == "1080p" else (1280, 720)
                width, height = self.dimensions
                await self.launch("Xvfb", ":99", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp", "-auth", self.environment["XAUTHORITY"])
                for _ in range(80):
                    probe = await asyncio.create_subprocess_exec("xdpyinfo", env=self.environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    if await probe.wait() == 0:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise UserError("Virtual display did not start")
                await self.launch("openbox", "--sm-disable")
                await self.launch("pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--log-level=error", "-L", f"module-native-protocol-unix socket={self.root / 'pulse.sock'} auth-cookie={self.root / 'pulse-cookie'}", "-L", "module-null-sink sink_name=airplayvideo rate=48000 channels=2")
                await self.launch("x11vnc", "-display", ":99", "-auth", self.environment["XAUTHORITY"], "-localhost", "-rfbport", "5900", "-rfbauth", str(self.root / "vnc-password"), "-forever", "-shared", "-xkb", "-quiet")
                await self.wait_port(5900)
                await self.launch("google-chrome", "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=9222", "--user-data-dir=" + str(self.root / "profile"), f"--window-size={width},{height}", "--kiosk", "about:blank")
                await self.wait_port(9222)
                self.running = True
                self.url = ""
            except BaseException:
                await self._close()
                raise

    async def cdp(self, method, params=None):
        check(self.running, "Open the browser first")
        async with self.cdp_lock:
            try:
                async with self.session.get("http://127.0.0.1:9222/json/list", timeout=aiohttp.ClientTimeout(total=3)) as response:
                    pages = await response.json()
                page = next((p for p in pages if p.get("type") == "page"), None)
                check(page is not None, "Browser page is unavailable")
                async with self.session.ws_connect(page["webSocketDebuggerUrl"], timeout=5, max_msg_size=2 * 1024 * 1024) as ws:
                    await ws.send_json({"id": 1, "method": method, "params": params or {}})
                    async with asyncio.timeout(10):
                        async for message in ws:
                            if message.type != aiohttp.WSMsgType.TEXT:
                                break
                            result = json.loads(message.data)
                            if result.get("id") == 1:
                                check("error" not in result, "Browser action could not be completed")
                                return result.get("result", {})
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                raise UserError("Browser is not responding; close and reopen it") from exc
        raise UserError("Browser connection ended")

    async def evaluate(self, expression):
        result = await self.cdp("Runtime.evaluate", {"expression": expression, "returnByValue": True, "userGesture": True, "awaitPromise": True})
        check("exceptionDetails" not in result, "The page needs interaction in the browser preview")
        return result.get("result", {}).get("value")

    def cancel_youtube(self):
        if self.youtube_task:
            self.youtube_task.cancel()
            self.youtube_task = None

    async def navigate(self, url, setup, youtube=False, watch_later=False):
        url = browser_url(url)
        await self.start(setup)
        self.cancel_youtube()
        await self.cdp("Page.navigate", {"url": url})
        self.url = url
        if youtube or watch_later:
            self.youtube_task = asyncio.create_task(self.prepare_youtube(setup["browser"]["youtube_quality"], watch_later))

    async def prepare_youtube(self, quality, watch_later):
        try:
            if watch_later:
                for _ in range(30):
                    chosen = await self.evaluate("""(() => {
                      if (!location.hostname.endsWith('youtube.com')) return false;
                      const rows = [...document.querySelectorAll('ytd-playlist-video-renderer')];
                      const row = rows.find(r => {const p=r.querySelector('#progress');return !p || parseFloat(p.style.width)<100;});
                      const link=row?.querySelector('a#video-title');
                      if(!link) return false;link.click();return true;
                    })()""")
                    if chosen:
                        break
                    await asyncio.sleep(1)
            preferred = {"1080p": "hd1080", "720p": "hd720", "auto": "auto"}[quality]
            for _ in range(45):
                ready = await self.evaluate("""(() => {
                  if (!location.hostname.endsWith('youtube.com')) return false;
                  const v=document.querySelector('video'),p=document.querySelector('#movie_player');
                  if(!v||!p||v.readyState<2) return false;
                  const q=""" + json.dumps(preferred) + """;
                  if(q!=='auto'&&typeof p.setPlaybackQualityRange==='function')p.setPlaybackQualityRange(q,q);
                  v.play().catch(()=>{});p.requestFullscreen().catch(()=>{});return true;
                })()""")
                if ready:
                    return
                await asyncio.sleep(1)
        except (UserError, asyncio.CancelledError):
            # Sign-in/consent stays in the preview. No credential or consent automation.
            return

    async def control(self, action, value=None):
        check(self.running, "Open the browser first")
        if action in {"back", "forward", "reload"}:
            self.cancel_youtube()
        if action == "reload":
            await self.cdp("Page.reload")
        elif action in {"back", "forward"}:
            history = await self.cdp("Page.getNavigationHistory")
            target = history["currentIndex"] + (-1 if action == "back" else 1)
            if 0 <= target < len(history["entries"]):
                await self.cdp("Page.navigateToHistoryEntry", {"entryId": history["entries"][target]["id"]})
        elif action == "paste":
            check(isinstance(value, str) and len(value) <= 16384, "Text is too long")
            await self.cdp("Input.insertText", {"text": value})
        elif action == "play_pause":
            await self.evaluate("(() => {const v=document.querySelector('video');if(v){if(v.paused)v.play();else v.pause();}})()")
        elif action == "fullscreen":
            await self.evaluate("(() => {const v=document.querySelector('video');if(v)v.requestFullscreen().catch(()=>{});})()")
        else:
            raise UserError("Unknown browser action")

    async def preview_connection(self):
        check(self.running and self.vnc_password, "Open the browser first")
        reader, writer = await asyncio.open_connection("127.0.0.1", 5900)
        try:
            greeting = await reader.readexactly(12)
            check(greeting == b"RFB 003.008\n", "Unexpected preview protocol")
            writer.write(greeting)
            count = (await reader.readexactly(1))[0]
            methods = await reader.readexactly(count)
            check(2 in methods, "Preview authentication unavailable")
            writer.write(b"\x02")
            challenge = await reader.readexactly(16)
            key = vnc_key(self.vnc_password)
            writer.write(DES.new(key, DES.MODE_ECB).encrypt(challenge))
            await writer.drain()
            check(await reader.readexactly(4) == b"\0\0\0\0", "Preview authentication failed")
            # The ingress websocket supplies authentication to the UI, while the
            # loopback VNC listener still requires its private session password.
            return reader, writer
        except BaseException:
            writer.close()
            await writer.wait_closed()
            raise

    async def _close(self):
        self.cancel_youtube()
        if self.running:
            with contextlib.suppress(UserError, OSError):
                await self.cdp("Browser.close")
        self.running = False
        for child in reversed(self.children):
            if child.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(child.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(child.wait(), 4)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(child.pid, signal.SIGKILL)
                    await child.wait()
            await child.error_reader
        self.children.clear()
        self.url = ""
        self.vnc_password = None

    async def close(self):
        async with self.lock:
            await self._close()
