"""One explicit, restart-safe live-game check window; never a game scheduler."""
from __future__ import annotations
import asyncio
import copy
from datetime import datetime, timedelta, timezone

from .model import UserError, browser_proxy_enabled, check
from .sportsnet import MAX_LIVE_AGE, SportsnetClient


WINDOW_SECONDS = 20 * 60
POLL_SECONDS = 30
FRESH_SECONDS = 120
MANUAL_SECONDS = 90


def utcnow():
    return datetime.now(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def deadline(value):
    if value is None:
        return None
    check(isinstance(value, str), "Saved Capitals check needs attention")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        check(result.tzinfo is not None and result.utcoffset() is not None,
              "Saved Capitals check needs attention")
        return result.astimezone(timezone.utc)
    except ValueError:
        raise UserError("Saved Capitals check needs attention") from None


class CapitalsLauncher:
    def __init__(self, store, controller, browser_vpn, configuration_lock, changed=lambda: None,
                 *, client=None, now=utcnow):
        self.store, self.controller, self.browser_vpn = store, controller, browser_vpn
        self.configuration_lock, self.changed = configuration_lock, changed
        self.client, self.now = client or SportsnetClient(), now
        self.task, self.generation, self.catalog, self.closing = None, 0, None, False
        self.command_lock = asyncio.Lock()
        self.manual_lock = asyncio.Lock()
        self.manual_epoch = 0
        self.manual_requests = {}
        self.manual_edges = {}
        defaults = {"status": "idle", "error": "", "deadline": None, "receivers": [],
                    "last_attempted_game": "", "last_started_game": "", "last_watch_url": ""}
        raw = store.data.get("capitals", {})
        self.saved = {**defaults, **raw} if isinstance(raw, dict) else defaults
        try:
            until = deadline(self.saved["deadline"])
            check(self.saved["status"] in {"idle", "checking", "started", "expired", "error"},
                  "Saved Capitals check needs attention")
            if until:
                self.receivers(self.saved["receivers"])
                check(until <= self.now() + timedelta(seconds=WINDOW_SECONDS),
                      "Saved Capitals check needs attention")
                self.saved["status"] = "checking" if until > self.now() else "expired"
            elif self.saved["status"] == "checking":
                self.saved.update(status="error", error="The manual live game launch was interrupted; press Play to try again")
        except UserError:
            # Preserve the original file; a new explicit kick can replace bad intent.
            self.saved = {**defaults, "status": "error", "error": "Saved Capitals check needs attention"}

    def state(self):
        return {key: copy.deepcopy(self.saved[key]) for key in
                ("status", "error", "deadline", "receivers", "last_started_game", "last_watch_url")} | {
                    "catalog": self.catalog.to_dict() if self.catalog else None}

    def receivers(self, values):
        check(isinstance(values, list) and 0 < len(values) <= 8, "Choose one or more TVs")
        result = sorted({self.store.receiver(value)["id"] for value in values})
        check(len(result) == len(values), "Choose each TV once")
        return result

    def persist(self, **changes):
        previous = self.store.data.get("capitals")
        value = {**self.saved, **changes}
        self.store.data["capitals"] = value
        try:
            self.store.save()
        except BaseException:
            if previous is None:
                self.store.data.pop("capitals", None)
            else:
                self.store.data["capitals"] = previous
            raise
        self.saved = value
        self.changed()

    def active(self, generation):
        return (not self.closing and generation == self.generation
                and self.saved["status"] == "checking" and bool(self.saved["deadline"]))

    def unexpired(self, generation):
        if not self.active(generation):
            return False
        if deadline(self.saved["deadline"]) <= self.now():
            self.persist(status="expired", deadline=None, error="No live game started during this check window")
            return False
        return True

    async def stop_worker(self):
        self.generation += 1
        task, self.task = self.task, None
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def kick(self, request):
        check(isinstance(request, dict), "Choose the TVs for the live game")
        targets = self.receivers(request.get("receivers"))
        self.controller.require_mode("browser")
        check(self.store.data["setup"]["browser"].get("http_proxy"),
              "Set a VPN gateway before checking the live game")
        async with self.command_lock:
            check(not self.closing, "Live game controls are shutting down")
            # An automation redelivery must not supersede an explicit manual launch.
            if any(not task.done() for task in self.manual_requests.values()):
                return
            # QoS 1 delivery and repeated triggers must not extend the first window.
            if (self.saved["status"] == "checking" and targets == self.saved["receivers"]
                    and self.saved["deadline"] and deadline(self.saved["deadline"]) > self.now()):
                if self.task is None or self.task.done():
                    self.task = asyncio.create_task(self.run(self.generation))
                return
            await self.stop_worker()
            check(not self.closing, "Live game controls are shutting down")
            self.persist(status="checking", error="", receivers=targets,
                         deadline=iso(self.now() + timedelta(seconds=WINDOW_SECONDS)))
            self.task = asyncio.create_task(self.run(self.generation))

    async def cancel(self):
        # Invalidate queued manual intents before waiting for any lifecycle lock.
        self.manual_epoch += 1
        async with self.command_lock:
            check(not self.closing, "Live game controls are shutting down")
            await self.stop_worker()
            await self.stop_manual()
            self.persist(status="idle", error="", deadline=None)

    async def stop_pending(self, receiver_id=None):
        """Stop cancels matching intent; the caller owns actual receiver Stop."""
        if receiver_id is not None:
            receiver_id = self.store.receiver(receiver_id)["id"]
        manual = any(not task.done() and (receiver_id is None or receiver_id in key[0])
                     for key, task in self.manual_requests.items())
        automatic = (self.saved["status"] == "checking" and self.saved["deadline"]
                     and (receiver_id is None or receiver_id in self.saved["receivers"]))
        if not (manual or automatic):
            return
        self.manual_epoch += 1
        async with self.command_lock:
            await self.stop_worker()
            await self.stop_manual()
            self.persist(status="idle", error="", deadline=None)

    def start(self):
        self.closing = False
        if (self.task is None or self.task.done()) and self.saved["status"] == "checking" and self.unexpired(self.generation):
            self.task = asyncio.create_task(self.run(self.generation))

    async def close(self):
        # Shutdown cancels work without discarding an unexpired, explicit kick.
        self.closing = True
        self.manual_epoch += 1
        async with self.command_lock:
            await self.stop_worker()
            await self.stop_manual()

    async def stop_manual(self):
        tasks = list(self.manual_requests.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def manual_current(self, epoch, generation):
        check(not self.closing and epoch == self.manual_epoch, "Live game playback was cancelled")
        # Only completed, verified manual operations can advance queued intents.
        # A user Stop or another source change is never absorbed as a new baseline.
        while generation != self.controller.generation and generation in self.manual_edges:
            generation = self.manual_edges[generation]
        check(generation == self.controller.generation, "Playback was cancelled")
        return generation

    async def play_current(self, request):
        """One explicit launch, including retries; never clear automatic history."""
        check(isinstance(request, dict), "Choose the TVs for the live game")
        targets = self.receivers(request.get("receivers"))
        join = request.get("join", False)
        check(type(join) is bool, "Choose whether to join the current live game")
        epoch, generation = self.manual_epoch, self.controller.generation
        check(not self.closing, "Live game controls are shutting down")
        key = (tuple(targets), join, epoch, generation,
               tuple(repr(request.get(field)) for field in ("expected_http_proxy", "expected_http_proxy_enabled")))
        # Register before the first await so a targeted Stop can see even an
        # intent still waiting for command_lock, before any TV is attached.
        task = self.manual_requests.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self.play_manual(request, targets, join, epoch, generation))
            self.manual_requests[key] = task
            def finished(completed):
                if self.manual_requests.get(key) is completed:
                    self.manual_requests.pop(key, None)
                if not self.manual_requests:
                    self.manual_edges.clear()
                if not completed.cancelled():
                    completed.exception()  # Also observe failure if the caller disconnected.
            task.add_done_callback(finished)
        try:
            await task
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise
            raise UserError("Live game playback was cancelled") from None

    async def play_manual(self, request, targets, join, epoch, generation):
        try:
            async with asyncio.timeout(MANUAL_SECONDS), self.manual_lock:
                generation = self.manual_current(epoch, generation)
                async with self.command_lock:
                    self.manual_current(epoch, generation)
                    await self.stop_worker()
                    self.manual_current(epoch, generation)
                    self.persist(status="checking", error="", deadline=None, receivers=targets)
                catalog = await self.refresh()
                self.manual_current(epoch, generation)
                check(catalog.status != "error", catalog.error or "The live game check could not finish")
                candidates = catalog.live_candidates
                check(len(candidates) == 1, "Sportsnet lists more than one live Capitals game" if len(candidates) > 1
                      else "Sportsnet does not currently list a live Capitals game")
                event = candidates[0]
                async with self.configuration_lock, self.controller.lock:
                    self.manual_current(epoch, generation)
                    control = self.controller
                    control.require_no_diagnostic()
                    check(not (control.recordings and control.recordings.current), "Wait for the recording to finish")
                    check(not control.tearing_down, "Wait for playback to finish stopping")
                    control.require_mode("browser")
                    setup = self.store.data["setup"]
                    check(setup["complete"], "Finish Setup first")
                    check(setup["browser"].get("http_proxy"), "Set a VPN gateway before playing the live game")
                    check(self.fresh(catalog, event), "The live game check is stale; no TVs were changed")
                    play = {"mode": "browser", "browser_source": "url", "url": event.watch_url, "receivers": targets}
                    for key in ("expected_http_proxy", "expected_http_proxy_enabled"):
                        if key in request:
                            play[key] = request[key]
                    _, input_config = control.requested_source(play)
                    control.source_config(input_config)
                    active = control.targets | control.pending_targets
                    browser = control.browser
                    same = bool(control.stream and not control.pending and control.source
                                and control.source.get("kind") == "browser"
                                and control.source.get("url") == event.watch_url
                                and control.source.get("profile") == "sportsnet"
                                and browser.running and browser.profile == "sportsnet"
                                and browser.url == event.watch_url and browser_proxy_enabled(setup))
                    check(active <= set(targets) or join and same,
                          "Another TV is using AirplayVideo; no TVs were changed")
                    # Connected/connecting targets already own the requested stream.
                    # An error target remains eligible for Controller's explicit retry.
                    attached = all(target in control.targets and
                                   control.receivers.get(target, {}).get("state") in {"connecting", "streaming"}
                                   for target in targets)
                    self.persist(last_attempted_game=event.game_key, error="")
                    if same and attached and (join or control.targets == set(targets)):
                        self.persist(status="started", deadline=None, last_started_game=event.game_key,
                                     last_watch_url=event.watch_url)
                        return
                    owned_generations = {generation}
                    if not same and (control.stream or control.pending or active):
                        await control.stop()
                        check(control.generation == generation + 1, "Playback was cancelled")
                        generation += 1
                        owned_generations.add(generation)
                    self.manual_current(epoch, generation)
                    check(self.fresh(catalog, event), "The live game check expired before playback could start")
                    await self.browser_vpn.set_enabled_locked({"enabled": True})
                    self.manual_current(epoch, generation)
                    check(self.fresh(catalog, event), "The live game check expired before playback could start")
                    expected_targets = (control.targets | set(targets)) if join and same else set(targets)
                    await control.play_locked(play, add=join and same)
                    check(control.generation == generation + 1, "Playback was cancelled")
                    generation += 1
                    self.manual_current(epoch, generation)
                    check(control.stream is not None and control.targets == expected_targets, "Playback was cancelled")
                    for previous in owned_generations:
                        self.manual_edges[previous] = generation
                    self.persist(status="started", deadline=None, error="", last_started_game=event.game_key,
                                 last_watch_url=event.watch_url)
        except asyncio.CancelledError:
            if not self.closing and epoch == self.manual_epoch:
                self.persist(status="idle", deadline=None, error="")
            raise
        except Exception as exc:
            error = str(exc) if isinstance(exc, UserError) else "The live game could not start; press Play to try again"
            if not self.closing and epoch == self.manual_epoch:
                try:
                    self.persist(status="error", deadline=None, error=error)
                except Exception:
                    error = "The live game status could not be saved; check the app before trying again"
                    self.saved = {**self.saved, "status": "error", "deadline": None, "error": error}
            raise UserError(error) from None

    async def refresh(self):
        check(not self.closing, "Live game controls are shutting down")
        catalog = await self.client.fetch()
        if self.catalog is None or catalog.checked_at >= self.catalog.checked_at:
            self.catalog = catalog
            self.changed()
        return catalog

    def fresh(self, catalog, event):
        now = self.now()
        return (0 <= (now - catalog.checked_at).total_seconds() <= FRESH_SECONDS
                and event.status == "Live" and event.start <= now < min(event.end, event.start + MAX_LIVE_AGE))

    async def check_once(self, generation):
        if not self.unexpired(generation):
            return
        catalog = await self.refresh()
        if not self.unexpired(generation):
            return
        if catalog.status == "error":
            self.persist(error=catalog.error)
            return
        candidates = catalog.live_candidates
        if len(candidates) > 1:
            self.persist(status="error", deadline=None, error="Sportsnet lists more than one live Capitals game; no TVs were changed")
            return
        if not candidates:
            if self.saved["error"]:
                self.persist(error="")
            return
        event = candidates[0]
        async with self.configuration_lock, self.controller.lock:
            if not self.unexpired(generation):
                return
            control = self.controller
            diagnostics = getattr(control, "diagnostics", None)
            if ((diagnostics and diagnostics.active) or (control.recordings and control.recordings.current)
                    or (control.recordings and getattr(control.recordings, "analysis_active", False))
                    or (getattr(control, "frame_probe", None) and control.frame_probe.active)
                    or control.tearing_down):
                self.persist(error="Waiting for the recording, diagnostic, picture analysis, frame measurement, or playback teardown to finish")
                return
            targets = set(self.receivers(self.saved["receivers"]))
            control.require_mode("browser")
            check(self.store.data["setup"]["browser"].get("http_proxy"), "Set a VPN gateway before checking the live game")
            check((control.targets | control.pending_targets) <= targets,
                  "Another TV is using AirplayVideo; no TVs were changed")
            check(self.fresh(catalog, event), "The live game check is stale; no TVs were changed")
            request = {"mode": "browser", "browser_source": "url",
                       "url": event.watch_url, "receivers": sorted(targets)}
            _, input_config = control.requested_source(request)
            control.source_config(input_config)  # Validate source/encoder before stopping owned media.
            if self.saved["last_attempted_game"] == event.game_key:
                started = self.saved["last_started_game"] == event.game_key
                self.persist(status="started" if started else "error", deadline=None,
                             error="" if started else "This live game was already attempted; check playback manually")
                return
            # Claim before Stop/VPN/Play: a crash or failed start cannot loop-recast a TV.
            self.persist(last_attempted_game=event.game_key, error="")
            stopped_generation = control.generation
            if control.stream or control.pending or control.targets or control.pending_targets:
                await control.stop()
                check(control.generation == stopped_generation + 1, "Playback was cancelled")
                stopped_generation += 1
            if not self.unexpired(generation):
                return
            check(self.fresh(catalog, event), "The live game check expired before playback could start")
            await self.browser_vpn.set_enabled_locked({"enabled": True})
            if not self.unexpired(generation):
                return
            check(control.generation == stopped_generation, "Playback was cancelled")
            check(self.fresh(catalog, event), "The live game check expired before playback could start")
            await control.play_locked(request, add=False)
            if not self.active(generation):
                return
            check(control.generation == stopped_generation + 1 and control.stream is not None
                  and control.targets == targets, "Playback was cancelled")
            self.persist(status="started", deadline=None, error="", last_started_game=event.game_key,
                         last_watch_url=event.watch_url)

    async def run(self, generation):
        try:
            while self.unexpired(generation):
                await self.check_once(generation)
                if self.unexpired(generation):
                    remaining = (deadline(self.saved["deadline"]) - self.now()).total_seconds()
                    await asyncio.sleep(min(POLL_SECONDS, max(remaining, 0)))
        except asyncio.CancelledError:
            raise
        except UserError as exc:
            if self.active(generation):
                self.persist(status="error", deadline=None, error=str(exc))
        except Exception:
            if self.active(generation):
                self.persist(status="error", deadline=None, error="The live game check could not finish; check playback manually")
