"""LAN discovery and lineup metadata. Reading a lineup never reserves a tuner."""
import asyncio
import json
import re
import socket
import struct
from urllib.parse import urlsplit
import zlib
import aiohttp
from .model import check, local_address, UserError


async def fetch_json(session, url, maximum=2 * 1024 * 1024):
    try:
        async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=8)) as response:
            check(response.status == 200, "The tuner did not answer; check its address and connection")
            data = bytearray()
            async for chunk in response.content.iter_chunked(32768):
                data.extend(chunk)
                check(len(data) <= maximum, "The tuner returned too much data")
            return json.loads(data)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        raise UserError("Could not read this HDHomeRun; check its address and connection") from exc


async def inspect(session, address):
    address = local_address(address)
    info = await fetch_json(session, f"http://{address}/discover.json", 65536)
    check(isinstance(info, dict), "Unexpected HDHomeRun response")
    device_id = str(info.get("DeviceID", "")).upper()
    check(re.fullmatch(r"[0-9A-F]{8}", device_id), "This address did not identify an HDHomeRun")
    name = str(info.get("FriendlyName") or info.get("ModelNumber") or "HDHomeRun")[:100]
    # DeviceAuth and server-provided URLs are deliberately discarded.
    return {"id": device_id, "name": name, "address": address}


def discover_addresses():
    body = bytes([1, 4]) + struct.pack("!I", 1) + bytes([2, 4]) + struct.pack("!I", 0xFFFFFFFF)
    packet = struct.pack("!HH", 2, len(body)) + body
    packet += struct.pack("<I", zlib.crc32(packet))
    addresses = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.settimeout(0.3)
        sock.sendto(packet, ("255.255.255.255", 65001))
        import time
        until = time.monotonic() + 2
        while time.monotonic() < until:
            try:
                data, sender = sock.recvfrom(4096)
                if len(data) >= 8 and data[:2] == b"\x00\x03" and zlib.crc32(data[:-4]) == struct.unpack("<I", data[-4:])[0]:
                    addresses.add(local_address(sender[0]))
            except (socket.timeout, UserError):
                continue
    return sorted(addresses)


async def discover(session):
    try:
        addresses = await asyncio.to_thread(discover_addresses)
    except OSError:
        addresses = []
    results = await asyncio.gather(*(inspect(session, ip) for ip in addresses[:8]), return_exceptions=True)
    return [r for r in results if isinstance(r, dict)]


def parse_lineup(device, rows):
    check(isinstance(rows, list) and len(rows) <= 5000, "Invalid channel lineup")
    channels = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        number = str(row.get("GuideNumber", ""))
        if not re.fullmatch(r"\d{1,4}(?:\.\d{1,4})?", number):
            continue
        name = str(row.get("GuideName", "Channel"))[:80]
        video = str(row.get("VideoCodec", "")).lower()
        audio = str(row.get("AudioCodec", "")).lower()
        reason = ""
        if row.get("DRM") not in (None, False, 0, "0", "false"):
            reason = "Protected channel"
        elif video and video not in {"mpeg2", "mpeg2video", "h264", "avc"}:
            reason = "Unsupported video format"
        elif audio and audio not in {"aac", "ac3", "mp2", "mp3", "mpeg"}:
            reason = "Unsupported audio format"
        try:
            parsed = urlsplit(str(row.get("URL", "")))
            valid = parsed.scheme == "http" and parsed.hostname == device["address"] and parsed.port == 5004 and parsed.path == "/auto/v" + number and not parsed.query and not parsed.fragment and parsed.username is None
        except ValueError:
            valid = False
        if not valid:
            reason = "Invalid tuner stream address"
        channels.append({"id": device["id"] + ":" + number, "device_id": device["id"], "number": number, "name": name, "label": f"{number} {name}", "supported": not reason, "reason": reason, "_url": f"http://{device['address']}:5004/auto/v{number}"})
    return channels


class Channels:
    def __init__(self, store, session):
        self.store, self.session = store, session
        self.rows = []
        self.error = ""

    async def refresh(self):
        found = []
        errors = []
        for device in self.store.data["setup"]["hdhomerun"]["devices"]:
            try:
                verified = await inspect(self.session, device["address"])
                check(verified["id"] == device["id"], "The device at this address changed; review Setup")
                rows = await fetch_json(self.session, f"http://{device['address']}/lineup.json")
                parsed = parse_lineup(device, rows)
                if len(self.store.data["setup"]["hdhomerun"]["devices"]) > 1:
                    for channel in parsed:
                        channel["label"] += " · " + device["id"]
                found.extend(parsed)
            except UserError as exc:
                errors.append(str(exc))
        self.error = "; ".join(errors)
        self.rows = found
        return self.public()

    def public(self):
        return [{k: v for k, v in row.items() if not k.startswith("_")} for row in self.rows]

    def get(self, channel_id):
        row = next((r for r in self.rows if r["id"] == channel_id), None)
        check(row is not None, "Channel is not in the current lineup; refresh channels")
        check(row["supported"], row["reason"])
        return row
