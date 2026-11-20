"""Bounded Sportsnet UI recovery; saved credentials never leave Chrome."""
from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import urlsplit

from .model import UserError, check


STATUSES = frozenset({"unavailable", "checking", "sign_in_required", "signing_in",
                      "signed_in", "playing", "verification_required",
                      "saved_login_required", "permission_required", "failed"})
LOGIN_STAGES = frozenset({"signin_choice", "email", "password"})
POLL_SECONDS = 5
ATTEMPT_SECONDS = 60
AUTO_COOLDOWN = 600


def sportsnet_target(value):
    """Only public playback addresses may be retained as a login return target."""
    if not isinstance(value, str) or len(value) > 512:
        return ""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.netloc != "watch.sportsnet.ca"
                or url.query or url.fragment):
            return ""
        if url.path in {"", "/"}:
            return "https://watch.sportsnet.ca/"
        if re.fullmatch(r"/(?:watch|channel)/[A-Za-z0-9_-]{1,200}", url.path):
            return "https://watch.sportsnet.ca" + url.path
    except ValueError:
        pass
    return ""


class SportsnetAuth:
    def __init__(self, store, controller, browser, changed=lambda: None):
        self.store, self.controller, self.browser = store, controller, browser
        self.changed = changed
        self.status = "unavailable"
        self.busy = False
        self.monitor = None
        self.operation = None
        self.lock = asyncio.Lock()
        self.closing = False
        self.attempted = None
        self.last_attempt = float("-inf")
        self.last_success = False
        self.stopped_navigation = None
        self.operation_owner = None

    @property
    def available(self):
        return bool(self.browser.running and getattr(self.browser, "profile", "default") == "sportsnet"
                    and getattr(self.browser, "companion", None))

    def state(self):
        return {"status": self.status if self.status in STATUSES else "failed",
                "busy": bool(self.busy), "available": self.available and self.status != "unavailable"}

    def set_status(self, status):
        check(status in STATUSES, "Invalid Sportsnet sign-in state")
        if self.status != status:
            self.status = status
            self.changed()

    def start(self):
        self.closing = False
        if self.monitor is None:
            self.monitor = asyncio.create_task(self.watch())

    def context(self):
        return (self.controller.generation, getattr(self.browser, "navigation_generation", 0),
                id(getattr(self.browser, "companion", None)))

    def target(self):
        source = self.controller.source
        if source and source.get("kind") == "browser" and source.get("profile") == "sportsnet":
            return sportsnet_target(source.get("url"))
        return sportsnet_target(getattr(self.browser, "url", ""))

    def require_ready(self):
        check(not self.closing and self.available, "Open Sportsnet in the browser first")
        self.controller.require_no_diagnostic()
        check(not (self.controller.recordings and self.controller.recordings.current),
              "Wait for the diagnostic recording to finish")
        check(not self.controller.tearing_down, "Wait for playback to finish stopping")

    def current(self, owner):
        self.require_ready()
        check(owner == self.context(), "Sportsnet sign-in was cancelled by another browser or playback action")

    async def request(self):
        """UI/MQTT starts one bounded task; repeated presses share that task."""
        self.require_ready()
        if self.operation is not None and not self.operation.done() or self.busy:
            return
        owner, target = self.context(), self.target()
        self.stopped_navigation = None
        self.set_status("checking")
        self.operation = asyncio.create_task(self.requested(owner, target))

    async def requested(self, owner, target):
        try:
            async with self.controller.lock, self.lock:
                self.current(owner)
                await self.perform(target)
        except asyncio.CancelledError:
            raise
        except (UserError, OSError):
            # Never persist a raw browser/provider error or an account identifier.
            if owner == self.context() and self.status not in {
                    "verification_required", "saved_login_required", "failed"}:
                self.set_status("failed")
        except Exception:
            self.set_status("failed")

    async def prepare(self, target):
        """Called under Controller.lock, before any requested TV is attached."""
        target = sportsnet_target(target)
        if not target:
            return
        owner = self.context()
        self.current(owner)
        self.stopped_navigation = None
        # An older queued request must not later act on this new navigation.
        if self.operation is not None and not self.operation.done():
            self.operation.cancel()
            await asyncio.gather(self.operation, return_exceptions=True)
            self.current(owner)
        async with self.lock:
            self.current(owner)
            # Optional page permissions preserve the existing companion during
            # upgrades. Normal manual playback remains available before opt-in.
            initial = await self.browser.sportsnet_status(target)
            self.current(owner)
            if initial["stage"] == "permission":
                self.set_status("permission_required")
                return
            await self.perform(target, owner)

    async def enable(self):
        self.require_ready()
        async with self.controller.lock:
            owner = self.context()
            await self.browser.sportsnet_enable(guard=lambda: self.current(owner))
            self.set_status("permission_required")

    async def perform(self, target, owner=None):
        owner = self.context() if owner is None else owner
        self.current(owner)
        self.attempted, self.last_attempt = owner, time.monotonic()
        self.last_success = False
        self.busy = True
        self.changed()
        try:
            async with asyncio.timeout(ATTEMPT_SECONDS):
                await self.workflow(target, owner)
                self.last_success = self.status in {"signed_in", "playing"}
                self.attempted = self.context() if self.status != "permission_required" else None
        except TimeoutError:
            self.set_status("failed")
            raise UserError("Sportsnet sign-in timed out; check the browser preview") from None
        except (UserError, OSError):
            if self.operation_owner == self.context() and self.status not in {
                    "verification_required", "saved_login_required", "permission_required", "failed"}:
                self.set_status("failed")
            raise
        finally:
            self.busy = False
            self.changed()

    async def workflow(self, target, owner):
        self.operation_owner = owner
        self.set_status("checking")
        actions = set()
        resumed = False
        saw_login = False
        first_video_time = None
        while True:
            self.current(owner)
            result = await self.browser.sportsnet_status(target)
            self.current(owner)
            stage = result["stage"]
            if stage == "permission":
                self.set_status("permission_required")
                return
            if stage == "verification":
                self.set_status("verification_required")
                raise UserError("Sportsnet needs verification; finish it in the browser preview")
            if stage == "error":
                self.set_status("failed")
                raise UserError("Sportsnet could not sign in; check the browser preview before retrying")
            if stage == "other":
                self.set_status("unavailable")
                raise UserError("Sportsnet sign-in stopped because the browser page changed")
            if stage in LOGIN_STAGES:
                saw_login = True
                self.set_status("signing_in")
                if stage in {"email", "password"} and not result["filled"]:
                    key = (stage, "autofill")
                    if key not in actions:
                        actions.add(key)
                        self.current(owner)
                        result = await self.browser.sportsnet_autofill(stage, target, guard=lambda: self.current(owner))
                        self.current(owner)
                    if result["stage"] == stage and not result["filled"]:
                        self.set_status("saved_login_required")
                        raise UserError("Choose or enter the saved Sportsnet login in the browser preview")
                    if result["stage"] != stage:
                        await asyncio.sleep(0.25)
                        continue
                key = (stage, result.get("next", "none"))
                if key not in actions:
                    # Consume intent before dispatch: a lost ACK must not resubmit.
                    actions.add(key)
                    self.current(owner)
                    await self.browser.sportsnet_step(stage, target, guard=lambda: self.current(owner))
                    self.current(owner)
            elif stage in {"signed_in", "playing"}:
                # Login redirects can leave Browser.url pointing at the old game.
                # Compare the actual page before deliberately returning exactly once.
                if target and not result["target_matches"]:
                    check(not resumed, "Sportsnet did not return to the requested page; use the browser preview")
                    self.current(owner)
                    await self.browser.navigate(target, self.store.data["setup"], guard=lambda: self.current(owner))
                    new_owner = self.context()
                    check(new_owner == (owner[0], owner[1] + 1, owner[2]),
                          "Sportsnet sign-in was cancelled by another browser or playback action")
                    owner = new_owner
                    self.operation_owner = owner
                    self.attempted = owner
                    resumed = True
                    first_video_time = None
                    continue
                if not target or target == "https://watch.sportsnet.ca/":
                    self.set_status("signed_in")
                    return
                value = result.get("video_time")
                if stage == "playing" and isinstance(value, (int, float)) and not result.get("paused", True):
                    if first_video_time is not None and value > first_video_time + 0.1:
                        self.set_status("playing")
                        return
                    first_video_time = value
                else:
                    first_video_time = None
                    self.set_status("signed_in")
            elif stage not in {"loading", "unknown"}:
                self.set_status("failed")
                raise UserError("Sportsnet sign-in needs attention in the browser preview")
            # A challenge or failed password never leads to another submission.
            # The overall deadline also bounds unchanged forms and SPA loading.
            await asyncio.sleep(0.5 if saw_login else 0.75)

    async def watch(self):
        try:
            while not self.closing:
                try:
                    if not self.available:
                        self.set_status("unavailable")
                    elif not self.busy and not (self.operation and not self.operation.done()) and not self.controller.lock.locked():
                        source = self.controller.source
                        if source and (source.get("kind") != "browser" or source.get("profile") != "sportsnet"):
                            self.set_status("unavailable")
                            await asyncio.sleep(POLL_SECONDS)
                            continue
                        self.require_ready()
                        owner, target = self.context(), self.target()
                        result = await self.browser.sportsnet_status(target)
                        if owner == self.context():
                            stage = result["stage"]
                            if stage in LOGIN_STAGES:
                                navigation = owner[1:]
                                eligible = (owner != self.attempted or self.last_success
                                            and time.monotonic() - self.last_attempt >= AUTO_COOLDOWN)
                                if eligible and navigation != self.stopped_navigation:
                                    self.set_status("sign_in_required")
                                    await self.request()
                                elif self.status not in {"verification_required", "saved_login_required", "failed"}:
                                    self.set_status("sign_in_required")
                            elif stage == "verification":
                                self.set_status("verification_required")
                            elif stage == "error":
                                self.set_status("failed")
                            elif stage in {"signed_in", "playing"}:
                                self.set_status(stage)
                            elif stage == "permission":
                                self.set_status("permission_required")
                            elif stage == "other":
                                self.set_status("unavailable")
                            elif stage in {"unknown", "loading"}:
                                self.set_status("checking")
                except (UserError, OSError):
                    pass  # Diagnostics/navigation can temporarily own the browser.
                except Exception:
                    self.set_status("failed")
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise

    def playback_stopped(self):
        # Stop cancels the current episode and cannot cause an automatic retry
        # on its new playback generation. Explicit Sign in/Play/navigation can.
        self.stopped_navigation = self.context()[1:]
        self.attempted = self.context()
        self.last_success = False

    async def close(self):
        self.closing = True
        tasks = [task for task in (self.operation, self.monitor) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.operation = self.monitor = None
