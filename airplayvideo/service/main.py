"""Authenticated ingress application and setup wizard API."""
from __future__ import annotations
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import secrets
import time
import uuid
import aiohttp
from aiohttp import web
from .model import Store, VERSION, UserError, check, identifier, local_address, validate_setup, patch_setup, atomic_json
from .browser import Browser
from .controller import PairingEngine, Controller
from .hdhomerun import Channels, inspect as inspect_tuner, discover as discover_tuners
from .mqtt import HomeAssistant
from .network import ingress_listener
from .recordings import Recordings
from .diagnostics import Diagnostics

ROOT = Path(os.environ.get("AIRPLAYVIDEO_DATA", "/data"))
WEB = Path(os.environ.get("AIRPLAYVIDEO_WEB", "/opt/airplayvideo/web"))


@web.middleware
async def boundary(request, handler):
    standalone = request.app["standalone"]
    if not standalone and request.remote != "172.30.32.2":
        raise web.HTTPForbidden(text="Open AirplayVideo through Home Assistant")
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        if request.headers.get("X-AirplayVideo") != "1" or request.content_type != "application/json":
            raise web.HTTPForbidden(text="Use the authenticated application controls")
    try:
        response = await handler(request)
    except UserError as exc:
        response = web.json_response({"error": str(exc)}, status=409)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        response = web.json_response({"error": "Invalid or incomplete request"}, status=400)
    except web.HTTPException:
        raise
    except Exception as exc:
        # Exception class only. No request body, URL, credential, or traceback.
        logging.error("Application operation failed (%s)", type(exc).__name__)
        response = web.json_response({"error": "The operation could not finish; your saved configuration was preserved"}, status=500)
    if not response.prepared:
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"})
    return response


class Application:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.pairing = PairingEngine(store.root, session)
        self.browser = Browser(store.root, session)
        self.channels = Channels(store, session)
        self.controller = Controller(store, self.pairing, self.browser, self.channels)
        self.recordings = Recordings(self.controller)
        self.controller.recordings = self.recordings
        self.diagnostics = Diagnostics(self.controller, session)
        self.controller.diagnostics = self.diagnostics
        self.ha = HomeAssistant(store, self.controller, self.channels, session)
        self.controller.changed = self.ha.publish_state
        self.tickets = {}
        self.preview_sockets = set()
        self.configuration_lock = asyncio.Lock()

    def state(self):
        return {"version": VERSION, **self.store.public(), "runtime": self.controller.status(), "recordings": self.recordings.state(), "diagnostics": self.diagnostics.state(), "channels": self.channels.public(), "channel_error": self.channels.error, "home_assistant": {"connected": self.ha.connected, "error": self.ha.error, "command_topic": self.ha.base + "/command"}, "capabilities": self.controller.capabilities}

    def require_idle(self):
        self.controller.require_no_diagnostic()
        check(not self.recordings.current, "Wait for the diagnostic recording to finish")
        check(not self.controller.stream and not self.controller.pending, "Stop playback before changing Setup")

    async def start(self):
        await self.pairing.start()
        self.controller.capabilities = await self.pairing.call("/capabilities")
        saved = await self.pairing.call("/receivers")
        existing = {r["id"] for r in self.store.data["receivers"]}
        slots = {r["slot"] for r in self.store.data["receivers"]}
        for receiver in saved:
            if receiver["id"] not in existing:
                check(len(slots) < 8, "The saved pairing limit was reached; existing data was preserved")
                slot = next(i for i in range(8) if i not in slots)
                slots.add(slot)
                self.store.data["receivers"].append({**receiver, "slot": slot})
        if len(saved) != len(existing):
            self.store.save()
        if self.store.data["setup"]["modes"]["hdhomerun"]:
            await self.channels.refresh()
        await self.ha.start()

    async def close(self):
        for ws in list(self.preview_sockets):
            await ws.close()
        await self.ha.close()
        await self.diagnostics.close()
        await self.recordings.close()
        await self.controller.close()
        await self.pairing.close()

    async def get_state(self, _request):
        return web.json_response(self.state())

    async def action(self, request):
        data = await request.json()
        action = request.match_info["action"]
        controller = self.controller
        if action == "measure_sync":
            await self.diagnostics.start(data)
        elif action == "cancel_sync":
            await self.diagnostics.close()
        elif action == "play":
            check(not self.recordings.current or not self.recordings.owned, "Wait for the diagnostic recording to finish")
            await controller.play(data)
        elif action == "record":
            await self.recordings.start(data)
        elif action == "remove_recording":
            self.recordings.remove(data.get("id"))
        elif action == "preview_generated":
            from .model import validate_generated
            settings = validate_generated(data.get("generated", {}), self.store.data["setup"]["generated"])
            return web.json_response(await self.pairing.call("/generated/preview", settings))
        elif action == "stop":
            await controller.stop(data.get("receiver"))
        elif action == "open_browser":
            await controller.open_browser(data)
        elif action == "close_browser":
            check(not self.recordings.current, "Wait for the diagnostic recording to finish")
            await controller.close_browser()
        elif action == "browser":
            await self.browser.control(data.get("action"), data.get("text"))
        elif action == "refresh_channels":
            controller.require_mode("hdhomerun")
            await self.channels.refresh()
            self.ha.refresh()
        elif action == "favorite":
            row = self.channels.get(data.get("channel"))
            favorites = set(self.store.data["favorites"])
            if row["id"] in favorites:
                favorites.remove(row["id"])
            else:
                favorites.add(row["id"])
            self.store.data["favorites"] = sorted(favorites)
            self.store.save()
        else:
            raise UserError("Unknown playback action")
        return web.json_response(self.state())

    async def recording_file(self, request):
        extension = request.match_info['extension']
        path = self.recordings.file(request.match_info['id'], extension)
        return web.FileResponse(path, headers={
            'Content-Disposition': 'attachment; filename="airplayvideo-capture.' + extension + '"',
        })

    async def sync_report(self, _request):
        check(self.diagnostics.report is not None, "Run a sync measurement first")
        return web.json_response(self.diagnostics.report, headers={
            'Content-Disposition': 'attachment; filename="airplayvideo-sync-report.json"'})

    async def save_page(self, request):
        self.controller.require_mode("browser")
        page = self.store.save_page(await request.json())
        for receiver in self.store.data["receivers"]:
            self.store.data["selected_pages"].setdefault(receiver["id"], page["id"])
        self.store.save()
        self.ha.refresh()
        return web.json_response(self.state())

    async def remove_page(self, request):
        data = await request.json()
        page = self.store.page(data.get("id"))
        # Removing a shortcut never deletes browser profile or site data.
        archived = self.store.data.setdefault("archived_pages", [])
        archived.append(page)
        self.store.data["pages"] = [p for p in self.store.data["pages"] if p["id"] != page["id"]]
        for receiver_id, page_id in list(self.store.data["selected_pages"].items()):
            if page_id == page["id"]:
                self.store.data["selected_pages"].pop(receiver_id)
        self.store.save()
        self.ha.refresh()
        return web.json_response(self.state())

    async def setup(self, request):
        async with self.configuration_lock, self.controller.lock:
            self.require_idle()
            data = await request.json()
            action = request.match_info["action"]
            if action in {"save", "update"}:
                previous = self.store.data["setup"]
                settings = (patch_setup(previous, data.get("changes"), data.get("expected"))
                            if action == "update" else validate_setup(data))
                encoder = settings["video"]["encoder"]
                check(encoder == "auto" or encoder in self.controller.capabilities["encoders"], "Choose an encoder available on this host")
                for tuner in settings["hdhomerun"]["devices"]:
                    if not any(t["id"] == tuner["id"] and t["address"] == tuner["address"] for t in previous["hdhomerun"]["devices"]):
                        verified = await inspect_tuner(self.session, tuner["address"])
                        check(verified["id"] == tuner["id"], "A tuner identity changed; find the device again")
                self.require_idle()
                if (previous["video"]["resolution"] != settings["video"]["resolution"] or
                        previous["modes"]["browser"] and not settings["modes"]["browser"]):
                    await self.browser.close()
                self.store.setup(settings)
                if settings["modes"]["browser"] and not self.store.data["pages"]:
                    page = self.store.save_page({"name": "Home", "url": settings["browser"]["home_url"]})
                    for receiver in self.store.data["receivers"]:
                        self.store.data["selected_pages"][receiver["id"]] = page["id"]
                    self.store.save()
                if (not previous["complete"] or previous["hdhomerun"] != settings["hdhomerun"] or
                        previous["modes"]["hdhomerun"] != settings["modes"]["hdhomerun"]):
                    await self.channels.refresh()
                for receiver in self.store.data["receivers"]:
                    first = next((r for r in self.channels.rows if r["supported"]), None)
                    if first:
                        self.store.data["selected_channels"].setdefault(receiver["id"], first["id"])
                self.store.save()
                if not previous["complete"] or previous["home_assistant"] != settings["home_assistant"]:
                    await self.ha.close()
                    await self.ha.start()
                self.ha.refresh()
            elif action == "open_browser":
                # First-run sign-in needs no tuner, receiver or saved setup.
                # Reopening the preview must preserve an in-progress login.
                if not self.browser.running:
                    await self.browser.navigate(data.get("url"), self.store.data["setup"])
            elif action == "discover_tuners":
                return web.json_response({"devices": await discover_tuners(self.session)})
            elif action == "probe_tuner":
                return web.json_response({"device": await inspect_tuner(self.session, data.get("address"))})
            elif action == "discover_tvs":
                return web.json_response({"receivers": await self.pairing.call("/discover")})
            elif action == "probe_tv":
                result = await self.pairing.call("/probe", {"address": local_address(data.get("address")), "port": data.get("port", 7000)})
                return web.json_response({"receiver": result})
            elif action == "pair_start":
                check(len(self.store.data["receivers"]) < 8, "This release supports eight saved TVs")
                receiver = data.get("receiver", {})
                address = local_address(receiver.get("address"))
                check(not any(r["device_id"] == receiver.get("device_id") for r in self.store.data["receivers"]), "This TV is already paired")
                result = await self.pairing.call("/pair/start", {"id": uuid.uuid4().hex, "address": address, "port": receiver.get("port", 7000), "name": receiver.get("name", "")})
                return web.json_response(result)
            elif action == "pair_finish":
                pin = data.pop("pin", "")
                check(isinstance(pin, str) and len(pin) == 4 and pin.isascii() and pin.isdigit(), "Enter the four-digit code shown on the TV")
                receiver = await self.pairing.call("/pair/finish", {"pin": pin})
                pin = None
                slots = {r["slot"] for r in self.store.data["receivers"]}
                receiver["slot"] = next(i for i in range(8) if i not in slots)
                self.store.data["receivers"].append(receiver)
                if self.store.data["pages"]:
                    self.store.data["selected_pages"][receiver["id"]] = self.store.data["pages"][0]["id"]
                first = next((r for r in self.channels.rows if r["supported"]), None)
                if first:
                    self.store.data["selected_channels"][receiver["id"]] = first["id"]
                self.store.save()
                self.ha.refresh()
            elif action == "pair_cancel":
                await self.pairing.call("/pair/cancel")
            elif action == "home_assistant":
                await self.ha.close()
                await self.ha.start()
            else:
                raise UserError("Unknown setup action")
            return web.json_response(self.state())

    async def preview_ticket(self, request):
        check(self.browser.running, "Open the browser first")
        now = time.monotonic()
        self.tickets = {key: deadline for key, deadline in self.tickets.items() if deadline > now}
        check(len(self.tickets) < 32, "Too many preview requests")
        ticket = secrets.token_urlsafe(24)
        self.tickets[ticket] = now + 30
        return web.json_response({"ticket": ticket})

    async def preview(self, request):
        deadline = self.tickets.pop(request.query.get("ticket", ""), 0)
        check(deadline > time.monotonic(), "Preview link expired; reopen the preview")
        try:
            async with asyncio.timeout(5):
                reader, writer = await self.browser.preview_connection()
        except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError) as exc:
            raise UserError("Browser preview did not respond; close and reopen the browser") from exc
        ws = web.WebSocketResponse(max_msg_size=1024 * 1024, heartbeat=30)
        await ws.prepare(request)
        self.preview_sockets.add(ws)
        buffer = bytearray()

        async def receive_bytes(count):
            while len(buffer) < count:
                message = await ws.receive(timeout=10)
                check(message.type == aiohttp.WSMsgType.BINARY, "Preview connection ended")
                buffer.extend(message.data)
                check(len(buffer) <= 65536, "Invalid preview handshake")
            result = bytes(buffer[:count])
            del buffer[:count]
            return result

        async def toward_browser():
            if buffer:
                writer.write(buffer)
                await writer.drain()
                buffer.clear()
            async for message in ws:
                if message.type != aiohttp.WSMsgType.BINARY:
                    break
                writer.write(message.data)
                await writer.drain()

        async def toward_ui():
            while data := await reader.read(65536):
                await ws.send_bytes(data)

        tasks = []
        try:
            await ws.send_bytes(b"RFB 003.008\n")
            check(await receive_bytes(12) == b"RFB 003.008\n", "Invalid preview version")
            await ws.send_bytes(b"\x01\x01")
            check(await receive_bytes(1) == b"\x01", "Invalid preview authentication choice")
            await ws.send_bytes(b"\0\0\0\0")
            tasks = [asyncio.create_task(toward_browser()), asyncio.create_task(toward_ui())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (UserError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            await ws.close()
            self.preview_sockets.discard(ws)
        return ws


def make_app(application, standalone=False, web_root=WEB):
    app = web.Application(middlewares=[boundary], client_max_size=64 * 1024)
    app["standalone"] = standalone
    app.router.add_get("/api/state", application.get_state)
    app.router.add_get("/api/sync-report", application.sync_report)
    app.router.add_get("/api/recordings/{id}/{extension}", application.recording_file)
    app.router.add_post("/api/actions/{action}", application.action)
    app.router.add_post("/api/setup/{action}", application.setup)
    app.router.add_post("/api/pages/save", application.save_page)
    app.router.add_post("/api/pages/remove", application.remove_page)
    app.router.add_get("/api/preview-ticket", application.preview_ticket)
    app.router.add_get("/preview", application.preview)
    app.router.add_get("/", lambda _request: web.FileResponse(web_root / "index.html"))
    app.router.add_static("/assets/", web_root, show_index=False)
    if Path("/usr/share/novnc").is_dir():
        app.router.add_static("/novnc/", "/usr/share/novnc", show_index=False)
    return app


async def serve():
    session = aiohttp.ClientSession()
    application = Application(Store(ROOT), session)
    try:
        await application.start()
        app = make_app(application, os.environ.get("AIRPLAYVIDEO_STANDALONE") == "1")
        async def cleanup(_app):
            await application.close()
            await session.close()
        app.on_cleanup.append(cleanup)
        return app
    except BaseException:
        await application.close()
        await session.close()
        raise


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    host, port = asyncio.run(ingress_listener())
    web.run_app(serve(), host=host, port=port, access_log=None, print=lambda _: print("AirplayVideo ready; playback starts only from an explicit user command", flush=True), shutdown_timeout=30)
