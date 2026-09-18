"""Private configuration, strict setup validation, and stable HA identities."""
from __future__ import annotations
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit, urlunsplit, parse_qs, urlencode
import uuid

VERSION = "0.1.1"
IDENTIFIER = re.compile(r"[a-f0-9]{32}\Z")


class UserError(Exception):
    """A deliberately safe, user-facing message."""


def check(condition, message):
    if not condition:
        raise UserError(message)


def identifier(value):
    check(isinstance(value, str) and IDENTIFIER.fullmatch(value), "Invalid identifier")
    return value


def text(value, label, maximum=100):
    check(isinstance(value, str) and 0 < len(value.strip()) <= maximum,
          f"Enter a valid {label}")
    check(not any(ord(c) < 32 for c in value), f"Invalid {label}")
    return value.strip()


def browser_url(value):
    value = text(value, "page URL", 4096)
    try:
        parsed = urlsplit(value)
        check(parsed.scheme in {"http", "https"} and parsed.hostname and
              parsed.username is None and parsed.password is None,
              "Use an http or https page URL without a username or password")
        check(parsed.port is None or 1 <= parsed.port <= 65535, "Invalid page port")
    except ValueError as exc:
        raise UserError("Invalid page URL") from exc
    return value


def youtube_url(value):
    parsed = urlsplit(browser_url(value))
    host = (parsed.hostname or "").lower()
    query = parse_qs(parsed.query)
    parts = parsed.path.strip("/").split("/")
    if host == "youtu.be":
        video = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        video = query.get("v", [""])[0] if parsed.path == "/watch" else parts[1] if len(parts) == 2 and parts[0] in {"shorts", "live", "embed"} else ""
    else:
        video = ""
    check(re.fullmatch(r"[A-Za-z0-9_-]{11}", video), "Enter a link to one YouTube video")
    output = {"v": video}
    start = query.get("t", query.get("start", [""]))[0]
    if re.fullmatch(r"\d{1,7}s?", start):
        output["t"] = start.rstrip("s")
    return "https://www.youtube.com/watch?" + urlencode(output)


def local_address(value):
    check(isinstance(value, str), "Enter a local IPv4 address")
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError as exc:
        raise UserError("Enter the device's local IPv4 address") from exc
    ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
    check(any(address in ipaddress.ip_network(net) for net in ranges),
          "Use a private LAN address for this device")
    return str(address)


def private_json(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        check(stat.S_ISREG(info.st_mode) and info.st_mode & 0o077 == 0,
              "Private configuration permissions need attention; data was preserved")
        check(info.st_size < 2 * 1024 * 1024, "Configuration is too large")
        with os.fdopen(fd, "r", closefd=False) as stream:
            return json.load(stream)
    finally:
        os.close(fd)


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise UserError("Refusing to replace a configuration symlink")
    fd, temporary = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def default_setup():
    return {
        "complete": False,
        "modes": {"browser": True, "hdhomerun": False},
        "browser": {"home_url": "https://www.youtube.com/", "youtube_quality": "1080p"},
        "hdhomerun": {"devices": []},
        "video": {"resolution": "1080p", "fps": 30, "codec": "h264", "encoder": "auto", "bitrate_mbps": 8, "deinterlace": True},
        "audio": {"enabled": True, "latency_ms": 1500},
        "home_assistant": {"enabled": True},
    }


def validate_setup(value):
    check(isinstance(value, dict), "Invalid setup")
    result = default_setup()
    try:
        for mode in ("browser", "hdhomerun"):
            check(type(value["modes"][mode]) is bool, "Choose which modes to enable")
            result["modes"][mode] = value["modes"][mode]
        check(any(result["modes"].values()), "Enable at least one source mode")
        result["browser"]["home_url"] = browser_url(value["browser"]["home_url"])
        check(value["browser"]["youtube_quality"] in {"1080p", "720p", "auto"}, "Invalid YouTube preference")
        result["browser"]["youtube_quality"] = value["browser"]["youtube_quality"]
        devices = value["hdhomerun"]["devices"]
        check(isinstance(devices, list) and len(devices) <= 4, "Use at most four HDHomeRun devices")
        ids = set()
        for device in devices:
            device_id = text(device["id"], "device ID", 8).upper()
            check(re.fullmatch(r"[0-9A-F]{8}", device_id) and device_id not in ids, "Invalid or duplicate tuner")
            ids.add(device_id)
            result["hdhomerun"]["devices"].append({"id": device_id, "name": text(device["name"], "device name"), "address": local_address(device["address"])})
        check(not result["modes"]["hdhomerun"] or devices, "Find or add your HDHomeRun before enabling live TV")
        video = value["video"]
        check(video["resolution"] in {"1080p", "720p"}, "Choose 1080p or 720p")
        check(type(video["fps"]) is int and video["fps"] in {30, 60}, "Choose 30 or 60 fps")
        check(video["codec"] == "h264", "This release supports H.264 video")
        check(video["encoder"] in {"auto", "libopenh264", "h264_vaapi"}, "Invalid encoder")
        check(type(video["bitrate_mbps"]) is int and 2 <= video["bitrate_mbps"] <= 20, "Video bitrate must be between 2 and 20 Mbps")
        check(type(video["deinterlace"]) is bool, "Invalid deinterlace choice")
        result["video"] = {key: video[key] for key in result["video"]}
        audio = value["audio"]
        check(type(audio["enabled"]) is bool, "Invalid audio choice")
        check(type(audio["latency_ms"]) is int and 500 <= audio["latency_ms"] <= 2000, "Playback buffer must be between 500 and 2000 ms")
        result["audio"] = {key: audio[key] for key in result["audio"]}
        check(type(value["home_assistant"]["enabled"]) is bool, "Invalid Home Assistant choice")
        result["home_assistant"]["enabled"] = value["home_assistant"]["enabled"]
        result["complete"] = True
    except (KeyError, TypeError) as exc:
        raise UserError("Setup is incomplete") from exc
    return result


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "settings.json"
        if self.path.exists() or self.path.is_symlink():
            try:
                self.data = private_json(self.path)
                check(self.data["schema"] == 1, "Unsupported configuration version; data preserved")
                identifier(self.data["installation_id"])
                if self.data["setup"]["complete"]:
                    validate_setup(self.data["setup"])
            except (ValueError, KeyError, TypeError) as exc:
                raise UserError("Saved configuration needs attention; existing data was preserved") from exc
        else:
            self.data = {"schema": 1, "installation_id": uuid.uuid4().hex, "setup": default_setup(), "receivers": [], "pages": [], "favorites": [], "selected_channels": {}, "selected_pages": {}}
            self.save()

    def save(self):
        atomic_json(self.path, self.data)

    def public(self):
        return copy.deepcopy({key: self.data[key] for key in ("setup", "receivers", "pages", "favorites", "selected_channels", "selected_pages")})

    def receiver(self, receiver_id):
        identifier(receiver_id)
        receiver = next((r for r in self.data["receivers"] if r["id"] == receiver_id), None)
        check(receiver is not None, "TV is not configured")
        return receiver

    def page(self, page_id):
        identifier(page_id)
        page = next((p for p in self.data["pages"] if p["id"] == page_id), None)
        check(page is not None, "Saved page was not found")
        return page

    def save_page(self, value):
        page = {"id": identifier(value["id"]) if value.get("id") else uuid.uuid4().hex, "name": text(value.get("name"), "page name", 60), "url": browser_url(value.get("url"))}
        pages = [p for p in self.data["pages"] if p["id"] != page["id"]]
        check(len(pages) < 32, "Use at most 32 saved pages")
        check(not any(p["name"] == page["name"] for p in pages), "A page already uses that name")
        pages.append(page)
        previous = self.data["pages"]
        self.data["pages"] = pages
        try:
            self.save()
        except Exception:
            self.data["pages"] = previous
            raise
        return page

    def setup(self, value):
        result = validate_setup(value)
        previous = self.data["setup"]
        self.data["setup"] = result
        try:
            self.save()
        except Exception:
            self.data["setup"] = previous
            raise
        return result
