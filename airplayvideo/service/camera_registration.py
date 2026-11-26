"""Register one native HA streaming camera through the supported Generic flow.

MQTT cameras accept still pictures only. Generic IP Camera supplies a real
CameraEntity with STREAM support and a device, so HA's media source/HLS helpers
can consume the selected source without a custom integration or Core file edits.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re

import aiohttp

from .model import atomic_json, private_json


TITLE = "AirplayVideo selected video"
ENTITY_ID = "camera.airplayvideo_selected_video"
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_RESPONSE_LIMIT = 2 * 1024 * 1024


class RegistrationError(Exception):
    """A deliberately bounded error safe for the authenticated app status."""


class CameraRegistration:
    def __init__(self, store, session, *, core_url="http://supervisor/core"):
        self.store, self.session = store, session
        self.core_url = core_url.rstrip("/")
        self.path = store.root / "camera-registration.json"
        self.saved = private_json(self.path) if self.path.exists() else {}
        self.lock = asyncio.Lock()
        self.status, self.error, self.entity_id = "pending", "", self.saved.get("entity_id", "")

    def state(self):
        # No private endpoint, bearer, flow result, or exception text is exposed.
        return {"status": self.status, "entity_id": self.entity_id, "error": self.error}

    def save(self, **changes):
        self.saved.update(changes)
        atomic_json(self.path, self.saved)

    async def request(self, method, path, token, value=None):
        arguments = {"json": value} if value is not None else {}
        async with self.session.request(
            method, self.core_url + path,
            headers={"Authorization": "Bearer " + token},
            timeout=aiohttp.ClientTimeout(total=90), **arguments,
        ) as response:
            if response.status in {401, 403}:
                raise RegistrationError("Home Assistant camera setup needs the app's Home Assistant API permission.")
            if response.status >= 400:
                raise RegistrationError("Home Assistant could not finish camera setup. Try again while video is playing.")
            # A StreamReader.read(n) can return as soon as one network chunk
            # arrives. Parse only after EOF, retaining the request's total
            # timeout and checking the byte limit before buffering each chunk.
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                if len(data) + len(chunk) > _RESPONSE_LIMIT:
                    raise RegistrationError("Home Assistant returned an oversized camera setup response.")
                data.extend(chunk)
            return json.loads(data)

    @staticmethod
    def flow_id(value, step):
        if not isinstance(value, dict):
            raise RegistrationError("Home Assistant returned an invalid camera setup response.")
        flow_id = value.get("flow_id")
        if (value.get("type") != "form" or value.get("step_id") != step
                or value.get("errors") or not isinstance(flow_id, str) or not _ID.fullmatch(flow_id)):
            raise RegistrationError("Home Assistant could not validate the camera. Try again while video is playing.")
        return flow_id

    async def configure(self, token, stream_url, snapshot_url, entry_id):
        options = bool(entry_id)
        prefix = "/api/config/config_entries/" + ("options/flow" if options else "flow")
        start = await self.request("POST", prefix, token,
                                   {"handler": entry_id if options else "generic"})
        flow_id = self.flow_id(start, "init" if options else "user")
        try:
            result = await self.request("POST", prefix + "/" + flow_id, token, {
                "stream_source": stream_url, "still_image_url": snapshot_url,
                "advanced": {"framerate": 2, "verify_ssl": True,
                             "rtsp_transport": "tcp", "authentication": "basic"},
            })
            self.flow_id(result, "user_confirm")
            # If confirmation is accepted but its response is lost, never blindly
            # create a second device. Save the uncertainty before the request.
            if not options:
                self.save(uncertain_creation=True)
            result = await self.request("POST", prefix + "/" + flow_id, token, {"confirmed_ok": True})
            if not isinstance(result, dict) or result.get("type") != "create_entry":
                raise RegistrationError("Home Assistant did not finish camera setup.")
            if not options:
                entry_id = result.get("result", {}).get("entry_id")
                if not isinstance(entry_id, str) or not _ID.fullmatch(entry_id):
                    raise RegistrationError("Home Assistant did not identify the new camera device.")
                self.save(entry_id=entry_id, uncertain_creation=False)
            return entry_id
        finally:
            # Config flows own temporary stream previews. Aborting an unfinished
            # flow releases them; a completed flow simply returns not found.
            with contextlib.suppress(Exception):
                await self.request("DELETE", prefix + "/" + flow_id, token)

    async def registry(self, token, entry_id):
        async with self.session.ws_connect(
            self.core_url + "/api/websocket", timeout=10,
            max_msg_size=8 * 1024 * 1024,
        ) as ws:
            async def receive():
                value = await asyncio.wait_for(ws.receive_json(), 15)
                if not isinstance(value, dict):
                    raise RegistrationError("Home Assistant returned an invalid camera registry response.")
                return value

            if (await receive()).get("type") != "auth_required":
                raise RegistrationError("Home Assistant camera naming is unavailable.")
            await ws.send_json({"type": "auth", "access_token": token})
            if (await receive()).get("type") != "auth_ok":
                raise RegistrationError("Home Assistant camera naming needs API permission.")
            sequence = 0

            async def call(command, **values):
                nonlocal sequence
                sequence += 1
                await ws.send_json({"id": sequence, "type": command, **values})
                result = await receive()
                if result.get("id") != sequence or result.get("type") != "result" or result.get("success") is not True:
                    raise RegistrationError("Home Assistant could not name the camera. Check its device settings.")
                return result.get("result")

            entity = None
            for attempt in range(10):
                entities = await call("config/entity_registry/list")
                if not isinstance(entities, list) or not all(isinstance(item, dict) for item in entities):
                    raise RegistrationError("Home Assistant returned an invalid camera registry response.")
                entity = next((item for item in entities if item.get("config_entry_id") == entry_id
                               and item.get("entity_id", "").startswith("camera.")), None)
                if entity:
                    break
                await asyncio.sleep(0.5)
            if entity is None:
                raise RegistrationError("The camera device is configured; Home Assistant is still loading its entity.")
            current = entity["entity_id"]
            if entity.get("disabled_by"):
                # HA also marks the entity disabled when its device is disabled.
                # Preserve that owner choice, including an unfinished first
                # naming pass, without issuing any registry update command.
                return current, True
            # Preserve names/IDs customized after initial registration.
            if not self.saved.get("named"):
                await call("config_entries/update", entry_id=entry_id, title=TITLE)
                # HA composes the device and entity name. An empty entity part
                # keeps the virtual device's title visible exactly once.
                changes = {"entity_id": current, "name": ""}
                if not any(item.get("entity_id") == ENTITY_ID for item in entities):
                    changes["new_entity_id"] = ENTITY_ID
                result = await call("config/entity_registry/update", **changes)
                current = result.get("entity_entry", {}).get("entity_id", changes.get("new_entity_id", current))
                if entity.get("device_id"):
                    await call("config/device_registry/update", device_id=entity["device_id"], name_by_user=TITLE)
                self.save(named=True, naming_version=1)
            elif "naming_version" not in self.saved:
                # 0.2.30 originally assigned TITLE to both name parts. Change
                # only that known automatic entity name; keep owner names,
                # device names, and entity IDs. Persist after acknowledgment.
                # If the reply is lost, the next read sees the empty part and
                # completes the marker without sending another update.
                if entity.get("name") == TITLE:
                    await call("config/entity_registry/update", entity_id=current, name="")
                self.save(naming_version=1)
            return current, False

    async def ensure(self, stream_url, snapshot_url):
        """Call once both private endpoints are ready; repeat safely on retries.

        This validates/creates the native camera, never starts a TV, changes a
        camera recording policy, or creates MQTT's snapshot-only camera type.
        """
        async with self.lock:
            token = os.environ.get("SUPERVISOR_TOKEN")
            if not token:
                self.status, self.error = "unavailable", "Native camera setup is available in the Home Assistant app."
                return self.state()
            if not self.store.data["setup"]["home_assistant"]["enabled"]:
                self.status, self.error = "disabled", ""
                return self.state()
            try:
                if self.saved.get("uncertain_creation"):
                    raise RegistrationError("Camera creation needs review in Home Assistant before retrying, to avoid a duplicate device.")
                digest = hashlib.sha256((stream_url + "\n" + snapshot_url).encode()).hexdigest()
                entry_id = self.saved.get("entry_id", "")
                if not isinstance(entry_id, str) or (entry_id and not _ID.fullmatch(entry_id)):
                    raise RegistrationError("Saved camera identity is invalid; configuration was preserved.")
                entries = await self.request("GET", "/api/config/config_entries/entry", token)
                if not isinstance(entries, list) or not all(isinstance(item, dict) for item in entries):
                    raise RegistrationError("Home Assistant returned an invalid camera registry response.")
                entry = next((item for item in entries if item.get("entry_id") == entry_id), None)
                if entry is not None and entry.get("domain") != "generic":
                    raise RegistrationError("Saved camera identity no longer belongs to a Generic IP Camera; configuration was preserved.")
                if entry is not None and entry.get("disabled_by"):
                    self.status, self.error = "disabled", "The camera device is disabled in Home Assistant."
                    return self.state()
                if entry is None:
                    entry_id = ""
                    self.save(named=False)
                if not entry_id or self.saved.get("endpoint_digest") != digest:
                    entry_id = await self.configure(token, stream_url, snapshot_url, entry_id)
                    self.save(endpoint_digest=digest)
                self.entity_id, disabled = await self.registry(token, entry_id)
                self.save(entity_id=self.entity_id)
                self.status, self.error = (("disabled", "The camera entity or device is disabled in Home Assistant.")
                                           if disabled else ("ready", ""))
            except RegistrationError as exc:
                self.status, self.error = "pending", str(exc)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError, KeyError):
                self.status, self.error = "pending", "Home Assistant camera setup could not finish. Check the connection and retry while video is playing."
            return self.state()

    async def close(self):
        # No global camera/stream stop: active HA viewers own their output.
        pass
