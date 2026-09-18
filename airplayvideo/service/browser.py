"""A persistent, sandboxed browser on a container-owned virtual display."""
from __future__ import annotations
import asyncio
import contextlib
import os
from pathlib import Path
import secrets
import signal
import struct
import time
from Cryptodome.Cipher import DES
from .model import UserError, check, browser_url
from .network import unused_loopback_port
from .companion import Companion, install as install_companion


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
        self.control_lock = asyncio.Lock()
        self.vnc_password = None
        self.vnc_port = None
        self.companion = None
        self.chrome_process = None
        self.youtube_task = None
        self.environment = child_environment()
        self.environment.update(LANG="C.UTF-8", LC_ALL="C.UTF-8", HOME=str(self.root), DISPLAY=":99.0", XAUTHORITY=str(self.root / "Xauthority"), XDG_RUNTIME_DIR=str(self.root / "runtime"), PULSE_SERVER="unix:" + str(self.root / "pulse.sock"), PULSE_COOKIE=str(self.root / "pulse-cookie"), PULSE_SINK="airplayvideo")

    def _private_file(self, name, data):
        path = self.root / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
            os.fchown(fd, 1000, 1000)
        finally:
            os.close(fd)

    async def launch(self, *args, stdin=None, pass_fds=()):
        groups = {1000}
        for path in Path("/dev/dri").glob("*"):
            groups.add(path.stat().st_gid)
        process = await asyncio.create_subprocess_exec(*args, env=self.environment, user=1000, group=1000, extra_groups=sorted(groups), pass_fds=pass_fds, start_new_session=True, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
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

    async def start_display(self, width, height, cookie):
        # X servers share abstract UNIX sockets on the host network. Let Xvfb
        # bind an unused display, then use that exact display for every child.
        read_fd, write_fd = os.pipe()
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0))
        try:
            try:
                await self.launch("Xvfb", "-displayfd", str(write_fd), "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp", "-auth", self.environment["XAUTHORITY"], pass_fds=(write_fd,))
            finally:
                os.close(write_fd)
            display = (await asyncio.wait_for(reader.readline(), 15)).strip()
            check(display.isdigit() and 0 <= int(display) < 65536, "Virtual display did not start")
            self.environment["DISPLAY"] = ":" + display.decode("ascii") + ".0"
            await self.launch("xauth", "-f", self.environment["XAUTHORITY"], "source", "-", stdin=f"add {self.environment['DISPLAY']} MIT-MAGIC-COOKIE-1 {cookie}\n".encode())
        finally:
            transport.close()

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
                await self.start_display(width, height, cookie)
                for _ in range(80):
                    probe = await asyncio.create_subprocess_exec("xdpyinfo", env=self.environment, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    if await probe.wait() == 0:
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise UserError("Virtual display did not start")
                await self.launch("openbox", "--sm-disable")
                await self.launch("pulseaudio", "-n", "--daemonize=no", "--exit-idle-time=-1", "--log-level=error", "-L", f"module-native-protocol-unix socket={self.root / 'pulse.sock'} auth-cookie={self.root / 'pulse-cookie'}", "-L", "module-null-sink sink_name=airplayvideo rate=48000 channels=2")
                self.vnc_port = unused_loopback_port()
                await self.launch("x11vnc", "-display", self.environment["DISPLAY"], "-auth", self.environment["XAUTHORITY"], "-localhost", "-rfbport", str(self.vnc_port), "-rfbauth", str(self.root / "vnc-password"), "-forever", "-shared", "-xkb", "-nosel", "-quiet")
                await self.wait_port(self.vnc_port)
                with contextlib.suppress(FileNotFoundError):
                    (self.root / "profile" / "DevToolsActivePort").unlink()
                identity = await asyncio.to_thread(install_companion, self.root)
                self.companion = Companion(self.root, identity)
                await self.companion.start()
                self.environment.update(AIRPLAYVIDEO_COMPANION_ID=identity, AIRPLAYVIDEO_COMPANION_SOCKET=str(self.companion.path))
                self.chrome_process = await self.launch("google-chrome", "--no-first-run", "--no-default-browser-check", "--password-store=basic", "--disable-dev-shm-usage", "--autoplay-policy=no-user-gesture-required", "--user-data-dir=" + str(self.root / "profile"), f"--window-size={width},{height}", "--start-fullscreen", "about:blank")
                await self.companion.wait_ready()
                self.running = True
                self.url = ""
            except BaseException:
                await self._close()
                raise

    async def native_command(self, *args, text=None, timeout=10):
        process = await asyncio.create_subprocess_exec(*args, env=self.environment, user=1000, group=1000, stdin=asyncio.subprocess.PIPE if text is not None else asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            output, _ = await asyncio.wait_for(process.communicate(text.encode() if text is not None else None), timeout)
            check(process.returncode == 0, "The browser control could not finish")
            return output.decode().strip()
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

    async def paste_clipboard(self, text):
        # One bounded, foreground clipboard owner on our private X display.
        # Input stays on stdin and the selection disappears when it exits.
        process = await asyncio.create_subprocess_exec("python3", str(Path(__file__).with_name("paste.py")), env=self.environment, user=1000, group=1000, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            process.stdin.write(text.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            check(await asyncio.wait_for(process.stdout.readline(), 5) == b"ready\n", "Paste could not prepare the clipboard")
            await self.native_command("xdotool", "key", "--clearmodifiers", "ctrl+v")
            check(await asyncio.wait_for(process.stdout.readline(), 5) == b"served\n", "Paste did not finish; select a field and try again")
            # Clipboard ownership changes can invalidate Chrome's prefetched
            # text. Let its native paste event finish before clearing ownership.
            await asyncio.sleep(0.5)
        except asyncio.TimeoutError as exc:
            raise UserError("Paste did not finish; select a field and try again") from exc
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    async def paste(self, text):
        check(isinstance(text, str) and len(text) <= 16384, "Text is too long")
        check(not any(ord(char) < 32 or ord(char) == 127 for char in text), "Paste a single line without control characters")
        async with self.control_lock:
            # Guard all preview clients while native keyboard input is delivered.
            remote = ["x11vnc", "-display", self.environment["DISPLAY"], "-auth", self.environment["XAUTHORITY"]]
            try:
                guarded = await self.native_command(*remote, "-R", "viewonly", "-Q", "viewonly", "-sync")
                check("viewonly:1" in guarded, "Preview input could not be paused for Paste")
                window = await self.native_command("xdotool", "getactivewindow")
                check(window.isdigit() and self.chrome_process is not None, "Select a browser field before pasting")
                owner = await self.native_command("xdotool", "getwindowpid", window)
                check(owner == str(self.chrome_process.pid), "Select a browser field before pasting")
                await self.paste_clipboard(text)
            finally:
                with contextlib.suppress(UserError, OSError):
                    await self.native_command(*remote, "-R", "noviewonly", "-sync")

    def cancel_youtube(self):
        if self.youtube_task:
            self.youtube_task.cancel()
            self.youtube_task = None

    async def navigate(self, url, setup, youtube=False, watch_later=False):
        url = browser_url(url)
        await self.start(setup)
        self.cancel_youtube()
        async with self.control_lock:
            check(self.running and self.companion, "Open the browser first")
            await self.companion.call("navigate", url=url)
        self.url = url
        if youtube or watch_later:
            self.youtube_task = asyncio.create_task(self.prepare_youtube(setup["browser"]["youtube_quality"], watch_later))

    async def prepare_youtube(self, quality, watch_later):
        try:
            for _ in range(45):
                result = await self.companion.call("youtube_prepare", quality=quality, watch_later=watch_later)
                if result.get("ready"):
                    return
                await asyncio.sleep(1)
        except (UserError, asyncio.CancelledError):
            # The companion cannot inspect or operate Google account pages.
            return

    async def control(self, action, value=None):
        check(self.running, "Open the browser first")
        if action in {"back", "forward", "reload"}:
            self.cancel_youtube()
            async with self.control_lock:
                await self.companion.call(action)
        elif action in {"zoom_in", "zoom_out", "zoom_reset"}:
            async with self.control_lock:
                await self.native_command("xdotool", "key", "--clearmodifiers", {"zoom_in": "ctrl+plus", "zoom_out": "ctrl+minus", "zoom_reset": "ctrl+0"}[action])
        elif action == "paste":
            await self.paste(value)
        elif action in {"play_pause", "fullscreen"}:
            async with self.control_lock:
                result = await self.companion.call(action)
                if not result.get("ready") and action == "play_pause":
                    await self.native_command("xdotool", "key", "XF86AudioPlay")
                else:
                    check(result.get("ready"), "Use the video controls in the preview on this page")
        else:
            raise UserError("Unknown browser action")

    async def preview_connection(self):
        check(self.running and self.vnc_password, "Open the browser first")
        reader, writer = await asyncio.open_connection("127.0.0.1", self.vnc_port)
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
        if self.running and self.companion:
            with contextlib.suppress(UserError, OSError):
                await self.companion.call("close")
            if self.chrome_process and self.chrome_process.returncode is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.chrome_process.wait(), 4)
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
        if self.companion:
            await self.companion.close()
        self.url = ""
        self.vnc_password = None
        self.vnc_port = None
        self.companion = None
        self.chrome_process = None

    async def close(self):
        async with self.lock, self.control_lock:
            await self._close()
