import asyncio
import copy
import json
from unittest.mock import AsyncMock
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestClient, TestServer
from service.main import Application, make_app
from service.model import Store, UserError, default_setup
from service.diagnostics import Diagnostics
from service.recordings import Recordings
from test_lifecycle import setup, request, A, B


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
        async with TestClient(TestServer(make_app(app))) as client:
            assert (await client.get('/api/sync-report')).status == 403
            assert (await client.post('/api/actions/measure_sync', json={})).status == 403
        async with TestClient(TestServer(make_app(app, standalone=True))) as client:
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
