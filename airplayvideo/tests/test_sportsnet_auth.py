"""Bounded saved-login recovery preserves ownership and never loops submits."""
import asyncio
from types import SimpleNamespace

import pytest

from service import sportsnet_auth as module
from service.model import UserError


TARGET = 'https://watch.sportsnet.ca/watch/Fixture_Capitals_123'


def page(stage, *, filled=False, matches=False, seconds=None, next_step=None):
    if next_step is None:
        next_step = {'email': 'continue', 'password': 'submit'}.get(stage, 'none')
    return {'stage': stage, 'filled': filled, 'target_matches': matches,
            'video_time': seconds, 'paused': stage != 'playing',
            'muted': False, 'fullscreen': False, 'next': next_step}


class Browser:
    def __init__(self, pages):
        self.running = True
        self.profile = 'sportsnet'
        self.companion = object()
        self.url = TARGET
        self.navigation_generation = 0
        self.pages = list(pages)
        self.reads = []
        self.autofills = []
        self.steps = []
        self.navigations = []
        self.saved = True
        self.on_read = self.on_autofill = self.on_navigate = None

    async def sportsnet_status(self, target=''):
        value = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        self.reads.append((target, value['stage']))
        if self.on_read:
            self.on_read(value)
        return dict(value)

    async def sportsnet_autofill(self, stage, target='', guard=None):
        if guard:
            guard()
        self.autofills.append((stage, target))
        if self.on_autofill:
            self.on_autofill(stage)
        return page(stage, filled=self.saved)

    async def sportsnet_step(self, stage, target='', guard=None):
        if guard:
            guard()
        self.steps.append((stage, target))
        return {**page(stage, filled=True), 'acted': True}

    async def navigate(self, target, setup, guard=None):
        if guard:
            guard()
        self.navigations.append(target)
        self.url = target
        self.navigation_generation += 1
        if self.on_navigate:
            self.on_navigate(target)


class Controller:
    def __init__(self):
        self.generation = 7
        self.source = {'kind': 'browser', 'profile': 'sportsnet', 'url': TARGET}
        self.lock = asyncio.Lock()
        self.recordings = None
        self.tearing_down = False
        self.diagnostic = False

    def require_no_diagnostic(self):
        if self.diagnostic:
            raise UserError('Wait for the diagnostic')


@pytest.fixture
def context(monkeypatch):
    # Exercise the real state machine and timeout context without spending real
    # seconds on each already-scripted page transition. Yield normally so Stop
    # and competing request tasks can still win races.
    real_sleep = asyncio.sleep

    async def short_sleep(_seconds):
        await real_sleep(0)

    monkeypatch.setattr(module.asyncio, 'sleep', short_sleep)
    monkeypatch.setattr(module, 'ATTEMPT_SECONDS', 0.2)
    control = Controller()
    store = SimpleNamespace(data={'setup': {'complete': True}})

    def make(*pages):
        browser = Browser(pages)
        updates = []
        auth = module.SportsnetAuth(store, control, browser, lambda: updates.append(auth.state()))
        return SimpleNamespace(auth=auth, control=control, browser=browser, updates=updates)

    return make


@pytest.mark.asyncio
async def test_saved_login_recovers_exact_game_after_homepage_redirect(context):
    x = context(page('signin_choice', next_step='signin_entry'),
                page('signin_choice', next_step='subscription'),
                page('email'), page('password'), page('signed_in'),
                page('playing', matches=True, seconds=5),
                page('playing', matches=True, seconds=5.4))
    await x.auth.perform(TARGET)
    assert x.auth.state() == {'status': 'playing', 'busy': False, 'available': True}
    assert [stage for stage, _ in x.browser.steps] == ['signin_choice', 'signin_choice', 'email', 'password']
    assert [stage for stage, _ in x.browser.autofills] == ['email', 'password']
    assert x.browser.navigations == [TARGET]
    assert all(target == TARGET for target, _ in x.browser.reads)
    assert x.control.generation == 7


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['email', 'password'])
async def test_missing_saved_login_never_submits(context, stage):
    x = context(page(stage))
    x.browser.saved = False
    with pytest.raises(UserError, match='saved Sportsnet login'):
        await x.auth.perform(TARGET)
    assert x.auth.status == 'saved_login_required' and not x.auth.busy
    assert x.browser.autofills == [(stage, TARGET)]
    assert not x.browser.steps and not x.browser.navigations


@pytest.mark.asyncio
@pytest.mark.parametrize('stage,status', [('verification', 'verification_required'), ('error', 'failed')])
async def test_challenge_or_provider_error_stops_before_any_credential_action(context, stage, status):
    x = context(page(stage))
    with pytest.raises(UserError):
        await x.auth.perform(TARGET)
    assert x.auth.status == status and not x.auth.busy
    assert len(x.browser.reads) == 1
    assert not x.browser.autofills and not x.browser.steps and not x.browser.navigations


@pytest.mark.asyncio
async def test_unchanged_forms_do_not_repeat_continue_or_password_submission(context):
    x = context(page('email', filled=True), page('email', filled=True),
                page('password', filled=True), page('password', filled=True), page('verification'))
    with pytest.raises(UserError):
        await x.auth.perform(TARGET)
    assert x.browser.steps == [('email', TARGET), ('password', TARGET)]
    assert not x.browser.autofills
    assert x.auth.status == 'verification_required'


@pytest.mark.asyncio
async def test_stalled_form_times_out_without_repeating_password(context, monkeypatch):
    monkeypatch.setattr(module, 'ATTEMPT_SECONDS', 0.01)
    x = context(page('password', filled=True))
    with pytest.raises(UserError, match='timed out'):
        await x.auth.perform(TARGET)
    assert x.auth.status == 'failed' and not x.auth.busy
    assert x.browser.steps == [('password', TARGET)]


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stop', 'navigation', 'companion'])
async def test_owner_change_during_page_read_prevents_later_submit(context, change):
    x = context(page('password', filled=True))

    def mutate(_value):
        if change == 'stop':
            x.control.generation += 1
        elif change == 'navigation':
            x.browser.navigation_generation += 1
        else:
            x.browser.companion = object()

    x.browser.on_read = mutate
    with pytest.raises(UserError, match='cancelled'):
        await x.auth.perform(TARGET)
    assert not x.browser.steps and not x.browser.navigations


@pytest.mark.asyncio
async def test_stop_during_autofill_prevents_continue(context):
    x = context(page('email'))
    x.browser.on_autofill = lambda _stage: setattr(x.control, 'generation', x.control.generation + 1)
    with pytest.raises(UserError, match='cancelled'):
        await x.auth.perform(TARGET)
    assert x.browser.autofills == [('email', TARGET)]
    assert not x.browser.steps and not x.browser.navigations


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['stop', 'extra_navigation', 'companion'])
async def test_return_navigation_cannot_adopt_a_concurrent_owner_change(context, change):
    x = context(page('signed_in'), page('playing', matches=True, seconds=2),
                page('playing', matches=True, seconds=2.5))

    def mutate(_target):
        if change == 'stop':
            x.control.generation += 1
        elif change == 'extra_navigation':
            x.browser.navigation_generation += 1
        else:
            x.browser.companion = object()

    x.browser.on_navigate = mutate
    with pytest.raises(UserError, match='cancelled'):
        await x.auth.perform(TARGET)
    assert x.browser.navigations == [TARGET]
    assert x.auth.status != 'playing'
    assert len(x.browser.reads) == 1


@pytest.mark.asyncio
async def test_homepage_login_does_not_invent_a_game_target(context):
    x = context(page('signed_in', matches=True))
    await x.auth.perform('https://watch.sportsnet.ca/')
    assert x.auth.status == 'signed_in' and not x.browser.navigations


@pytest.mark.asyncio
async def test_static_video_time_does_not_qualify_playing(context, monkeypatch):
    monkeypatch.setattr(module, 'ATTEMPT_SECONDS', 0.01)
    x = context(page('playing', matches=True, seconds=5))
    with pytest.raises(UserError, match='timed out'):
        await x.auth.perform(TARGET)
    assert x.auth.status == 'failed' and not x.browser.navigations


@pytest.mark.asyncio
async def test_passive_detection_attempts_once_then_latches_missing_saved_login(context, monkeypatch):
    monkeypatch.setattr(module, 'AUTO_COOLDOWN', 0)
    x = context(page('email'))
    x.browser.saved = False
    x.auth.start()
    try:
        for _ in range(100):
            if x.auth.status == 'saved_login_required' and x.auth.operation.done():
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError('Passive detection did not attempt the saved login')
        first_reads = len(x.browser.reads)
        for _ in range(30):
            await asyncio.sleep(0)
        assert len(x.browser.reads) > first_reads
        assert x.browser.autofills == [('email', TARGET)]
        assert not x.browser.steps and not x.browser.navigations
        assert x.auth.status == 'saved_login_required'
    finally:
        await x.auth.close()


@pytest.mark.asyncio
async def test_repeated_manual_requests_share_one_operation(context):
    x = context(page('verification'))
    await x.auth.request()
    operation = x.auth.operation
    await x.auth.request()
    assert x.auth.operation is operation
    await operation
    assert x.auth.status == 'verification_required'
    assert len(x.browser.reads) == 1 and not x.browser.steps
    await x.auth.close()


@pytest.mark.asyncio
async def test_prepare_cancels_a_request_queued_behind_playback_lock(context):
    x = context(page('signed_in', matches=True))
    async with x.control.lock:
        await x.auth.request()
        queued = x.auth.operation
        await x.auth.prepare('https://watch.sportsnet.ca/')
    assert queued.cancelled()
    assert x.auth.status == 'signed_in'
    assert x.browser.reads and all(target == 'https://watch.sportsnet.ca/' for target, _ in x.browser.reads)
    assert not x.browser.steps


@pytest.mark.asyncio
async def test_missing_optional_permission_preserves_manual_playback_preflight(context):
    x = context(page('permission'))
    async with x.control.lock:
        await x.auth.prepare(TARGET)
    assert x.auth.status == 'permission_required'
    assert x.auth.attempted is None and not x.auth.busy
    assert not x.browser.steps and not x.browser.autofills and not x.browser.navigations


@pytest.mark.asyncio
async def test_manual_sign_in_reports_missing_permission_without_attempting_login(context):
    x = context(page('permission'))
    await x.auth.request()
    await x.auth.operation
    assert x.auth.status == 'permission_required'
    assert not x.browser.steps and not x.browser.autofills and not x.browser.navigations
    await x.auth.close()


@pytest.mark.asyncio
async def test_permission_opt_in_keeps_same_episode_eligible_for_first_automatic_login(context, monkeypatch):
    monkeypatch.setattr(module, 'AUTO_COOLDOWN', 0)
    x = context(page('permission'))
    owner = x.auth.context()
    await x.auth.request()
    await x.auth.operation
    assert x.auth.status == 'permission_required' and x.auth.attempted is None
    assert not x.browser.autofills and not x.browser.steps
    # Granting the optional host permissions need not navigate or replace the
    # companion. A permission-only probe must not consume this login episode.
    x.browser.pages = [page('email')]
    x.browser.saved = False
    assert x.auth.context() == owner
    x.auth.start()
    try:
        for _ in range(100):
            if x.auth.status == 'saved_login_required' and x.auth.operation.done():
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError('The first authorized login was suppressed after opt-in')
        assert x.browser.autofills == [('email', TARGET)]
        assert not x.browser.steps and not x.browser.navigations
        assert x.auth.context() == owner
    finally:
        await x.auth.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('after', ['wrong_page', 'loading'])
async def test_failed_resume_after_owned_navigation_finishes_with_terminal_status(context, monkeypatch, after):
    monkeypatch.setattr(module, 'ATTEMPT_SECONDS', 0.01)
    x = context(page('signed_in'), page('signed_in' if after == 'wrong_page' else 'loading'))
    before = x.auth.context()
    await x.auth.request()
    await x.auth.operation
    assert x.browser.navigations == [TARGET]
    assert x.auth.context() == (before[0], before[1] + 1, before[2])
    assert x.auth.status == 'failed' and not x.auth.busy
    assert not x.browser.steps and not x.browser.autofills
    await x.auth.close()


@pytest.mark.asyncio
async def test_stop_during_prepare_initial_probe_prevents_adopting_new_generation(context):
    x = context(page('email', filled=True))
    before = x.control.generation
    x.browser.on_read = lambda _value: setattr(x.control, 'generation', x.control.generation + 1)
    async with x.control.lock:
        with pytest.raises(UserError, match='cancelled'):
            await x.auth.prepare(TARGET)
    assert x.control.generation == before + 1
    assert len(x.browser.reads) == 1 and x.auth.attempted is None
    assert not x.browser.autofills and not x.browser.steps and not x.browser.navigations


@pytest.mark.asyncio
@pytest.mark.parametrize('source', [
    {'kind': 'generated'}, {'kind': 'youtube'}, {'kind': 'hdhomerun'},
    {'kind': 'browser', 'profile': 'default'},
])
async def test_monitor_ignores_unrelated_active_source_with_sportsnet_browser_left_open(context, source):
    x = context(page('email'))
    x.control.source = source
    assert x.browser.running and x.browser.profile == 'sportsnet'
    x.auth.start()
    try:
        for _ in range(30):
            await asyncio.sleep(0)
        assert x.auth.status == 'unavailable' and x.auth.state()['available'] is False
        assert not x.browser.reads and not x.browser.autofills and not x.browser.steps
        assert x.auth.operation is None
    finally:
        await x.auth.close()


@pytest.mark.asyncio
async def test_stop_latches_passive_detection_until_new_explicit_navigation(context, monkeypatch):
    monkeypatch.setattr(module, 'AUTO_COOLDOWN', 0)
    x = context(page('email'))
    x.control.generation += 1
    x.auth.playback_stopped()
    x.auth.start()
    try:
        for _ in range(30):
            await asyncio.sleep(0)
        assert not x.browser.autofills and not x.browser.steps
        assert x.auth.operation is None
        x.browser.navigation_generation += 1
        x.browser.saved = False
        for _ in range(100):
            if x.auth.status == 'saved_login_required':
                break
            await asyncio.sleep(0)
        assert x.browser.autofills == [('email', TARGET)]
        assert not x.browser.steps
    finally:
        await x.auth.close()


@pytest.mark.asyncio
async def test_recording_or_unavailable_browser_blocks_manual_request(context):
    x = context(page('email'))
    x.control.recordings = SimpleNamespace(current=object())
    with pytest.raises(UserError, match='recording'):
        await x.auth.request()
    x.control.recordings = None
    x.browser.profile = 'default'
    with pytest.raises(UserError, match='Open Sportsnet'):
        await x.auth.request()
    assert not x.browser.reads and not x.browser.steps


@pytest.mark.parametrize('value', [
    'http://watch.sportsnet.ca/watch/Test_1',
    'https://watch.sportsnet.ca.evil.invalid/watch/Test_1',
    'https://user:password@watch.sportsnet.ca/watch/Test_1',
    'https://account.sportsnet.ca/login?session=fixture',
    'https://watch.sportsnet.ca/watch/Test_1?token=fixture',
    'https://watch.sportsnet.ca/watch/Test_1#fragment',
    'https://watch.sportsnet.ca:443/watch/Test_1',
    'https://watch.sportsnet.ca/watch/../account',
])
def test_return_target_rejects_authentication_or_noncanonical_addresses(value):
    assert module.sportsnet_target(value) == ''
