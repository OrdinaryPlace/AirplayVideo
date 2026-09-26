"""Daily direct playback keeps source replacement, Stop and ingress boundaries."""
import asyncio
import copy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer

from service import controller as module
from service.main import Application, make_app
from service.model import Store, UserError, default_setup
from service.recordings import Recordings
from test_lifecycle import A, B, request, setup


def native_request(receiver=A, **fields):
    return dict(mode='browser', browser_source='youtube', url='https://youtu.be/aqz-KE-bpKQ',
                receivers=[receiver], **fields)


@pytest.fixture
def native(setup, monkeypatch):
    store, c = setup
    for receiver in store.data['receivers']:
        receiver.update(address='192.168.250.10', model='AppleTV11,1')
    order = []
    class Session:
        created = []
        gate = None
        prepare_error = None
        connect_error = None
        def __init__(self, root, receiver, *, config, on_event):
            self.receiver, self.config, self.on_event = receiver, config, on_event
            self.closed = False
            self.entered = asyncio.Event()
            self.done = asyncio.Event()
            self.report = {'phase': 'preparing', 'error': '', 'quality': None}
            self.created.append(self)
        @property
        def status(self):
            return copy.deepcopy(self.report)
        async def prepare(self, url):
            self.url = url
            order.append('prepare')
            self.entered.set()
            self.on_event(self.status)
            if self.gate:
                await self.gate.wait()
            if self.closed:
                raise UserError('Playback was cancelled')
            if self.prepare_error:
                self.report.update(phase='failed', error=self.prepare_error)
                raise UserError(self.prepare_error)
            self.report['phase'] = 'ready'
            self.on_event(self.status)
        async def connect(self):
            order.append('connect')
            if self.connect_error:
                self.report.update(phase='failed', error=self.connect_error)
                raise UserError(self.connect_error)
            self.report['phase'] = 'playing'
            self.on_event(self.status)
        async def wait(self):
            await self.done.wait()
            return self.status
        async def close(self):
            self.closed = True
            if self.report['phase'] != 'failed':
                self.report['phase'] = 'stopped'
            self.done.set()
            if self.gate:
                self.gate.set()
        def finish(self, error=''):
            self.report.update(phase='failed' if error else 'completed', error=error)
            self.done.set()
    monkeypatch.setattr(module, 'NativeSession', Session)
    async def close_browser():
        order.append('browser_close')
        c.browser.running = False
    c.browser.close = AsyncMock(side_effect=close_browser)
    c.browser.navigate = AsyncMock()
    c.browser.start = AsyncMock()
    return store, c, Session, order


@pytest.mark.asyncio
async def test_direct_defaults_to_best_quality_and_replaces_only_after_preparation(native):
    store, c, Session, order = native
    await c.play(request())
    old = c.stream
    original_close = old.close
    async def close_old():
        order.append('old_close')
        await original_close()
    old.close = close_old
    # Native selection does not depend on the configured screen encoder or VBR.
    c.capabilities = {'encoders': []}
    before = copy.deepcopy(store.data)
    await c.play(native_request())
    active = Session.created[-1]
    assert order == ['prepare', 'old_close', 'browser_close', 'connect']
    assert active.config['max_resolution'] is None
    assert c.source['kind'] == 'youtube' and c.phase == 'playing' and c.targets == {A}
    assert c.stream is active and old.closed and c.pending is None
    assert store.data == before
    c.browser.navigate.assert_not_awaited()
    await c.stop(A)
    assert active.closed and c.phase == 'idle' and not c.targets
    assert c.native_report['phase'] == 'stopped'
    await c.close()


@pytest.mark.asyncio
async def test_preparation_failure_preserves_playing_source_and_does_not_connect(native):
    _, c, Session, order = native
    await c.play(request())
    old, source = c.stream, copy.deepcopy(c.source)
    Session.prepare_error = 'The selected 4K video needs an unavailable HEVC encoder'
    with pytest.raises(UserError, match='4K'):
        await c.play(native_request(B))
    assert c.stream is old and not old.closed and c.source == source and c.targets == {A}
    assert order == ['prepare'] and Session.created[-1].closed
    assert c.pending is None and not c.pending_targets
    await c.close()


@pytest.mark.asyncio
async def test_stop_pending_receiver_preserves_unrelated_current_receiver(native):
    _, c, Session, order = native
    await c.play(request())
    old = c.stream
    Session.gate = asyncio.Event()
    starting = asyncio.create_task(c.play(native_request(B)))
    await asyncio.sleep(0)
    pending = Session.created[-1]
    await pending.entered.wait()
    await c.stop(B)
    with pytest.raises(UserError, match='cancelled'):
        await starting
    assert pending.closed and 'connect' not in order
    assert c.stream is old and not old.closed and c.targets == {A}
    assert c.phase == 'playing' and c.pending is None
    await c.close()


@pytest.mark.asyncio
async def test_stop_all_cancels_native_preparation_without_a_receiver_command(native):
    _, c, Session, order = native
    Session.gate = asyncio.Event()
    starting = asyncio.create_task(c.play(native_request()))
    await asyncio.sleep(0)
    await Session.created[-1].entered.wait()
    await c.stop()
    with pytest.raises(UserError, match='cancelled'):
        await starting
    assert c.stream is None and c.pending is None and c.phase == 'idle'
    assert 'connect' not in order
    await c.close()


@pytest.mark.asyncio
async def test_failed_connect_clears_closed_old_source_and_completed_session_returns_idle(native):
    _, c, Session, _ = native
    await c.play(request())
    old = c.stream
    Session.connect_error = 'Native receiver connection failed'
    with pytest.raises(UserError, match='connection failed'):
        await c.play(native_request())
    assert old.closed and c.stream is None and c.source is None and not c.targets
    Session.connect_error = None
    await c.play(native_request(native_resolution='1080p'))
    active = Session.created[-1]
    assert active.config['max_resolution'] == '1080p'
    active.finish(error=None)
    await asyncio.gather(*list(c.cleanup_tasks))
    assert c.phase == 'idle' and c.stream is None and c.source is None and not c.targets
    assert c.native_report['phase'] == 'completed'
    assert c.error == ''
    await c.close()


@pytest.mark.asyncio
async def test_new_play_waits_for_stop_teardown_and_keeps_its_state(native):
    _, c, Session, order = native
    await c.play(request())
    old = c.stream
    closing, release = asyncio.Event(), asyncio.Event()
    async def delayed_close():
        closing.set()
        await release.wait()
        old.closed = True
        order.append('old_closed')
    old.close = delayed_close
    stopping = asyncio.create_task(c.stop())
    await closing.wait()
    starting = asyncio.create_task(c.play(native_request()))
    while not Session.created:
        await asyncio.sleep(0)
    await Session.created[-1].entered.wait()
    assert 'connect' not in order
    release.set()
    await asyncio.gather(stopping, starting)
    assert order.index('old_closed') < order.index('connect')
    assert c.stream is Session.created[-1] and c.targets == {A}
    assert c.source['kind'] == 'youtube' and c.phase == 'playing'
    assert c.receivers[A]['state'] == 'streaming'
    await c.close()


@pytest.mark.asyncio
async def test_cancelled_replacement_does_not_advertise_its_retiring_source(native):
    _, c, _, _ = native
    await c.play(request())
    old = c.stream
    closing, release = asyncio.Event(), asyncio.Event()
    async def delayed_close():
        closing.set()
        await release.wait()
        old.closed = True
    old.close = delayed_close
    starting = asyncio.create_task(c.play(request(channel='ABCDEF12:4.2')))
    await closing.wait()
    starting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert c.stream is None and c.source is None and not c.targets and c.phase == 'idle'
    release.set()
    await c.close()
    assert old.closed and not c.retiring


@pytest.mark.asyncio
async def test_native_policy_errors_do_not_start_or_silently_use_the_browser(native):
    store, c, Session, _ = native
    for changed, message in (({'receivers': [A, B]}, 'one TV'),
                             ({'native_resolution': '360p'}, 'quality'),
                             ({'url': 'https://youtu.be/aqz-KE-bpKQ?t=20'}, 'start time')):
        with pytest.raises(UserError, match=message):
            await c.play({**native_request(), **changed})
    store.data['setup']['audio']['enabled'] = False
    with pytest.raises(UserError, match='includes sound'):
        await c.play(native_request())
    assert not Session.created
    c.browser.navigate.assert_not_awaited()
    await c.play(native_request(delivery='browser'))
    assert c.source['kind'] == 'browser' and not Session.created
    c.browser.navigate.assert_awaited_once()
    await c.close()


@pytest.mark.asyncio
async def test_recording_rejects_native_without_touching_saved_captures(native):
    _, c, _, _ = native
    c.recordings = Recordings(c)
    await c.play(native_request())
    with pytest.raises(UserError, match='cannot be recorded'):
        await c.recordings.start({})
    assert c.recordings.current is None and c.phase == 'playing'
    await c.close()


@pytest.mark.asyncio
async def test_native_report_is_available_only_behind_existing_ingress(tmp_path):
    async with ClientSession() as session:
        store = Store(tmp_path)
        store.setup(default_setup())
        app = Application(store, session)
        app.controller.native_report = {'phase': 'stopped', 'quality': {'height': 2160}}
        web_root = Path(__file__).resolve().parents[1] / 'web'
        async with TestClient(TestServer(make_app(app, web_root=web_root))) as client:
            assert (await client.get('/api/native-report')).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True, web_root=web_root))) as client:
            response = await client.get('/api/native-report')
            assert response.status == 200 and 'attachment' in response.headers['Content-Disposition']
            assert (await response.json())['quality']['height'] == 2160
