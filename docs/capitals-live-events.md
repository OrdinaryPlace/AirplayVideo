# Live Washington Capitals events

The Capitals helper starts a bounded check after an explicit UI or MQTT request.
Its intended trigger is the existing Home Assistant `sensor.team_tracker`
transition from `PRE` to `IN`, after that automation's red-light/restore actions.
It does not run an independent daily game scheduler.

## How a check works

The request names the paired TVs that may switch to Sportsnet. The app checks
the public catalog immediately, then every 30 seconds for up to 20 minutes.
Upcoming games and replays never start playback. A launch requires exactly one
fresh, recognized NHL event with Washington as the home or away team and
`VideoStatus` exactly `Live`. It rechecks catalog age and event time bounds
before playback; unknown, ambiguous, stale or malformed results cannot start
Sportsnet playback.
The catalog's live age limit is six hours, bounded further by the provider's
event end time. Launches require a catalog checked within the last two minutes.

When eligible, the app enables the saved browser VPN route, then requests the
provider-supplied game page on the selected TVs using the persistent Sportsnet
session. Existing AirplayVideo playback on those targets can be replaced.
An unrelated AirplayVideo TV, active recording, measurement or teardown can
prevent the switch. The gateway remains the fixed route if it is unavailable;
there is no direct fallback while VPN is On.

The app records the game key before attempting Stop/VPN/Play. A game has at
most one automatic attempt, including failed or interrupted starts, so a later
kick cannot repeatedly recast it. An unexpired explicit check can resume after
an app restart. **Cancel pending check** stops the check; it does not stop
playback already requested. Normal playback controls remain available.

## Play the current game now

Each paired TV's Home Assistant device has **Play current Capitals game** when
browser mode and a VPN gateway are configured. Press the buttons for the TVs
you want to watch. These controls look up a fresh live game and open its exact
Sportsnet watch page; they do not open the saved Sportsnet homepage. The app
also has **Play current game** for its selected TVs.

This is one immediate check. If no single eligible live game is available,
the app reports the result without starting a TV or waiting for a future game.
An explicit manual request can retry a game that an automatic check already
attempted. It does not erase the automatic attempt history or weaken the
automatic routine's duplicate protection.

Per-TV buttons can join the same exact game already playing through this app
without navigating or restarting other viewers. They reject a request that
would change an unrequested TV's different content. A repeat for an already
connected target is a no-op; a failed target can be retried. Stop and cancel
take precedence over pending work. Cancel does not stop playback that was
already requested; use the TV's Stop control to end that playback.

For a single explicit multi-TV request, publish to the app's displayed command
topic with paired receiver IDs. Use **QoS 0** and **retain: false** for manual
playback commands, as the native buttons do:

```yaml
action: mqtt.publish
data:
  topic: airplayvideo/INSTALLATION_ID/command
  qos: 0
  retain: false
  payload: >-
    {"action":"capitals_play","receivers":["UPSTAIRS_RECEIVER_ID","BASEMENT_RECEIVER_ID"]}
```

The optional `"join":true` permits adding these TVs to an existing session of
the same exact game, preserving its other viewers. It never authorizes changing
their content. Commands are not an exactly-once messaging protocol: publishing
another manual command after a stop or failure is a new retry request. Do not
use retained commands or at-least-once delivery to automate manual retries.

These controls retain the saved Sportsnet graphics, VPN gateway, profile and
output settings. They do not force a player quality or certify live-edge
selection, account entitlement, physical picture or sound. The connection
sensors and Last error identify each TV's sender status separately from the
Capitals launch status.

### Home Assistant Supervisor action

An authenticated Home Assistant Supervisor app-input action is also supported.
This uses the existing Supervisor authorization and opens no network listener.
Use the installed app slug and the same explicit paired TV IDs:

```yaml
action: hassio.app_stdin
data:
  app: YOUR_AIRPLAYVIDEO_APP_SLUG
  input:
    action: capitals_play
    request_id: manual-game-1
    receivers:
      - UPSTAIRS_RECEIVER_ID
      - BASEMENT_RECEIVER_ID
    join: true
```

The input actions are `capitals_play` and read-only `status`, plus the scoped
`stop` and idle-only `measure_frame_stages` operations introduced in 0.2.28.
Use a fresh `request_id` to correlate a request with its bounded
`AIRPLAYVIDEO_CONTROL` log response; it is not a duplicate-suppression key.
Status returns connection enums and target IDs, without account data, page
content, URLs or raw errors. An `ok` response means the app handled the command, not
that a physical TV displayed the game. Existing per-TV HA sensors remain the
normal source for connection and error details.

Since 0.2.28, successful status responses also include a bounded graphics and
video projection. `video.sportsnet_rendering` is the saved choice;
`browser_graphics.launch_rendering` and `gpu_flags_applied` describe the exact
arguments selected for the current Chrome launch. They do not certify a video
decoder or effective hardware renderer. A mapped render-node basename and,
where readable, an owned GPU process with an open render device provide
separate evidence. Process restrictions, nested sandbox namespaces, incomplete
observations and unsupported hosts produce `null` rather than a claim of no GPU.

`video.stream_encoder` and `stream_fps` come from the current owned native
stream. Saved output settings are reported separately. `metrics` contains
only source encoded-frame and current receiver delivery counters, with a
monotonic sample time. Compare two samples with the same generation and stable
streaming targets over at least fifteen seconds. Discard intervals crossing
Stop, restart, source replacement or a receiver join. These counters do not
count fresh source pictures or certify frame rate on a physical TV.

For a controlled picture-freshness measurement, first send
`{"action":"stop","receiver":"PAIRED_RECEIVER_ID","request_id":"stop-1"}`.
Stop requires one explicit paired receiver and also cancels its pending game
launch. Check that status has no remaining media targets before requesting
`{"action":"measure_frame_stages","request_id":"frames-1"}`. It uses the
existing two-second warmup and fifteen-second browser measurement, preserving
the current page and profile and returning only a strictly validated numeric
report. It refuses active playback and conflicting diagnostics; it never stops
other TVs or resumes playback itself. Cancellation and timeout reap only its
owned measurement. Request a new explicit game launch when ready to resume.

The picture-change samples can identify repeated content before H.264 but are
not original frame identities. The test is off-TV, so content changes and the
different host load limit comparison with a running AirPlay session.

This follows Home Assistant's documented
[app-input action](https://contributors.home-assistant.io/apps/guides/communication/#send-input-to-your-app)
and authenticated
[Supervisor stdin endpoint](https://contributors.home-assistant.io/apps/supervisor-api/apps/#post-addonsappstdin).

These are source behavior and acceptance criteria for the new helper. Its
tests do not establish a successful natural game-start trigger, account
entitlement, player live-edge selection, physical picture or audible sound.
`Live` describes the provider broadcast, which can include pregame; it does
not independently certify puck drop.

## Home Assistant trigger

Keep the current `PRE` → `IN` red-light automation and its existing light
restore actions. Append the final `mqtt.publish` action below after the restore
sequence. Use this installation's command topic and its paired receiver IDs;
the placeholders here are not usable IDs. Keep `retain: false`.

```yaml
triggers:
  - trigger: state
    entity_id: sensor.team_tracker
    from: PRE
    to: IN
actions:
  # Keep the existing red-light and restore actions here, in their current order.
  - action: mqtt.publish
    data:
      topic: airplayvideo/INSTALLATION_ID/command
      retain: false
      payload: >-
        {"action":"capitals_live","receivers":["CART_RECEIVER_ID","UPSTAIRS_RECEIVER_ID","BASEMENT_RECEIVER_ID"]}
```

This is the surrounding shape, not a replacement for the existing automation.
Use the app's displayed command topic and paired IDs; do not publish household
IDs, credentials or account URLs in public documentation. To cancel only a
pending check, publish `{"action":"capitals_cancel"}` to the same topic with
`retain: false`.

The browser VPN switch is also exposed in Home Assistant. Its display name is
set in **Settings → Source defaults → VPN control name**, for example
`Montréal VPN`. Off uses direct browser networking and keeps the saved gateway;
On uses that gateway for browser pages in both persistent sessions. The control
does not change AirPlay's LAN route. See [VPN setup and boundaries](sportsnet-vpn.md).

## Public catalog evidence and limits

On 2026-10-25, Sportsnet's public [Capitals team page](https://watch.sportsnet.ca/team/Washington_Capitals_155283)
and individual event pages embedded `window.__data = JSON.parse(...)` in HTML.
The app reads only catalog `cache.page`/`cache.list` items, using a separate
cookie-free public HTTP session. It does not read browser/account state or
request media manifests, licenses or DRM data. Public requests have bounded
response sizes, timeouts and event counts, and do not follow redirects.

Observed fields include event ID, title, UTC start/end, `path`, `watchPath`,
`League`, `HomeTeamToggle`, `AwayTeamToggle`, `VideoStatus`, `BroadcastName` and
`FeedType`. The observed status values were `Live` for a
[live soccer broadcast](https://watch.sportsnet.ca/event/NEC_vs_FC_Groningen_226995),
`Scheduled` for future hockey, and `OnDemand` for the completed
[Buffalo–Washington game](https://watch.sportsnet.ca/event/Buffalo_Washington_226879).
**`VideoWorkflow: LIVE` also remains on replays and is not a live-game test.**
Cached rendered labels were observed lagging the fresh event metadata; a start
time or a search result saying upcoming/live is insufficient.

The next listed example on that research date was
[Philadelphia @ Washington, October 28, 2026](https://watch.sportsnet.ca/event/Philadelphia_Washington_227232),
with `eventStartDate: 2026-10-28T23:30:00.000Z` — **7:30 p.m. EDT** — and
`VideoStatus: Scheduled`. Its supplied player path was
[`/watch/Philadelphia_Washington_227232`](https://watch.sportsnet.ca/watch/Philadelphia_Washington_227232).
This example is not a standing future schedule or a live selection.

Multiple feeds share a game key made from teams and UTC start. The helper
prefers the Capitals broadcast when it can freshly verify the corresponding
home/away feed. For example,
[Washington @ Nashville](https://watch.sportsnet.ca/event/Washington_Nashville_227490)
defaulted to the Predators home feed; its related
[Capitals away feed](https://watch.sportsnet.ca/watch/Washington_Nashville_227484)
had a different event ID. A missing preferred feed can leave the verified base
feed usable. URLs must remain HTTPS on exactly `watch.sportsnet.ca`, with the
expected event identity and no credentials, query or fragment.

No documented supported Sportsnet API/feed contract was found. This is an
observed website format that may change; parsing failures must not launch a TV.
Fresh catalog metadata is not an entitlement result. Check Sportsnet's
[NHL Game Finder](https://support.sportsnetplus.ca/hc/en-gb/articles/46381950833556-NHL-Game-Finder)
and [regional blackout guidance](https://support.sportsnetplus.ca/hc/en-gb/articles/46382278933780-Are-there-NHL-regional-blackouts-on-Sportsnet).
The [2026/27 rights guide](https://support.sportsnetplus.ca/hc/en-gb/articles/53444277671828-Where-to-Watch-NHL-Hockey-In-2026-2027)
also lists national programming exclusive to other services. A catalog entry,
Canadian exit or Premium marker does not promise this account can play it.
