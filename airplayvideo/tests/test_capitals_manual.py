"""Explicit live-game retries preserve automatic history and other TVs."""
import asyncio
import copy
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest

from service import capitals as module
from service.model import Store, UserError
from service.sportsnet import SportsnetCatalog
from test_capitals import A, B, C, D, NOW, Stream, context, live


async def prepared(x, targets=(A,)):
    x.store.data['setup']['browser'].update(http_proxy_enabled=True, sportsnet_rendering='opengl')
    await x.control.play({'mode':'browser', 'browser_source':'url',
                          'url':live().watch_url, 'receivers':list(targets)})
    for target in targets:
        x.control.receivers[target]['state'] = 'streaming'
    x.browser.navigate.reset_mock()
    x.browser.start.reset_mock()
    x.browser.close.reset_mock()
    return x.control.stream


@pytest.mark.asyncio
async def test_manual_retry_preserves_history_and_settings_without_auto_resume(context):
    x = context
    x.store.data['setup']['browser']['sportsnet_rendering'] = 'opengl'
    x.launcher.persist(last_attempted_game=live().game_key)
    original = copy.deepcopy(x.store.data['setup'])
    await x.launcher.play_current({'receivers':[A]})
    assert x.control.targets == {A} and x.control.source['url'] == live().watch_url
    assert x.launcher.saved['last_attempted_game'] == live().game_key
    assert x.launcher.state()['last_started_game'] == live().game_key
    assert x.launcher.state()['status'] == 'started' and x.launcher.state()['deadline'] is None
    original['browser']['http_proxy_enabled'] = True
    assert x.store.data['setup'] == original
    assert x.launcher.task is None and x.client.fetch.await_count == 1
    await x.control.stop()
    # A later deliberate manual press may reconnect the same attempted game.
    await x.launcher.play_current({'receivers':[B]})
    assert x.control.targets == {B} and x.client.fetch.await_count == 2
    resumed = x.make(Store(x.store.root))
    resumed.launcher.start()
    assert resumed.launcher.task is None
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['none','upcoming','replay','ambiguous','stale','ended','error'])
async def test_manual_requires_exactly_one_fresh_live_game_before_any_tv_effect(context, kind):
    x = context
    events = (live(),)
    checked, status, error = NOW, 'live', ''
    if kind == 'none': events = ()
    elif kind == 'upcoming': events = (live(status='Upcoming', start=NOW+timedelta(minutes=5)),)
    elif kind == 'replay': events = (live(status='Replay'),)
    elif kind == 'ambiguous': events = (live(), live(id='456'))
    elif kind == 'stale': checked -= timedelta(minutes=3)
    elif kind == 'ended': events = (live(end=NOW),)
    elif kind == 'error': status, error = 'error', 'Sportsnet is unavailable'
    x.client.fetch.return_value = SportsnetCatalog(status, checked, events, error=error)
    before = x.control.generation
    with pytest.raises(UserError):
        await x.launcher.play_current({'receivers':[A]})
    assert not Stream.created and x.control.generation == before and not x.control.targets
    assert not x.launcher.saved['last_attempted_game']
    assert x.launcher.state()['status'] == 'error' and x.launcher.state()['deadline'] is None
    x.browser.close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('payload', [{}, {'receivers':[]}, {'receivers':[A,A]},
    {'receivers':[D+'bad']}, {'receivers':[A],'join':1}])
async def test_manual_invalid_request_does_not_cancel_or_launch(context, payload):
    x = context
    before = x.store.path.read_bytes()
    with pytest.raises(UserError):
        await x.launcher.play_current(payload)
    assert x.store.path.read_bytes() == before and not Stream.created
    x.client.fetch.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('join', [False, True])
async def test_different_source_on_an_unrequested_tv_is_untouched(context, join):
    x = context
    await x.control.play({'mode':'generated','receivers':[D]})
    old, generation = x.control.stream, x.control.generation
    with pytest.raises(UserError, match='Another TV'):
        await x.launcher.play_current({'receivers':[A], 'join':join})
    assert x.control.stream is old and not old.closed and x.control.targets == {D}
    assert x.control.generation == generation and not x.launcher.saved['last_attempted_game']
    x.browser.close.assert_not_awaited()
    await x.control.close()


@pytest.mark.asyncio
async def test_same_exact_game_joins_and_repeated_connected_press_is_a_true_noop(context):
    x = context
    old = await prepared(x, (A,))
    await x.launcher.play_current({'receivers':[B], 'join':True})
    assert x.control.stream is old and x.control.targets == {A,B}
    assert old.commands == [{'action':'add','id':A,'slot':0}, {'action':'add','id':B,'slot':1}]
    generation, commands = x.control.generation, list(old.commands)
    await x.launcher.play_current({'receivers':[B], 'join':True})
    assert x.control.generation == generation and old.commands == commands
    assert len(Stream.created) == 1 and x.control.stream is old
    x.browser.navigate.assert_not_awaited()
    x.browser.close.assert_not_awaited()
    # Errors permit retry of only that target on the same prepared stream.
    x.control.receivers[B]['state'] = 'error'
    await x.launcher.play_current({'receivers':[B], 'join':True})
    assert old.commands[-1] == {'action':'add','id':B,'slot':1} and len(old.commands) == 3
    assert x.control.targets == {A,B} and x.control.stream is old
    await x.control.close()


@pytest.mark.asyncio
async def test_exact_targets_default_rejects_removing_an_unrequested_same_game_tv(context):
    x = context
    old = await prepared(x, (A,B))
    before, commands = x.control.generation, list(old.commands)
    with pytest.raises(UserError, match='Another TV'):
        await x.launcher.play_current({'receivers':[A]})
    assert x.control.generation == before and old.commands == commands and x.control.targets == {A,B}
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('mismatch', ['source_url','browser_url','profile','route'])
async def test_join_requires_exact_source_browser_profile_and_enabled_route(context, mismatch):
    x = context
    old = await prepared(x)
    if mismatch == 'source_url': x.control.source['url'] += '_other'
    elif mismatch == 'browser_url': x.browser.url += '_other'
    elif mismatch == 'profile': x.browser.profile = 'default'
    else: x.store.data['setup']['browser']['http_proxy_enabled'] = False
    before, commands = x.control.generation, list(old.commands)
    with pytest.raises(UserError, match='Another TV'):
        await x.launcher.play_current({'receivers':[B], 'join':True})
    assert old.commands == commands and x.control.stream is old and x.control.generation == before
    x.browser.close.assert_not_awaited()
    await x.control.close()


@pytest.mark.asyncio
async def test_sequential_and_concurrent_per_tv_joins_keep_one_stream(context):
    x = context
    await asyncio.gather(x.launcher.play_current({'receivers':[A],'join':True}),
                         x.launcher.play_current({'receivers':[B],'join':True}),
                         x.launcher.play_current({'receivers':[C],'join':True}))
    assert len(Stream.created) == 1 and x.control.targets == {A,B,C}
    assert len(x.control.stream.commands) == 3 and x.client.fetch.await_count == 3
    assert x.browser.navigate.await_count == 1
    await x.control.close()


@pytest.mark.asyncio
async def test_identical_inflight_manual_presses_coalesce(context):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await entered.wait()
    second = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(first, second)
    assert x.client.fetch.await_count == 1 and len(Stream.created) == 1
    assert x.control.stream.commands == [{'action':'add','id':A,'slot':0}]
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['stop','cancel','close'])
async def test_stop_cancel_or_close_during_fetch_prevents_active_and_queued_launch(context, action):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await entered.wait()
    second = asyncio.create_task(x.launcher.play_current({'receivers':[B],'join':True}))
    await asyncio.sleep(0)
    if action == 'stop': await x.control.stop()
    elif action == 'cancel': await x.launcher.cancel()
    else: await x.launcher.close()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, UserError) for result in results)
    assert not Stream.created and not x.control.targets and not x.launcher.saved['last_started_game']
    if action == 'close':
        with pytest.raises(UserError):
            await x.launcher.play_current({'receivers':[A]})
        resumed = x.make(Store(x.store.root))
        resumed.launcher.start()
        assert resumed.launcher.task is None and resumed.launcher.state()['status'] == 'error'


@pytest.mark.asyncio
async def test_user_stop_during_last_add_invalidates_an_already_queued_join(context):
    x = context
    first_finished, release = asyncio.Event(), asyncio.Event()
    persist = x.launcher.persist
    def completion(**changes):
        persist(**changes)
        if changes.get('status') == 'started': first_finished.set()
    x.launcher.persist = completion
    # Queue B while A is awaiting its last native add, then Stop before B can fetch.
    command = Stream.command
    entered = asyncio.Event()
    async def add(self, data):
        await command(self, data)
        entered.set()
        await release.wait()
    Stream.command = add
    try:
        first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
        await entered.wait()
        second = asyncio.create_task(x.launcher.play_current({'receivers':[B],'join':True}))
        await asyncio.sleep(0)
        await x.control.stop()
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, UserError) for result in results)
        assert not x.control.targets and not x.launcher.saved['last_started_game']
        assert len(Stream.created) == 1
    finally:
        Stream.command = command


@pytest.mark.asyncio
async def test_cancel_while_last_add_awaits_never_marks_success_or_launches_queued_tv(context):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    command = Stream.command
    async def add(self, data):
        await command(self, data)
        entered.set()
        await release.wait()
    Stream.command = add
    try:
        first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
        await entered.wait()
        second = asyncio.create_task(x.launcher.play_current({'receivers':[B],'join':True}))
        await asyncio.sleep(0)
        await x.launcher.cancel()
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(result, UserError) for result in results)
        assert not x.launcher.saved['last_started_game'] and x.launcher.state()['status'] == 'idle'
        assert all(data['id'] != B for stream in Stream.created for data in stream.commands)
        await x.control.close()
    finally:
        Stream.command = command


@pytest.mark.asyncio
async def test_failed_manual_start_claim_is_persisted_but_deliberate_retry_can_succeed(context):
    x = context
    Stream.fail = True
    with pytest.raises(UserError, match='Source unavailable'):
        await x.launcher.play_current({'receivers':[A]})
    assert Store(x.store.root).data['capitals']['last_attempted_game'] == live().game_key
    assert x.launcher.state()['status'] == 'error' and not x.launcher.saved['last_started_game']
    Stream.fail = False
    await x.launcher.play_current({'receivers':[A]})
    assert x.launcher.state()['status'] == 'started' and x.control.targets == {A}
    await x.control.close()


@pytest.mark.asyncio
async def test_manual_cancels_pending_auto_and_automatic_redelivery_does_not_interrupt_it(context):
    x = context
    await x.launcher.kick({'receivers':[C]})
    auto = x.launcher.task
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        if asyncio.current_task() is not auto:
            entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    manual = asyncio.create_task(x.launcher.play_current({'receivers':[A]}))
    await entered.wait()
    assert auto.done() and x.launcher.task is None and x.launcher.state()['deadline'] is None
    await x.launcher.kick({'receivers':[C]})
    assert x.launcher.task is None
    release.set()
    await manual
    assert x.control.targets == {A}
    # Later automatic kicks retain the ordinary per-game dedupe guard.
    await x.launcher.kick({'receivers':[C]})
    await x.launcher.task
    assert x.control.targets == {A} and len(Stream.created) == 1
    await x.control.close()


@pytest.mark.asyncio
async def test_manual_rechecks_freshness_after_vpn_wait(context):
    x = context
    original = x.vpn.set_enabled_locked
    async def enabled(value):
        await original(value)
        x.clock.value = NOW + timedelta(minutes=3)
    x.vpn.set_enabled_locked = enabled
    with pytest.raises(UserError, match='expired'):
        await x.launcher.play_current({'receivers':[A]})
    assert not Stream.created and not x.launcher.saved['last_started_game']


@pytest.mark.asyncio
async def test_manual_timeout_is_safe_and_does_not_resume(context, monkeypatch):
    x = context
    monkeypatch.setattr(module, 'MANUAL_SECONDS', .01)
    x.client.fetch.side_effect = lambda: None
    async def fetch(): await asyncio.Event().wait()
    x.client.fetch.side_effect = fetch
    with pytest.raises(UserError, match='could not start'):
        await x.launcher.play_current({'receivers':[A]})
    assert x.launcher.state()['status'] == 'error' and not Stream.created
    x.launcher.start()
    assert x.launcher.task is None


@pytest.mark.asyncio
async def test_new_explicit_press_after_stop_does_not_coalesce_with_stale_inflight_work(context):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    old = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await entered.wait()
    await x.control.stop()
    new = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(old, new, return_exceptions=True)
    assert isinstance(results[0], UserError) and results[1] is None
    assert x.client.fetch.await_count == 2 and len(Stream.created) == 1 and x.control.targets == {A}
    await x.control.close()


@pytest.mark.asyncio
async def test_stop_between_verified_manual_success_and_queued_join_is_not_rebased(context):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    persist, stops = x.launcher.persist, []
    def completion(**changes):
        persist(**changes)
        if changes.get('status') == 'started' and not stops:
            # This task is scheduled before releasing manual_lock wakes the join.
            stops.append(asyncio.create_task(x.control.stop()))
    x.launcher.persist = completion
    first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await entered.wait()
    second = asyncio.create_task(x.launcher.play_current({'receivers':[B],'join':True}))
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    await asyncio.gather(*stops)
    assert results[0] is None and isinstance(results[1], UserError)
    assert not x.control.targets and len(Stream.created) == 1
    assert all(data['id'] != B for data in Stream.created[0].commands)


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['recording','diagnostic','picture_analysis','frame_probe','teardown'])
async def test_manual_busy_preflight_never_stops_or_claims_a_game(context, busy):
    from types import SimpleNamespace
    x = context
    if busy == 'recording': x.control.recordings.current = object()
    elif busy == 'diagnostic': x.control.diagnostics = SimpleNamespace(active=True)
    elif busy == 'picture_analysis': x.control.recordings.analysis_active = True
    elif busy == 'frame_probe': x.control.frame_probe = SimpleNamespace(active=True)
    else:
        retiring = asyncio.create_task(asyncio.Event().wait())
        x.control.retiring[object()] = retiring
    before = x.control.generation
    with pytest.raises(UserError):
        await x.launcher.play_current({'receivers':[A]})
    assert not Stream.created and x.control.generation == before
    assert not x.launcher.saved['last_attempted_game']
    x.browser.close.assert_not_awaited()
    if busy == 'teardown':
        retiring.cancel()
        await asyncio.gather(retiring, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_during_owned_teardown_is_not_absorbed_into_manual_generation(context):
    x = context
    await x.control.play({'mode':'generated','receivers':[A]})
    original, entered, release = x.control.stop, asyncio.Event(), asyncio.Event()
    calls = 0
    async def stop():
        nonlocal calls
        calls += 1
        await original()
        if calls == 1:
            entered.set()
            await release.wait()
    x.control.stop = stop
    manual = asyncio.create_task(x.launcher.play_current({'receivers':[A]}))
    await entered.wait()
    await stop()
    release.set()
    with pytest.raises(UserError, match='cancelled'):
        await manual
    assert len(Stream.created) == 1 and not x.control.targets and not x.launcher.saved['last_started_game']


@pytest.mark.asyncio
async def test_null_deadline_after_interrupted_manual_status_allows_a_new_auto_kick(context):
    x = context
    x.launcher.persist(status='checking', deadline=None, receivers=[A])
    await x.launcher.kick({'receivers':[A]})
    assert x.launcher.state()['deadline'] is not None and x.launcher.task is not None
    await x.launcher.stop_worker()


@pytest.mark.asyncio
@pytest.mark.parametrize('user_stop', [False, True])
async def test_join_queued_during_verified_owned_stop_uses_only_owned_generation_edges(context, user_stop):
    x = context
    await x.control.play({'mode':'generated','receivers':[A]})
    old = x.control.stream
    entered, release = asyncio.Event(), asyncio.Event()
    original = x.browser.close
    async def close():
        entered.set()
        await release.wait()
        await original()
    x.browser.close = AsyncMock(side_effect=close)
    first = asyncio.create_task(x.launcher.play_current({'receivers':[A],'join':True}))
    await entered.wait()  # Manual's own Stop has incremented generation already.
    second = asyncio.create_task(x.launcher.play_current({'receivers':[B],'join':True}))
    await asyncio.sleep(0)
    if user_stop: await x.control.stop()
    release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    if user_stop:
        assert all(isinstance(result, UserError) for result in results)
        assert not x.control.targets and len(Stream.created) == 1
    else:
        assert results == [None,None] and x.control.targets == {A,B}
        assert len(Stream.created) == 2 and old.closed
        assert len(x.control.stream.commands) == 2
    await x.control.close()


@pytest.mark.asyncio
async def test_final_status_save_failure_keeps_claim_and_explicit_retry_does_not_recast(context):
    x = context
    save, count = x.store.save, 0
    def saving():
        nonlocal count
        count += 1
        if count == 4:  # checking, claim, VPN settings, then final success.
            raise OSError('fixture-private-runtime-detail')
        save()
    x.store.save = saving
    with pytest.raises(UserError) as failure:
        await x.launcher.play_current({'receivers':[A]})
    assert 'fixture-private' not in str(failure.value)
    saved = Store(x.store.root).data['capitals']
    assert saved['status'] == 'error' and saved['last_attempted_game'] == live().game_key
    assert not saved['last_started_game'] and x.control.targets == {A}
    old, generation, commands = x.control.stream, x.control.generation, list(x.control.stream.commands)
    await x.launcher.play_current({'receivers':[A],'join':True})
    assert x.control.stream is old and x.control.generation == generation and old.commands == commands
    assert x.launcher.state()['status'] == 'started'
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('receiver', [A, D, None])
async def test_stop_pending_matches_manual_target_without_stopping_playback_itself(context, receiver):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    manual = asyncio.create_task(x.launcher.play_current({'receivers':[A]}))
    await entered.wait()
    generation = x.control.generation
    await x.launcher.stop_pending(receiver)
    assert x.control.generation == generation  # Helper never invokes receiver Stop.
    await x.control.stop(receiver)
    release.set()
    if receiver == D:
        await manual
        assert x.control.targets == {A} and len(Stream.created) == 1
        await x.control.close()
    else:
        with pytest.raises(UserError, match='cancelled'):
            await manual
        assert not Stream.created and x.launcher.state()['status'] == 'idle'


@pytest.mark.asyncio
@pytest.mark.parametrize('receiver', [False, '', 'invalid', 'e'*32])
async def test_invalid_targeted_stop_does_not_cancel_pending_manual_intent(context, receiver):
    x = context
    entered, release = asyncio.Event(), asyncio.Event()
    async def fetch():
        entered.set()
        await release.wait()
        return SportsnetCatalog('live', NOW, (live(),))
    x.client.fetch.side_effect = fetch
    manual = asyncio.create_task(x.launcher.play_current({'receivers':[A]}))
    await entered.wait()
    epoch, before = x.launcher.manual_epoch, x.store.path.read_bytes()
    with pytest.raises(UserError):
        await x.launcher.stop_pending(receiver)
    assert x.launcher.manual_epoch == epoch and x.store.path.read_bytes() == before
    release.set()
    await manual
    assert x.control.targets == {A}
    await x.control.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('receiver,matched', [(A,True),(D,False),(None,True)])
async def test_stop_pending_matches_automatic_intent_and_preserves_history(context, receiver, matched):
    x = context
    x.launcher.persist(last_attempted_game='previous-attempt', last_started_game='previous-start')
    await x.launcher.kick({'receivers':[A]})
    await x.launcher.stop_pending(receiver)
    assert x.launcher.saved['last_attempted_game'] == 'previous-attempt'
    assert x.launcher.saved['last_started_game'] == 'previous-start'
    assert (x.launcher.state()['status'] == 'idle') is matched
    assert (x.launcher.state()['deadline'] is None) is matched
    assert not Stream.created
    await x.launcher.stop_worker()


@pytest.mark.asyncio
async def test_targeted_stop_can_see_manual_intent_waiting_for_command_lock(context):
    x = context
    await x.launcher.command_lock.acquire()
    manual = asyncio.create_task(x.launcher.play_current({'receivers':[A]}))
    await asyncio.sleep(0)
    stopping = asyncio.create_task(x.launcher.stop_pending(A))
    await asyncio.sleep(0)
    x.launcher.command_lock.release()
    await stopping
    with pytest.raises(UserError, match='cancelled'):
        await manual
    assert not Stream.created
