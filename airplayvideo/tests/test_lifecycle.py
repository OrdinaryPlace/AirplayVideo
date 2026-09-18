"""Behavioral checks for Stop, source replacement, and native HA commands."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from aiohttp import web, ClientSession
from aiohttp.test_utils import TestServer, TestClient
from service import controller as module
from service.controller import Controller, PairingEngine
from service.main import Application, make_app
from service.model import Store, UserError, default_setup
from service.mqtt import HomeAssistant

A, B = 'a'*32, 'b'*32

class Browser:
    running = True
    environment = {"DISPLAY": ":42.0"}
    navigate = AsyncMock()
    start = AsyncMock()
    close = AsyncMock()

class Channels:
    error = ''
    rows = [dict(id='ABCDEF12:4.1', label='4.1 Fixture', supported=True, _url='http://192.168.250.10:5004/auto/v4.1'),
            dict(id='ABCDEF12:4.2', label='4.2 Fixture', supported=True, _url='http://192.168.250.10:5004/auto/v4.2')]
    def get(self, key):
        row = next((r for r in self.rows if r['id'] == key), None)
        if not row: raise UserError('Choose a channel')
        return row
    def public(self):
        return [{k:v for k,v in row.items() if not k.startswith('_')} for row in self.rows]

class Stream:
    created = []
    fail = False
    gate = None
    def __init__(self, *_):
        self.config = _[1]
        self.closed = False
        self.commands = []
        self.entered = asyncio.Event()
        Stream.created.append(self)
    async def start(self):
        self.entered.set()
        if self.gate: await self.gate.wait()
        if self.fail: raise UserError('Source unavailable')
    async def command(self, data):
        assert not self.closed
        self.commands.append(data)
    async def close(self):
        self.closed = True
        if self.gate: self.gate.set()

@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = Store(tmp_path)
    store.data['receivers'] = [dict(id=A,name='TV A',slot=0),dict(id=B,name='TV B',slot=1)]
    config = default_setup()
    config['modes']['hdhomerun'] = True
    config['hdhomerun']['devices'] = [dict(id='ABCDEF12',address='192.168.250.10',name='Fixture')]
    store.setup(config)
    Stream.created, Stream.fail, Stream.gate = [], False, None
    monkeypatch.setattr(module,'Stream',Stream)
    control = Controller(store,None,Browser(),Channels())
    return store,control

def request(receiver=A, channel='ABCDEF12:4.1'):
    return dict(mode='hdhomerun', channel=channel, receivers=[receiver])

@pytest.mark.asyncio
async def test_browser_capture_uses_the_allocated_display(setup):
    _, control = setup
    await control.play(dict(mode='browser', browser_source='url', url='https://example.com', receivers=[A]))
    assert Stream.created[-1].config['source']['display'] == ':42.0'
    await control.stop()

@pytest.mark.asyncio
async def test_join_shares_source_and_stop_only_removes_requested_tv(setup):
    _, control = setup
    await control.play(request())
    first = control.stream
    await control.play(request(B), add=True)
    assert control.stream is first and len(Stream.created) == 1
    await control.stop(A)
    assert not first.closed and control.targets == {B}
    assert first.commands[-1] == dict(action='remove',id=A)
    await control.stop(A)  # an idle TV must never stop another viewer
    assert not first.closed and control.targets == {B}
    await control.stop(B)
    assert first.closed and control.stream is None and control.phase == 'idle'

@pytest.mark.asyncio
async def test_bad_replacement_keeps_current_source(setup):
    _, control = setup
    await control.play(request())
    first = control.stream
    Stream.fail = True
    with pytest.raises(UserError): await control.play(request(channel='ABCDEF12:4.2'))
    assert control.stream is first and not first.closed
    assert control.source['key'] == 'ABCDEF12:4.1'
    assert Stream.created[-1].closed and control.pending is None
    await control.stop()

@pytest.mark.asyncio
async def test_stop_cancels_source_warmup_without_orphan(setup):
    _, control = setup
    Stream.gate = asyncio.Event()
    starting = asyncio.create_task(control.play(request()))
    while not Stream.created: await asyncio.sleep(0)
    await Stream.created[0].entered.wait()
    await control.stop()
    with pytest.raises(UserError): await starting
    assert all(s.closed for s in Stream.created)
    assert control.stream is None and control.pending is None and not control.targets

@pytest.mark.asyncio
async def test_selection_does_not_reserve_a_tuner_and_retained_play_is_ignored(setup):
    store, control = setup
    control.play = AsyncMock()
    ha = HomeAssistant(store,control,Channels(),None)
    await ha.command(dict(action='select_channel',receiver=A,channel='4.2 Fixture'))
    assert store.data['selected_channels'][A] == 'ABCDEF12:4.2'
    control.play.assert_not_awaited()
    ha.on_message(None,None,SimpleNamespace(topic=ha.base+'/command',retain=True,payload=b'{"action":"play_channel"}'))
    await asyncio.sleep(0)
    assert not ha.pending
    await ha.command(dict(action='play_channel',receiver=A))
    control.play.assert_awaited_once_with(request(channel='ABCDEF12:4.2'),add=True)

@pytest.mark.asyncio
async def test_pairing_client_accepts_receiver_array():
    app = web.Application()
    app.router.add_post('/receivers',lambda _: web.json_response([]))
    server = TestServer(app,host='127.0.0.1')
    async with server, ClientSession() as session:
        engine = PairingEngine('/tmp/unused',session)
        engine.port = server.port
        assert await engine.call('/receivers') == []

@pytest.mark.asyncio
async def test_ingress_and_mutation_boundary(tmp_path):
    async with ClientSession() as session:
        application = Application(Store(tmp_path),session)
        async with TestClient(TestServer(make_app(application))) as client:
            assert (await client.get('/api/state')).status == 403
            assert (await client.get('/api/state', headers={'X-Forwarded-For': '172.30.32.2'})).status == 403
        async with TestClient(TestServer(make_app(application,standalone=True))) as client:
            assert (await client.get('/api/state')).status == 200
            assert (await client.post('/api/actions/stop',json={})).status == 403
            response = await client.post('/api/actions/play',json=request(),headers={'X-AirplayVideo':'1'})
            assert response.status == 409
            assert (await response.json())['error'] == 'TV is not configured'
            application.controller.stream = object()
            response = await client.post('/api/setup/save',json=default_setup(),headers={'X-AirplayVideo':'1'})
            assert response.status == 409
            assert 'Stop playback' in (await response.json())['error']
            application.controller.stream = None
            response = await client.get('/preview?ticket=invalid')
            assert response.status == 409
            assert not application.store.data['setup']['complete']
