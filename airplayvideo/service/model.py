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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

VERSION = "0.2.11"
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


def generated_defaults():
    return {"title": "Hello world", "tagline": "Made with AirplayVideo", "duration_seconds": 60,
            "display": "countdown", "timezone": os.environ.get("TZ", "UTC")}


def validate_generated(value, defaults=None):
    check(isinstance(value, dict), "Invalid generated video settings")
    result = dict(defaults or generated_defaults())
    check(not value.keys() - result.keys(), "Unknown generated video setting")
    result.update(value)
    for key, maximum in (("title", 160), ("tagline", 240)):
        item = result[key]
        check(isinstance(item, str) and len(item) <= maximum and
              not any((ord(c) < 32 and c != "\n") or 0xD800 <= ord(c) <= 0xDFFF for c in item),
              f"Use plain text for the {key} (up to {maximum} characters)")
        result[key] = item.strip()
    check(bool(result["title"]), "Enter a title")
    check(type(result["duration_seconds"]) is int and 1 <= result["duration_seconds"] <= 86400,
          "Length must be a whole number from 1 to 86400 seconds")
    check(result["display"] in ("time", "countdown", "neither"), "Choose time, countdown or neither")
    zone = result["timezone"]
    check(isinstance(zone, str) and len(zone) <= 100 and re.fullmatch(r"[A-Za-z0-9_+/-]+", zone) and
          not zone.startswith("/") and ".." not in zone, "Enter an IANA time zone, such as America/New_York")
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise UserError("Unknown time zone") from exc
    return result


def default_setup():
    return {
        "complete": False,
        "modes": {"browser": True, "hdhomerun": False, "generated": True},
        "generated": generated_defaults(),
        "browser": {"home_url": "https://www.youtube.com/", "youtube_quality": "1080p"},
        "hdhomerun": {"devices": []},
        "video": {"resolution": "1080p", "fps": 30, "codec": "h264", "encoder": "auto", "bitrate_mbps": 8, "rate_control": "auto", "max_bitrate_mbps": 16, "deinterlace": True},
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
        generated = value["modes"].get("generated", True)
        check(type(generated) is bool, "Choose which modes to enable")
        result["modes"]["generated"] = generated
        result["generated"] = validate_generated(value.get("generated", {}))
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
        video = dict(value["video"])
        check(video["resolution"] in {"1080p", "720p"}, "Choose 1080p or 720p")
        check(type(video["fps"]) is int and video["fps"] in {30, 60}, "Choose 30 or 60 fps")
        check(video["codec"] == "h264", "This release supports H.264 video")
        check(video["encoder"] in {"auto", "libopenh264", "h264_vaapi"}, "Invalid encoder")
        check(type(video["bitrate_mbps"]) is int and 2 <= video["bitrate_mbps"] <= 20, "Video bitrate must be between 2 and 20 Mbps")
        video.setdefault("rate_control", "auto")
        video.setdefault("max_bitrate_mbps", max(16, video["bitrate_mbps"]))
        check(video["rate_control"] in {"auto", "vbr"}, "Choose automatic or variable bitrate")
        check(type(video["max_bitrate_mbps"]) is int and 2 <= video["max_bitrate_mbps"] <= 40,
              "Maximum bitrate must be between 2 and 40 Mbps")
        if video["rate_control"] == "vbr":
            check(video["encoder"] != "libopenh264", "Variable bitrate requires hardware encoding")
            check(video["max_bitrate_mbps"] >= video["bitrate_mbps"], "Maximum bitrate must be at least the target bitrate")
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


def patch_setup(current, changes, expected):
    """Merge only edited fields; reject conflicting edits from another client."""
    check(current["complete"], "Finish first-time setup before editing settings")
    check(isinstance(changes, dict) and isinstance(expected, dict), "Invalid settings changes")
    check(changes.keys() == expected.keys(), "Invalid settings changes")
    result = copy.deepcopy(current)
    allowed = default_setup()
    for section, fields in changes.items():
        check(section != "complete" and section in allowed and isinstance(fields, dict), "Unknown settings section")
        check(isinstance(expected[section], dict) and fields.keys() == expected[section].keys(), "Invalid settings changes")
        for key, value in fields.items():
            check(key in allowed[section], "Unknown setting")
            check(current[section][key] == expected[section][key] or current[section][key] == value,
                  "This setting changed in another window. Use Reset this page, then make your edit again.")
            result[section][key] = copy.deepcopy(value)
    return validate_setup(result)


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
                # Additive upgrade: keep every existing preference and identity.
                migrated = "generated" not in self.data["setup"] or "generated" not in self.data["setup"]["modes"]
                self.data["setup"].setdefault("generated", generated_defaults())
                self.data["setup"]["modes"].setdefault("generated", True)
                video = self.data["setup"]["video"]
                for key, value in {"rate_control": "auto", "max_bitrate_mbps": max(16, video["bitrate_mbps"])}.items():
                    if key not in video:
                        video[key] = value
                        migrated = True
                if self.data["setup"]["complete"]:
                    validate_setup(self.data["setup"])
                if migrated:
                    self.save()
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
