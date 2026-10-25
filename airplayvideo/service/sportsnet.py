"""Read public Sportsnet catalog metadata; never access accounts or media URLs.

Observed 2026-10-25 at https://watch.sportsnet.ca/team/Washington_Capitals_155283:
HTML embeds window.__data = JSON.parse('...'), with cache.page/list items.
This is an observed website shape, not a documented or stable Sportsnet API.
VideoStatus (Live/Scheduled/OnDemand) matters: VideoWorkflow=LIVE also marks
replays. A live catalog broadcast does not certify puck drop or the live edge.
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from urllib.parse import urlsplit

import aiohttp


ORIGIN = "https://watch.sportsnet.ca"
TEAM_URL = ORIGIN + "/team/Washington_Capitals_155283"
MAX_HTML_BYTES = 2 * 1024 * 1024
MAX_LIVE_AGE = timedelta(hours=6)
UPCOMING_HORIZON = timedelta(days=30)
_STATE = re.compile(r"window\.__data\s*=\s*JSON\.parse\(\s*(('(?:[^'\\]|\\.)*')|(\"(?:[^\"\\]|\\.)*\"))\s*\)", re.S)
_SAFE_ERROR = "Sportsnet public catalog is unavailable; no live game was selected."
_FETCH_ERRORS = (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError,
                 AttributeError, UnicodeError, OverflowError)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware check time is required.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class SportsnetEvent:
    id: str
    title: str
    watch_url: str
    event_url: str
    start: datetime
    end: datetime
    home: str
    away: str
    status: str
    broadcast: str
    feed: str

    @property
    def game_key(self) -> str:
        return f"nhl|{self.away}|{self.home}|{_iso(self.start)}"

    @property
    def capitals_broadcast(self) -> bool:
        return (self.broadcast.startswith("Capitals Broadcast:")
                and self.feed == ("home" if self.home == "Washington" else "away"))

    def to_dict(self) -> dict:
        return {"id": self.id, "title": self.title, "watch_url": self.watch_url,
                "event_url": self.event_url, "start": _iso(self.start), "end": _iso(self.end),
                "home": self.home, "away": self.away, "status": self.status,
                "broadcast": self.broadcast, "feed": self.feed, "game_key": self.game_key}


@dataclass(frozen=True)
class SportsnetCatalog:
    status: str
    checked_at: datetime
    live_candidates: tuple[SportsnetEvent, ...] = ()
    upcoming: tuple[SportsnetEvent, ...] = ()
    error: str = ""

    def to_dict(self) -> dict:
        return {"status": self.status, "checked_at": _iso(self.checked_at),
                "live_candidates": [event.to_dict() for event in self.live_candidates],
                "upcoming": [event.to_dict() for event in self.upcoming], "error": self.error}


def _public_cache(html: str) -> dict:
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("Invalid public catalog.")
    match = _STATE.search(html)
    if not match:
        raise ValueError("Invalid public catalog.")
    # Only a quoted string literal is accepted, never JavaScript execution/eval.
    try:
        state = json.loads(ast.literal_eval(match.group(1)))
        cache = state.get("cache")
    except (ValueError, SyntaxError, TypeError, AttributeError, RecursionError):
        raise ValueError("Invalid public catalog.") from None
    if not isinstance(cache, dict):
        raise ValueError("Invalid public catalog.")
    return cache  # Deliberately never inspect session/account/profile state.


def _items(cache: dict):
    pages, lists = cache.get("page", {}), cache.get("list", {})
    if isinstance(pages, dict):
        for page in pages.values():
            if isinstance(page, dict) and isinstance(page.get("item"), dict):
                yield page["item"]
    if isinstance(lists, dict):
        for value in lists.values():
            listing = value.get("list", {}) if isinstance(value, dict) else {}
            items = listing.get("items", []) if isinstance(listing, dict) else []
            if isinstance(items, list):
                yield from (item for item in items if isinstance(item, dict))


def _url(value, kind: str, event_id: str) -> str | None:
    if not isinstance(value, str) or not value or any(ord(c) <= 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.query or parsed.fragment:
        return None
    if parsed.scheme or parsed.netloc:
        if parsed.scheme != "https" or parsed.netloc != "watch.sportsnet.ca":
            return None
    if not re.fullmatch(r"/" + kind + r"/[A-Za-z0-9_-]+_" + re.escape(event_id), parsed.path):
        return None
    return ORIGIN + parsed.path


def _text(value, limit=200) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= limit and all(ord(c) >= 32 for c in value) else ""


def _event(item: dict, now: datetime) -> SportsnetEvent | None:
    fields = item.get("customFields")
    event_id = item.get("id")
    if (item.get("type") != "event" or not isinstance(fields, dict)
            or not isinstance(event_id, str) or not re.fullmatch(r"[0-9]{1,12}", event_id)
            or fields.get("League") != "nhl"):
        return None
    home, away = _text(fields.get("HomeTeamToggle"), 64), _text(fields.get("AwayTeamToggle"), 64)
    status = fields.get("VideoStatus")
    if not home or not away or home == away or "Washington" not in (home, away) or status not in ("Live", "Scheduled"):
        return None
    try:
        start = _utc(datetime.fromisoformat(item["eventStartDate"].replace("Z", "+00:00")))
        end = _utc(datetime.fromisoformat(item["eventEndDate"].replace("Z", "+00:00")))
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return None
    if not start < end or end - start > timedelta(hours=12):
        return None
    if status == "Live" and not start <= now < min(end, start + MAX_LIVE_AGE):
        return None
    if status == "Scheduled" and not now - MAX_LIVE_AGE < start <= now + UPCOMING_HORIZON:
        return None
    watch, event = _url(item.get("watchPath"), "watch", event_id), _url(item.get("path"), "event", event_id)
    title = _text(item.get("title"))
    if not watch or not event or not title:
        return None
    return SportsnetEvent(event_id, title, watch, event, start, end, home, away, status,
                         _text(fields.get("BroadcastName")), _text(fields.get("FeedType"), 16))


def _deduplicate(events):
    chosen = {}
    for event in events:
        previous = chosen.get(event.game_key)
        rank = (event.status == "Live", event.capitals_broadcast, event.id)
        if previous is None or rank > (previous.status == "Live", previous.capitals_broadcast, previous.id):
            chosen[event.game_key] = event
    return tuple(sorted(chosen.values(), key=lambda event: (event.start, event.game_key)))


def parse_catalog_page(html: str, *, checked_at: datetime) -> tuple[SportsnetEvent, ...]:
    """Parse eligible public entries; this alone does not establish freshness."""
    now = _utc(checked_at)
    return _deduplicate(event for item in _items(_public_cache(html)) if (event := _event(item, now)))


def _preferred_alternate(item: dict, event: SportsnetEvent) -> str | None:
    relationships = item.get("relationships", [])
    if not isinstance(relationships, list):
        return None
    for relationship in relationships:
        items = relationship.get("items", []) if isinstance(relationship, dict) else []
        if not isinstance(items, list):
            continue
        for alternate in items:
            if not isinstance(alternate, dict):
                continue
            fields, event_id = alternate.get("customFields", {}), alternate.get("id")
            if (not isinstance(fields, dict) or not isinstance(event_id, str)
                    or not re.fullmatch(r"[0-9]{1,12}", event_id)):
                continue
            if (alternate.get("title") == event.title
                    and _text(fields.get("BroadcastName")).startswith("Capitals Broadcast:")
                    and fields.get("FeedType") == ("home" if event.home == "Washington" else "away")
                    and _url(alternate.get("watchPath"), "watch", event_id)):
                return _url(alternate.get("path"), "event", event_id)
    return None


class SportsnetClient:
    """One bounded public check, with no cookies, redirects, login or retries."""
    def __init__(self, *, timeout_seconds=8, max_event_fetches=8):
        self.timeout_seconds = min(max(float(timeout_seconds), 1), 15)
        self.max_event_fetches = min(max(int(max_event_fetches), 1), 12)

    async def _get(self, session, url: str) -> str:
        async with session.get(url, allow_redirects=False, headers={"Cache-Control": "no-cache"}) as response:
            if response.status != 200 or (response.content_length or 0) > MAX_HTML_BYTES:
                raise ValueError("Invalid public catalog response.")
            body = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                body.extend(chunk)
                if len(body) > MAX_HTML_BYTES:
                    raise ValueError("Invalid public catalog response.")
            return body.decode("utf-8")

    async def fetch(self, *, now: datetime | None = None) -> SportsnetCatalog:
        checked = _utc(now or _now())
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
            async with asyncio.timeout(self.timeout_seconds * 3):
                async with aiohttp.ClientSession(timeout=timeout, cookie_jar=aiohttp.DummyCookieJar(),
                                                 trust_env=False) as session:
                    discovery = parse_catalog_page(await self._get(session, TEAM_URL), checked_at=checked)
                    # A cached Scheduled entry may already be live on its own page.
                    pending = sorted(discovery, key=lambda event: (event.status != "Live", event.start))
                    verified, fetched, count = [], set(), 0
                    for candidate in pending:
                        if count >= self.max_event_fetches:
                            break
                        url = candidate.event_url
                        count += 1
                        cache = _public_cache(await self._get(session, url))
                        page = cache.get("page", {}).get(url.removeprefix(ORIGIN), {})
                        item = page.get("item", {}) if isinstance(page, dict) else {}
                        event = _event(item, checked) if isinstance(item, dict) else None
                        if (not event or event.id != candidate.id or event.event_url != url
                                or event.game_key != candidate.game_key):
                            continue
                        fetched.add(url)
                        alternate = _preferred_alternate(item, event) if not event.capitals_broadcast else None
                        if alternate and alternate not in fetched and count < self.max_event_fetches:
                            count += 1
                            fetched.add(alternate)
                            try:
                                alt_cache = _public_cache(await self._get(session, alternate))
                                alt_page = alt_cache.get("page", {}).get(alternate.removeprefix(ORIGIN), {})
                                alt_item = alt_page.get("item", {}) if isinstance(alt_page, dict) else {}
                                alt_event = _event(alt_item, checked) if isinstance(alt_item, dict) else None
                                if (alt_event and alt_event.event_url == alternate
                                        and alt_event.game_key == event.game_key and alt_event.capitals_broadcast
                                        and (event.status != "Live" or alt_event.status == "Live")):
                                    event = alt_event
                            except _FETCH_ERRORS:
                                pass  # Commentary preference cannot invalidate the verified base feed.
                        verified.append(event)
            checked = _utc(now or _now())
            events = _deduplicate(verified)
            live = tuple(event for event in events if event.status == "Live"
                         and event.start <= checked < min(event.end, event.start + MAX_LIVE_AGE))
            upcoming = tuple(event for event in events if event.status == "Scheduled" and event.start > checked)
            return SportsnetCatalog("live" if live else "upcoming" if upcoming else "none", checked, live, upcoming)
        except _FETCH_ERRORS:
            return SportsnetCatalog("error", _utc(now or _now()), error=_SAFE_ERROR)
