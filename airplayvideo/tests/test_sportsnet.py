"""Small public metadata fixtures; no raw HTML, accounts, cookies or media data."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import aiohttp
import pytest

from service import sportsnet


NOW = datetime(2026, 10, 28, 23, 45, tzinfo=timezone.utc)
# Public factual subset observed 2026-10-25 at:
# https://watch.sportsnet.ca/event/Philadelphia_Washington_227232
PHILADELPHIA = {
    "id": "227232", "type": "event", "title": "Philadelphia @ Washington",
    "eventStartDate": "2026-10-28T23:30:00.000Z", "eventEndDate": "2026-10-29T03:30:00.000Z",
    "path": "/event/Philadelphia_Washington_227232", "watchPath": "/watch/Philadelphia_Washington_227232",
    "customFields": {"League": "nhl", "HomeTeamToggle": "Washington", "AwayTeamToggle": "Philadelphia",
                     "VideoStatus": "Scheduled", "VideoWorkflow": "LIVE",
                     "BroadcastName": "Capitals Broadcast: Philadelphia @ Washington", "FeedType": "home"},
}
# This completed event still had VideoWorkflow=LIVE, so it must never launch.
# https://watch.sportsnet.ca/event/Buffalo_Washington_226879
BUFFALO_REPLAY = {
    "id": "226879", "type": "event", "title": "Buffalo @ Washington",
    "eventStartDate": "2026-10-24T23:00:00.000Z", "eventEndDate": "2026-10-25T00:50:00.000Z",
    "path": "/event/Buffalo_Washington_226879", "watchPath": "/watch/Buffalo_Washington_226879",
    "customFields": {"League": "nhl", "HomeTeamToggle": "Washington", "AwayTeamToggle": "Buffalo",
                     "VideoStatus": "OnDemand", "VideoWorkflow": "LIVE"},
}


def event(*, status="Live", event_id="227232", **changes):
    # Synthetic Live transition of the observed scheduled event, not evidence
    # that a live Capitals game was observed during implementation.
    item = deepcopy(PHILADELPHIA)
    item["customFields"]["VideoStatus"] = status
    if event_id != item["id"]:
        item["id"] = event_id
        item["path"] = "/event/Philadelphia_Washington_" + event_id
        item["watchPath"] = "/watch/Philadelphia_Washington_" + event_id
    item.update(changes)
    return item


def html(*items, page=None):
    cache = {"list": {"23855|page_size=24": {"list": {"items": list(items)}}}}
    if page is not None:
        cache["page"] = {page["path"]: {"item": page}}
    return "<script>window.__data = JSON.parse(" + repr(json.dumps({"cache": cache})) + ");</script>"


def parsed(item, *, now=NOW):
    return sportsnet.parse_catalog_page(html(item), checked_at=now)


def test_observed_scheduled_fixture_preserves_provider_url_and_utc_identity():
    game, = parsed(PHILADELPHIA, now=NOW - timedelta(hours=1))
    assert game.watch_url == "https://watch.sportsnet.ca/watch/Philadelphia_Washington_227232"
    assert game.game_key == "nhl|Philadelphia|Washington|2026-10-28T23:30:00Z"
    assert game.start.tzinfo == timezone.utc and game.capitals_broadcast
    assert game.to_dict()["status"] == "Scheduled"


def test_workflow_live_is_not_liveness_and_unknown_status_fails_closed():
    assert not parsed(BUFFALO_REPLAY)
    for status in ("OnDemand", "LIVE", "live", "Unknown", None, True):
        assert not parsed(event(status=status))


def test_matching_requires_explicit_nhl_and_washington_team_not_title():
    for changes in ({"League": "soccer"}, {"League": None},
                    {"HomeTeamToggle": "Chicago", "AwayTeamToggle": "Philadelphia"},
                    {"HomeTeamToggle": "Washington Capitals"},
                    {"AwayTeamToggle": "Washington"}):
        item = event()
        item["customFields"].update(changes)
        assert not parsed(item)


@pytest.mark.parametrize("watch_path", [
    "http://watch.sportsnet.ca/watch/Philadelphia_Washington_227232",
    "https://watch.sportsnet.ca.evil.invalid/watch/Philadelphia_Washington_227232",
    "https://user:secret@watch.sportsnet.ca/watch/Philadelphia_Washington_227232",
    "//evil.invalid/watch/Philadelphia_Washington_227232",
    "/watch/Philadelphia_Washington_227232?token=secret",
    "/watch/Philadelphia_Washington_227232#secret",
    "/watch/Philadelphia_Washington_227232/../other",
    "/watch/Philadelphia_Washington_%3227232",
    "/watch/Philadelphia_Washington_999999",
    "\nhttps://watch.sportsnet.ca/watch/Philadelphia_Washington_227232",
    "/watch\\Philadelphia_Washington_227232", None, {},
])
def test_untrusted_watch_paths_are_never_emitted(watch_path):
    assert not parsed(event(watchPath=watch_path))


def test_absolute_same_origin_path_is_allowed_but_bad_event_path_is_not():
    item = event(watchPath="https://watch.sportsnet.ca/watch/Philadelphia_Washington_227232")
    assert parsed(item)
    item["path"] = "https://evil.invalid/event/Philadelphia_Washington_227232"
    assert not parsed(item)


@pytest.mark.parametrize("start,end", [
    ("2026-10-28T23:30:00", "2026-10-29T03:30:00Z"),
    ("not-a-date", "2026-10-29T03:30:00Z"),
    (None, "2026-10-29T03:30:00Z"),
    ("2026-10-28T23:30:00Z", "2026-10-28T23:30:00Z"),
    ("2026-10-28T23:30:00Z", "2026-10-30T03:30:00Z"),
    ("9999-12-31T23:00:00Z", "9999-12-31T23:30:00Z"),
    ("0001-01-01T00:00:00+10:00", "0001-01-01T00:30:00+10:00"),
])
def test_invalid_or_unbounded_dates_fail_closed(start, end):
    assert not parsed(event(eventStartDate=start, eventEndDate=end))


def test_stale_future_and_ended_live_events_never_launch():
    assert parsed(event())
    assert not parsed(event(), now=NOW - timedelta(hours=1))
    assert not parsed(event(), now=NOW + timedelta(hours=4))
    assert not parsed(event(eventEndDate="2026-10-29T11:00:00Z"), now=NOW + timedelta(hours=6))
    assert not parsed(event(status="Scheduled"), now=NOW - timedelta(days=31))
    assert not parsed(event(status="Scheduled"), now=NOW + timedelta(hours=7))


def test_duplicate_broadcasts_prefer_capitals_and_share_one_game_key():
    away = event(event_id="227233")
    away["customFields"].update(BroadcastName="Flyers Broadcast: Philadelphia @ Washington", FeedType="away")
    games = sportsnet.parse_catalog_page(html(away, event(), event()), checked_at=NOW)
    assert len(games) == 1 and games[0].id == "227232"


def test_duplicate_live_feed_wins_over_scheduled_preferred_broadcast():
    live = event(event_id="227233")
    live["customFields"].update(BroadcastName="Flyers Broadcast: Philadelphia @ Washington", FeedType="away")
    games = sportsnet.parse_catalog_page(html(live, event(status="Scheduled")), checked_at=NOW)
    assert len(games) == 1 and games[0].id == "227233" and games[0].status == "Live"


@pytest.mark.parametrize("source", ["", "<script>window.__data = {}; </script>",
    "window.__data = JSON.parse(doSomething())", "window.__data = JSON.parse('{}')",
    "window.__data = JSON.parse('[]')", "window.__data = JSON.parse('{bad')",
    "x" * (sportsnet.MAX_HTML_BYTES + 1)])
def test_absent_malformed_executable_or_oversized_state_is_rejected(source):
    with pytest.raises(ValueError):
        sportsnet.parse_catalog_page(source, checked_at=NOW)


def test_double_quoted_json_wrapper_and_unrelated_private_state_are_ignored():
    data = {"cache": {"list": {"x": {"list": {"items": [event()]}}}},
            "account": "not-read", "session": "not-read", "profile": "not-read"}
    source = "window.__data = JSON.parse(" + json.dumps(json.dumps(data)) + ")"
    assert len(sportsnet.parse_catalog_page(source, checked_at=NOW)) == 1


def fake_pages(monkeypatch, client, pages):
    requests = []
    async def get(session, url):
        requests.append(url)
        value = pages[url]
        if isinstance(value, BaseException):
            raise value
        return value
    monkeypatch.setattr(client, "_get", get)
    return requests


@pytest.mark.asyncio
async def test_client_refreshes_scheduled_discovery_before_reporting_live(monkeypatch):
    client = sportsnet.SportsnetClient()
    requests = fake_pages(monkeypatch, client, {
        sportsnet.TEAM_URL: html(PHILADELPHIA),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: html(page=event()),
    })
    result = await client.fetch(now=NOW)
    assert result.status == "live" and len(result.live_candidates) == 1 and not result.upcoming
    assert requests == [sportsnet.TEAM_URL, sportsnet.ORIGIN + PHILADELPHIA["path"]]
    assert result.to_dict()["checked_at"] == "2026-10-28T23:45:00Z"


@pytest.mark.asyncio
async def test_specific_replay_overrules_cached_live_discovery(monkeypatch):
    client = sportsnet.SportsnetClient()
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html(event()),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: html(page=event(status="OnDemand"))})
    result = await client.fetch(now=NOW)
    assert result.status == "none" and not result.live_candidates


@pytest.mark.asyncio
async def test_live_deadline_is_rechecked_when_fetch_finishes(monkeypatch):
    clock = iter((NOW, NOW + timedelta(hours=4)))
    monkeypatch.setattr(sportsnet, "_now", lambda: next(clock))
    client = sportsnet.SportsnetClient()
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html(event()),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: html(page=event())})
    result = await client.fetch()
    assert result.status == "none" and not result.live_candidates
    assert result.checked_at == NOW + timedelta(hours=4)


@pytest.mark.asyncio
async def test_client_reports_upcoming_without_claiming_live(monkeypatch):
    client = sportsnet.SportsnetClient()
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html(PHILADELPHIA),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: html(page=PHILADELPHIA)})
    result = await client.fetch(now=NOW - timedelta(hours=1))
    assert result.status == "upcoming" and not result.live_candidates and len(result.upcoming) == 1


@pytest.mark.asyncio
async def test_client_freshly_verifies_capitals_alternate_and_game_identity(monkeypatch):
    primary = event()
    primary["customFields"].update(BroadcastName="Flyers Broadcast: Philadelphia @ Washington", FeedType="away")
    alternate = event(event_id="227233")
    # Related entries on the real site omit dates/league/teams. They are only
    # discovery hints; the alternate's full event page must establish identity.
    ref = {k: alternate[k] for k in ("id", "type", "title", "path", "watchPath", "customFields")}
    primary["relationships"] = [{"items": [ref]}]
    client = sportsnet.SportsnetClient()
    pages = {sportsnet.TEAM_URL: html(primary),
        sportsnet.ORIGIN + primary["path"]: html(page=primary),
        sportsnet.ORIGIN + alternate["path"]: html(page=alternate)}
    requests = fake_pages(monkeypatch, client, pages)
    result = await client.fetch(now=NOW)
    assert len(requests) == 3 and result.live_candidates[0].id == "227233"
    alternate["eventStartDate"] = "2026-10-28T22:30:00Z"
    pages[sportsnet.ORIGIN + alternate["path"]] = html(page=alternate)
    result = await client.fetch(now=NOW)
    assert result.live_candidates[0].id == "227232"


@pytest.mark.asyncio
async def test_wrong_alternate_identity_or_scheduled_feed_cannot_replace_live(monkeypatch):
    primary = event()
    primary["customFields"].update(BroadcastName="Flyers Broadcast: Philadelphia @ Washington", FeedType="away")
    alternate = event(event_id="227233")
    primary["relationships"] = [{"items": [alternate]}]
    client = sportsnet.SportsnetClient()
    pages = {sportsnet.TEAM_URL: html(primary),
             sportsnet.ORIGIN + primary["path"]: html(page=primary)}
    fake_pages(monkeypatch, client, pages)
    for invalid in (event(status="Scheduled", event_id="227233"), event(event_id="999999")):
        # A malicious item may appear beneath the requested event's cache key.
        pages[sportsnet.ORIGIN + alternate["path"]] = html(page=invalid).replace(invalid["path"], alternate["path"], 1)
        result = await client.fetch(now=NOW)
        assert result.status == "live" and result.live_candidates[0].id == "227232"
    pages[sportsnet.ORIGIN + alternate["path"]] = aiohttp.ClientError("private details")
    result = await client.fetch(now=NOW)
    assert result.status == "live" and result.live_candidates[0].id == "227232" and not result.error


@pytest.mark.asyncio
async def test_specific_page_cannot_substitute_another_game(monkeypatch):
    client = sportsnet.SportsnetClient()
    wrong = event(event_id="999999")
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html(event()),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: html(page=wrong)})
    result = await client.fetch(now=NOW)
    assert result.status == "none" and not result.live_candidates


@pytest.mark.asyncio
async def test_failure_is_constant_safe_error_and_discards_partial_results(monkeypatch):
    client = sportsnet.SportsnetClient()
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html(event()),
        sportsnet.ORIGIN + PHILADELPHIA["path"]: aiohttp.ClientError("secret cookie credential")})
    result = await client.fetch(now=NOW)
    assert result.status == "error" and not result.live_candidates and not result.upcoming
    assert "secret" not in repr(result) and "credential" not in result.error


@pytest.mark.asyncio
async def test_request_count_and_whole_check_timeout_are_bounded(monkeypatch):
    client = sportsnet.SportsnetClient(max_event_fetches=1)
    first, second = event(), event(event_id="227234", eventStartDate="2026-10-28T22:30:00Z")
    pages = {sportsnet.TEAM_URL: html(first, second),
             sportsnet.ORIGIN + first["path"]: html(page=first),
             sportsnet.ORIGIN + second["path"]: html(page=second)}
    requests = fake_pages(monkeypatch, client, pages)
    result = await client.fetch(now=NOW)
    assert result.status == "live" and len(requests) == 2
    async def timed_out(session, url):
        raise asyncio.TimeoutError("private details")
    monkeypatch.setattr(client, "_get", timed_out)
    assert (await client.fetch(now=NOW)).status == "error"


@pytest.mark.asyncio
async def test_client_uses_private_cookie_free_session_without_env_auth(monkeypatch):
    captured = {}
    real_session = aiohttp.ClientSession
    def session(**kwargs):
        captured.update(kwargs)
        return real_session(**kwargs)
    monkeypatch.setattr(sportsnet.aiohttp, "ClientSession", session)
    client = sportsnet.SportsnetClient()
    fake_pages(monkeypatch, client, {sportsnet.TEAM_URL: html()})
    assert (await client.fetch(now=NOW)).status == "none"
    assert isinstance(captured["cookie_jar"], aiohttp.DummyCookieJar)
    assert captured["trust_env"] is False and "auth" not in captured and "cookies" not in captured


class Response:
    def __init__(self, chunks, status=200, content_length=None):
        self.chunks, self.status, self.content_length = chunks, status, content_length
        self.content = self
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def iter_chunked(self, size):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_http_layer_disallows_redirects_and_bounds_streamed_body():
    client = sportsnet.SportsnetClient()
    class Session:
        response = Response([b"public"])
        def get(self, url, **kwargs):
            self.options = kwargs
            return self.response
    session = Session()
    assert await client._get(session, sportsnet.TEAM_URL) == "public"
    assert session.options == {"allow_redirects": False, "headers": {"Cache-Control": "no-cache"}}
    for response in (Response([], status=302), Response([], content_length=sportsnet.MAX_HTML_BYTES + 1),
                     Response([b"x" * sportsnet.MAX_HTML_BYTES, b"x"])):
        session.response = response
        with pytest.raises(ValueError):
            await client._get(session, sportsnet.TEAM_URL)
