"""Home Assistant's standard MQTT discovery; no Core files or custom card."""
from __future__ import annotations
import asyncio
import contextlib
import json
import os
import aiohttp
import paho.mqtt.client as mqtt
from .model import VERSION, UserError, check, atomic_json, private_json


def discovery_documents(store, channels):
    installation = store.data["installation_id"]
    base = "airplayvideo/" + installation
    documents = {}
    if not store.data["setup"]["complete"]:
        return documents

    def entity(component, receiver, key, name, **properties):
        device_id = "airplayvideo_" + installation + "_" + receiver["id"]
        unique = device_id + "_" + key
        properties.update(name=name, unique_id=unique, availability_topic=base + "/availability", device={"identifiers": [device_id], "name": receiver["name"] + " AirplayVideo", "manufacturer": "OrdinaryPlace", "model": "AirplayVideo", "sw_version": VERSION})
        documents[f"homeassistant/{component}/{device_id}/{key}/config"] = properties

    def command(action, receiver, **values):
        return json.dumps({"action": action, "receiver": receiver["id"], **values}, separators=(",", ":"))

    setup = store.data["setup"]
    for receiver in store.data["receivers"]:
        state = base + "/state/" + receiver["id"]
        common = {"command_topic": base + "/command"}
        entity("button", receiver, "stop", "Stop", icon="mdi:stop", payload_press=command("stop", receiver), **common)
        entity("sensor", receiver, "status", "Status", icon="mdi:cast-connected", state_topic=state, value_template="{{ value_json.connection }}")
        entity("sensor", receiver, "source", "Source", icon="mdi:play-box-outline", state_topic=state, value_template="{{ value_json.source }}")
        entity("sensor", receiver, "error", "Last error", entity_category="diagnostic", state_topic=state, value_template="{{ value_json.error }}")
        if setup["modes"].get("generated"):
            entity("button", receiver, "generated", "Play generated video", icon="mdi:card-text", payload_press=command("generated", receiver), **common)
        if setup["modes"]["browser"]:
            pages = store.data["pages"]
            for page in pages:
                entity("button", receiver, "page_" + page["id"], "Show " + page["name"], icon="mdi:web", payload_press=command("page", receiver, page=page["id"]), **common)
            if pages:
                template = '{"action":"select_page","receiver":' + json.dumps(receiver["id"]) + ',"page":{{ value | tojson }}}'
                entity("select", receiver, "page", "Page", options=[p["name"] for p in pages], state_topic=state, value_template="{{ value_json.selected_page }}", command_template=template, **common)
                entity("button", receiver, "play_page", "Play selected page", payload_press=command("play_page", receiver), icon="mdi:play", **common)
            template = '{"action":"youtube","receiver":' + json.dumps(receiver["id"]) + ',"url":{{ value | tojson }}}'
            entity("text", receiver, "youtube", "Play YouTube URL", icon="mdi:youtube", mode="text", min=1, max=255, command_template=template, **common)
            entity("button", receiver, "watch_later", "Play Watch Later", icon="mdi:playlist-play", payload_press=command("watch_later", receiver), **common)
        if setup["modes"]["hdhomerun"]:
            supported = [row for row in channels.public() if row["supported"]]
            if supported:
                template = '{"action":"select_channel","receiver":' + json.dumps(receiver["id"]) + ',"channel":{{ value | tojson }}}'
                entity("select", receiver, "channel", "Channel", options=[row["label"] for row in supported], state_topic=state, value_template="{{ value_json.selected_channel }}", command_template=template, **common)
                entity("button", receiver, "play_channel", "Play selected channel", icon="mdi:television-play", payload_press=command("play_channel", receiver), **common)
    return documents


async def supervisor_mqtt(session):
    token = os.environ.get("SUPERVISOR_TOKEN")
    if not token:
        return None
    try:
        async with session.get("http://supervisor/services/mqtt", headers={"Authorization": "Bearer " + token}, timeout=aiohttp.ClientTimeout(total=5)) as response:
            if response.status != 200:
                return None
            result = await response.json()
            data = result.get("data", {})
            return data if data.get("host") and data.get("username") and data.get("password") else None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return None


class HomeAssistant:
    def __init__(self, store, controller, channels, session):
        self.store, self.controller, self.channels, self.session = store, controller, channels, session
        self.client = None
        self.connected = False
        self.error = ""
        self.loop = asyncio.get_running_loop()
        self.base = "airplayvideo/" + store.data["installation_id"]
        self.path = store.root / "mqtt-discovery.json"
        self.previous = set(private_json(self.path)) if self.path.exists() else set()
        self.pending = set()
        self.credentials_task = None
        self.closing = False

    async def start(self):
        self.closing = False
        if not self.store.data["setup"]["home_assistant"]["enabled"]:
            return
        credentials = await supervisor_mqtt(self.session)
        if not credentials:
            self.error = "Enable Home Assistant's MQTT service to create playback entities. The app controls work without it."
            return
        self.error = ""
        client = mqtt.Client(client_id="airplayvideo_" + self.store.data["installation_id"], clean_session=True)
        client.username_pw_set(credentials["username"], credentials["password"])
        if credentials.get("ssl"):
            client.tls_set()
        client.will_set(self.base + "/availability", "offline", qos=1, retain=True)
        client.reconnect_delay_set(1, 30)
        client.on_connect = self.on_connect
        client.on_disconnect = lambda *_: self.loop.call_soon_threadsafe(self.offline)
        client.on_message = self.on_message
        self.client = client
        client.connect_async(credentials["host"], int(credentials.get("port", 1883)), 30)
        client.loop_start()

    def offline(self):
        self.connected = False

    def on_connect(self, client, _userdata, _flags, result):
        if result == 0:
            client.subscribe([(self.base + "/command", 0), ("homeassistant/status", 0)])
            self.loop.call_soon_threadsafe(self.online)

    def online(self):
        if self.closing or not self.client:
            return
        self.connected = True
        self.refresh()
        self.publish_state()
        self.client.publish(self.base + "/availability", "online", qos=1, retain=True)

    def on_message(self, _client, _userdata, message):
        if message.topic == "homeassistant/status" and message.payload == b"online":
            self.loop.call_soon_threadsafe(self.refresh)
            return
        if message.retain or message.topic != self.base + "/command" or len(message.payload) > 8192:
            return
        try:
            value = json.loads(message.payload)
            if not isinstance(value, dict):
                return
        except (ValueError, UnicodeError):
            return
        self.loop.call_soon_threadsafe(self.schedule, value)

    def schedule(self, value):
        if self.closing:
            return
        task = asyncio.create_task(self.command(value))
        self.pending.add(task)
        task.add_done_callback(self.pending.discard)

    async def command(self, value):
        try:
            if value.get("action") == "generated":
                receivers = value.get("receivers", [value.get("receiver")])
                # Explicit targets: an automation never adds an unrelated active TV.
                await self.controller.play({"mode": "generated", "receivers": receivers,
                                            "generated": value.get("generated", {})})
                self.publish_state()
                return
            receiver = self.store.receiver(value.get("receiver"))
            receiver_id = receiver["id"]
            action = value.get("action")
            if action == "stop":
                await self.controller.stop(receiver_id)
            elif action == "select_page":
                self.controller.require_mode("browser")
                page = next((p for p in self.store.data["pages"] if p["name"] == value.get("page")), None)
                check(page is not None, "Saved page was not found")
                self.store.data["selected_pages"][receiver_id] = page["id"]
                self.store.save()
            elif action == "select_channel":
                self.controller.require_mode("hdhomerun")
                row = next((r for r in self.channels.rows if r["label"] == value.get("channel")), None)
                check(row and row["supported"], "Choose a supported channel")
                self.store.data["selected_channels"][receiver_id] = row["id"]
                self.store.save()
            elif action in {"channel", "play_channel"}:
                channel = value.get("channel") if action == "channel" else self.store.data["selected_channels"].get(receiver_id)
                await self.controller.play({"mode": "hdhomerun", "channel": channel, "receivers": [receiver_id]}, add=True)
            elif action in {"page", "play_page"}:
                page = value.get("page") if action == "page" else self.store.data["selected_pages"].get(receiver_id)
                await self.controller.play({"mode": "browser", "browser_source": "page", "page": page, "receivers": [receiver_id]}, add=True)
            elif action in {"youtube", "watch_later"}:
                await self.controller.play({"mode": "browser", "browser_source": action, "url": value.get("url"), "receivers": [receiver_id]}, add=True)
            else:
                raise UserError("Unknown Home Assistant playback command")
        except UserError as exc:
            self.controller.error = str(exc)
        except Exception:
            self.controller.error = "Home Assistant command failed; review the app controls"
        self.publish_state()

    def refresh(self):
        if not self.connected:
            return
        documents = discovery_documents(self.store, self.channels)
        for topic in self.previous - documents.keys():
            if topic.startswith("homeassistant/") and ("/airplayvideo_" + self.store.data["installation_id"] + "_") in topic:
                self.client.publish(topic, "", qos=1, retain=True)
        for topic, payload in documents.items():
            self.client.publish(topic, json.dumps(payload), qos=1, retain=True)
        self.previous = set(documents)
        atomic_json(self.path, sorted(self.previous))
        self.publish_state()

    def publish_state(self):
        if not self.connected:
            return
        runtime = self.controller.status()
        pages = {page["id"]: page["name"] for page in self.store.data["pages"]}
        channels = {row["id"]: row["label"] for row in self.channels.rows}
        for receiver in self.store.data["receivers"]:
            receiver_id = receiver["id"]
            detail = runtime["receivers"].get(receiver_id, {})
            state = {"connection": detail.get("state", "idle"), "source": runtime["source"]["label"] if receiver_id in runtime["targets"] and runtime["source"] else "None", "error": detail.get("message", runtime["error"])[:255], "selected_page": pages.get(self.store.data["selected_pages"].get(receiver_id), next(iter(pages.values()), "")), "selected_channel": channels.get(self.store.data["selected_channels"].get(receiver_id), "")}
            self.client.publish(self.base + "/state/" + receiver_id, json.dumps(state), qos=1, retain=True)

    async def close(self):
        self.closing = True
        client, self.client = self.client, None
        connected, self.connected = self.connected, False
        if client:
            if connected:
                delivery = client.publish(self.base + "/availability", "offline", qos=1, retain=True)
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(delivery.wait_for_publish, 3)
            client.disconnect()
            await asyncio.to_thread(client.loop_stop)
        self.connected = False
        if self.pending:
            for task in self.pending:
                task.cancel()
            await asyncio.gather(*self.pending, return_exceptions=True)
