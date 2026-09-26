import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from service.main import Application, make_app
from service.model import Store, UserError, default_setup
from service.diagnostics import Diagnostics
from service import diagnostics as module
from service.recordings import Recordings
from test_lifecycle import setup, request, A, B

WEB = Path(__file__).resolve().parents[1] / 'web'


@pytest.mark.asyncio
async def test_measurement_is_exclusive_cancellable_and_preserves_state(setup, monkeypatch):
    store, c = setup
    c.browser.running = False
    c.recordings = Recordings(c)
    for i in range(8):
        (c.recordings.root / (str(i)*32 + '.json')).write_text('{}')
    c.diagnostics = d = Diagnostics(c, None)
    gate = asyncio.Event()
    observed = []
    async def measure(root, config, receivers):
        observed.append((root, config, receivers))
        (root / 'generated').write_text('temporary fixture')
        await gate.wait()
    monkeypatch.setattr(d, 'measure', measure)
    before = copy.deepcopy(store.data)
    await d.start({'receivers': [A]})
    await asyncio.sleep(0)
    assert d.active and not c.targets and c.stream is None
    with pytest.raises(UserError, match='already running'): await d.start({})
    with pytest.raises(UserError, match='sync measurement'): await c.play(request())
    with pytest.raises(UserError, match='sync measurement'): await c.recordings.start({})
    with pytest.raises(UserError, match='sync measurement'): await c.open_browser({})
    await c.stop(B)
    assert d.active
    await c.stop(A)
    assert not d.active and d.report['status'] == 'cancelled'
    assert observed[0][1]['width'] == 1920 and observed[0][1]['height'] == 1080
    assert not observed[0][0].exists()
    assert store.data == before and len(list(c.recordings.root.glob('*.json'))) == 8
    assert json.loads(d.path.read_text())['physical_output_measured'] is False


@pytest.mark.asyncio
async def test_measurement_rejects_unknown_targets_and_busy_sources(setup):
    _, c = setup
    d = Diagnostics(c, None)
    with pytest.raises(UserError, match='Close the browser'): await d.start({})
    c.browser.running = False
    for receivers in ([A, A], ['f'*32], 'all'):
        with pytest.raises(UserError): await d.start({'receivers': receivers})
    await c.play(request())
    with pytest.raises(UserError, match='Stop playback'): await d.start({})
    await c.stop()
    assert not d.active and d.report is None


@pytest.mark.asyncio
async def test_reports_and_commands_keep_ingress_boundary(tmp_path, monkeypatch):
    async with ClientSession() as session:
        store = Store(tmp_path)
        store.setup(default_setup())
        app = Application(store, session)
        monkeypatch.setattr(app.diagnostics, 'measure', AsyncMock())
        async with TestClient(TestServer(make_app(app, web_root=WEB))) as client:
            assert (await client.get('/api/sync-report')).status == 403
            assert (await client.post('/api/actions/measure_sync', json={})).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True, web_root=WEB))) as client:
            assert (await client.post('/api/actions/measure_sync', json={})).status == 403
            result = await client.post('/api/actions/measure_sync', json={}, headers={'X-AirplayVideo': '1'})
            assert result.status == 200
            await app.diagnostics.task
            result = await client.get('/api/sync-report')
            assert result.status == 200 and 'attachment' in result.headers['Content-Disposition']
            assert (await result.json())['status'] == 'complete'
            assert app.diagnostics.path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_failure_retains_completed_stages_and_releases_lock(setup, monkeypatch):
    _, c = setup
    c.browser.running = False
    c.diagnostics = d = Diagnostics(c, None)
    async def fail(root, config, receivers):
        d.report['stages']['fixture'] = {'measured': True}
        raise OSError('private path or data must not escape')
    monkeypatch.setattr(d, 'measure', fail)
    await d.start({})
    await d.task
    assert d.report['status'] == 'failed' and d.report['stages']['fixture']['measured']
    assert 'private path' not in d.path.read_text()
    await c.play(request())
    await c.stop()


@pytest.fixture
def native_trial(setup, monkeypatch):
    store, c = setup
    c.browser.running = False
    c.recordings = Recordings(c)
    store.data['receivers'][0]['address'] = '192.168.250.10'
    store.data['receivers'][1]['address'] = '192.168.250.11'
    c.diagnostics = d = Diagnostics(c, None)
    calls, origins, players = [], [], []

    async def command(*args, **kwargs):
        calls.append(args)
        if '--fixture' in args or args[0] == 'ffmpeg':
            args[-1].write_bytes(b'generated sample')
        return b'{"private": "raw probe output must not be saved"}\n'

    class Origin:
        def __init__(self, files, allowed, **options):
            self.files, self.allowed, self.options = files, allowed, options
            self.closed = asyncio.Event()
            self.started = False
            origins.append(self)
        async def start(self):
            self.started = True
        def url(self, name):
            return 'http://192.168.250.2:3456/random-private-token/' + name
        def counters(self):
            return {'receiver': {'requests': 3, 'bytes_sent': 12345, 'headers': 'private'},
                    'preflight': {'requests': 0, 'bytes_sent': 0}, 'private': 'forbidden'}
        async def close(self):
            self.closed.set()

    class Player:
        def __init__(self, root, receiver_id, url, **options):
            self.root, self.receiver_id, self.url, self.options = root, receiver_id, url, options
            self.entered, self.finished, self.closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
            self.failed = False
            self.close_gate = None
            players.append(self)
        async def start(self):
            self.entered.set()
            self.options['on_event']({'event': 'native_stage', 'details': {
                'stage': 'insert', 'status': 200, 'elapsed_ms': 42,
                'headers': 'pairing secret', 'url': self.url}})
        async def wait(self):
            await self.finished.wait()
            if self.failed:
                raise RuntimeError('raw receiver response or private path')
        async def close(self):
            if self.close_gate:
                await self.close_gate.wait()
            self.closed.set()

    monkeypatch.setattr(module, 'command', command)
    monkeypatch.setattr(module, 'MediaOrigin', Origin)
    monkeypatch.setattr(module, 'NativeEnginePlayer', Player)
    monkeypatch.setattr(module, 'native_bind_address', lambda _: '192.168.250.2')
    return store, c, d, calls, origins, players


async def wait_native_player(players):
    async with asyncio.timeout(1):
        while not players:
            await asyncio.sleep(0)
        await players[0].entered.wait()
    return players[0]


@pytest.mark.asyncio
async def test_native_trial_has_one_bound_target_and_private_transport_report(native_trial):
    store, c, d, calls, origins, players = native_trial
    before = copy.deepcopy(store.data)
    await d.start({'kind': 'native', 'receivers': [A], 'seconds': 999})
    player = await wait_native_player(players)
    assert player.root == store.root and player.receiver_id == A
    assert player.options['receiver_address'] == '192.168.250.10'
    assert player.options['seconds'] == 30
    origin = origins[0]
    assert origin.allowed == origin.options['receiver_clients'] == {'192.168.250.10'}
    assert origin.options['bind_address'] == '192.168.250.2' and origin.options['lifetime'] == 30
    assert list(origin.files) == ['reference.mp4']
    assert calls[0][1] == '--fixture' and calls[1][0] == 'ffmpeg' and calls[2][1] == '--reference'
    assert calls[1][calls[1].index('-c:v') + 1] == 'copy'
    assert calls[1][calls[1].index('-c:a') + 1] == 'aac'
    assert c.stream is None and not c.targets and not c.browser.running
    player.finished.set()
    await d.task
    assert player.closed.is_set() and origin.closed.is_set()
    assert not origin.files['reference.mp4'].parent.exists()
    assert store.data == before
    report = json.loads(d.path.read_text())
    assert report['kind'] == 'native' and report['status'] == 'complete'
    assert report['physical_output_measured'] is False and report['physical_observation'] == 'pending'
    delivery = report['stages']['native_delivery']
    assert delivery['counters'] == {'receiver': {'requests': 3, 'bytes_sent': 12345},
                                     'preflight': {'requests': 0, 'bytes_sent': 0}}
    assert delivery['ended'] == 'worker_finished'
    assert report['stages']['native_events'] == [{'event': 'native_stage', 'details': {
        'stage': 'insert', 'status': 200, 'elapsed_ms': 42}}]
    raw = d.path.read_text()
    for private in ('192.168.', 'random-private-token', 'pairing secret', 'raw probe output', 'headers'):
        assert private not in raw
    assert d.path.stat().st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_native_trial_shares_exclusivity_and_selected_target_stop(native_trial):
    _, c, d, _, origins, players = native_trial
    await d.start({'kind': 'native', 'receivers': [A]})
    player = await wait_native_player(players)
    with pytest.raises(UserError, match='already running'):
        await d.start({})
    with pytest.raises(UserError):
        await c.play(request())
    with pytest.raises(UserError):
        await c.recordings.start({})
    with pytest.raises(UserError):
        await c.open_browser({})
    await c.stop(B)
    assert d.active and not player.closed.is_set()
    await c.stop(A)
    assert not d.active and d.report['status'] == 'cancelled'
    assert player.closed.is_set() and origins[0].closed.is_set()
    assert not origins[0].files['reference.mp4'].parent.exists()


@pytest.mark.asyncio
async def test_native_trial_rejects_invalid_targets_and_busy_sources(native_trial):
    store, c, d, _, origins, players = native_trial
    for wanted in ([], [A, B], [A, A], ['f' * 32], [[A]], 'all'):
        with pytest.raises(UserError):
            await d.start({'kind': 'native', 'receivers': wanted})
    for address in (None, '::1', '8.8.8.8', 'example.com'):
        store.data['receivers'][0]['address'] = address
        with pytest.raises(UserError):
            await d.start({'kind': 'native', 'receivers': [A]})
    store.data['receivers'][0]['address'] = '192.168.250.10'
    c.browser.running = True
    with pytest.raises(UserError, match='Close the browser'):
        await d.start({'kind': 'native', 'receivers': [A]})
    c.browser.running = False
    c.pending = {'source': 'test'}
    with pytest.raises(UserError, match='Stop playback'):
        await d.start({'kind': 'native', 'receivers': [A]})
    c.pending = None
    c.recordings.current = {'status': 'recording'}
    with pytest.raises(UserError, match='recording'):
        await d.start({'kind': 'native', 'receivers': [A]})
    assert not d.active and not origins and not players


@pytest.mark.asyncio
async def test_native_trial_deadline_closes_player_and_origin(native_trial, monkeypatch):
    _, _, d, _, origins, players = native_trial
    monkeypatch.setattr(module, 'NATIVE_SECONDS', .01)
    await d.start({'kind': 'native', 'receivers': [A]})
    player = await wait_native_player(players)
    await asyncio.wait_for(d.task, 1)
    assert player.closed.is_set() and origins[0].closed.is_set()
    assert d.report['stages']['native_delivery']['ended'] == 'time_limit'
    assert d.report['status'] == 'complete' and not d.report['physical_output_measured']


@pytest.mark.asyncio
async def test_repeated_native_cancel_waits_for_cleanup(native_trial):
    _, _, d, _, origins, players = native_trial
    await d.start({'kind': 'native', 'receivers': [A]})
    player = await wait_native_player(players)
    player.close_gate = asyncio.Event()
    first = asyncio.create_task(d.close())
    await asyncio.sleep(0)
    second = asyncio.create_task(d.close())
    await asyncio.sleep(0)
    assert d.active and not player.closed.is_set()
    assert origins[0].files['reference.mp4'].parent.exists()
    player.close_gate.set()
    await asyncio.gather(first, second)
    assert not d.active and player.closed.is_set() and origins[0].closed.is_set()


@pytest.mark.asyncio
async def test_native_cancel_before_background_start_saves_final_state(native_trial):
    _, _, d, calls, origins, players = native_trial
    await d.start({'kind': 'native', 'receivers': [A]})
    await d.close()
    assert not d.active and not calls and not origins and not players
    assert json.loads(d.path.read_text())['status'] == 'cancelled'


@pytest.mark.asyncio
async def test_diagnostic_command_adopts_and_reaps_child_when_spawn_is_cancelled(monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    process = SimpleNamespace(returncode=None, communicate=AsyncMock(), kill=Mock(), wait=AsyncMock())
    async def spawn(*args, **kwargs):
        entered.set()
        await release.wait()
        return process
    monkeypatch.setattr(module.asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(module.command('generated-fixture-only'))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    process.communicate.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_start_failure_closes_origin_and_removes_fixture(native_trial, monkeypatch):
    _, _, d, _, origins, players = native_trial
    async def failed_start(self):
        raise RuntimeError('private native configuration path')
    monkeypatch.setattr(module.NativeEnginePlayer, 'start', failed_start)
    await d.start({'kind': 'native', 'receivers': [A]})
    await d.task
    assert d.report['status'] == 'failed'
    assert players[0].closed.is_set() and origins[0].closed.is_set()
    assert not origins[0].files['reference.mp4'].parent.exists()
    assert 'private native configuration' not in d.path.read_text()


@pytest.mark.asyncio
async def test_native_failure_redacts_output_and_retains_transport(native_trial):
    _, _, d, _, origins, players = native_trial
    await d.start({'kind': 'native', 'receivers': [A]})
    player = await wait_native_player(players)
    player.failed = True
    player.finished.set()
    await d.task
    assert d.report['status'] == 'failed'
    assert d.report['stages']['native_delivery']['engine_failed'] is True
    assert d.report['stages']['native_events']
    assert player.closed.is_set() and origins[0].closed.is_set()
    assert 'raw receiver' not in d.path.read_text() and 'private path' not in d.path.read_text()


@pytest.mark.asyncio
async def test_native_probe_failure_does_not_persist_arbitrary_child_json(native_trial, monkeypatch):
    _, _, d, _, origins, players = native_trial
    async def failure(*args, **kwargs):
        raise module.MeasurementFailed(1, b'{"private":"credential material"}')
    monkeypatch.setattr(module, 'command', failure)
    await d.start({'kind': 'native', 'receivers': [A]})
    await d.task
    assert d.report['status'] == 'failed' and d.report['failed_process'] == {'exit_code': 1}
    assert 'credential material' not in d.path.read_text()
    assert not origins and not players


@pytest.mark.asyncio
async def test_native_action_requires_ingress_and_cannot_be_selected_by_sync_action(tmp_path, monkeypatch):
    async with ClientSession() as session:
        store = Store(tmp_path)
        store.data['receivers'] = [dict(id=A, name='TV A', slot=0, address='192.168.250.10')]
        store.setup(default_setup())
        app = Application(store, session)
        sync, native = AsyncMock(), AsyncMock()
        monkeypatch.setattr(app.diagnostics, 'measure', sync)
        monkeypatch.setattr(app.diagnostics, 'measure_native', native)
        async with TestClient(TestServer(make_app(app, web_root=WEB))) as client:
            assert (await client.post('/api/actions/test_native_video', json={'receivers': [A]},
                                      headers={'X-AirplayVideo': '1'})).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True, web_root=WEB))) as client:
            assert (await client.post('/api/actions/test_native_video', json={'receivers': [A]})).status == 403
            result = await client.post('/api/actions/test_native_video',
                                      json={'receivers': [A], 'kind': 'sync'}, headers={'X-AirplayVideo': '1'})
            assert result.status == 200
            await app.diagnostics.task
            native.assert_awaited_once()
            sync.assert_not_awaited()
            report = await client.get('/api/sync-report')
            assert (await report.json())['kind'] == 'native'
            result = await client.post('/api/actions/measure_sync', json={'kind': 'native'},
                                      headers={'X-AirplayVideo': '1'})
            assert result.status == 200
            await app.diagnostics.task
            sync.assert_awaited_once()
            assert native.await_count == 1 and app.diagnostics.report['kind'] == 'sync'
