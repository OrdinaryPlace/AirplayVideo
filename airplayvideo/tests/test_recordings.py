import asyncio
import copy
import json
from unittest.mock import AsyncMock
import pytest
from aiohttp import ClientSession
from aiohttp.test_utils import TestServer, TestClient
from service import recordings as module
from service.recordings import Recordings
from service.main import Application, make_app
from service.model import Store, UserError
from test_lifecycle import setup, request, Stream, A


@pytest.mark.asyncio
async def test_recording_taps_live_stream_without_replacing_or_stopping_tv(setup):
    store,c=setup
    c.recordings=recorder=Recordings(c)
    await c.play(request());playing=c.stream
    before=copy.deepcopy(store.data)
    await recorder.start({'mode':'browser','seconds':5})
    capture_id=recorder.current['id']
    assert recorder.stream is playing and not recorder.owned and c.targets=={A}
    assert playing.commands[-1]=={'action':'record','id':capture_id,'seconds':5}
    await c.event(playing,'streaming',{'id':A,'video_age_us':5000,'audio_age_us':7000,'unexpected_private_field':'never save'})
    await c.event(playing,'recording_finished',{'id':capture_id,'status':'complete'})
    await asyncio.gather(*recorder.tasks)
    assert recorder.current is None and c.stream is playing and not playing.closed
    assert store.data==before
    report=json.loads((recorder.root/(capture_id+'.json')).read_text())
    assert report['receiver_timing'][A]=={'video_age_us':5000,'audio_age_us':7000}
    await c.stop()


@pytest.mark.asyncio
async def test_idle_browser_recording_does_not_navigate_or_contact_tv(setup,monkeypatch):
    store,c=setup
    monkeypatch.setattr(module,'Stream',Stream)
    c.browser.navigate=AsyncMock()
    c.recordings=recorder=Recordings(c)
    await recorder.start({'mode':'browser'})
    capture=recorder.stream
    assert capture.config['source']['display']==':42.0'
    assert not c.targets and c.stream is None and c.phase=='idle'
    c.browser.navigate.assert_not_awaited()
    with pytest.raises(UserError,match='Wait for'):await c.play(request())
    with pytest.raises(UserError,match='already running'):await recorder.start({})
    await recorder.finish()
    assert capture.closed and not recorder.current and c.stream is None


@pytest.mark.asyncio
async def test_capture_failure_and_bounds_leave_playback_available(setup,monkeypatch):
    _,c=setup;monkeypatch.setattr(module,'Stream',Stream)
    recorder=Recordings(c)
    for seconds in (0,31,True,'15'):
        with pytest.raises(UserError):await recorder.start({'seconds':seconds})
        assert recorder.current is None
    Stream.fail=True
    with pytest.raises(UserError):await recorder.start({'mode':'browser'})
    assert recorder.current is None and recorder.stream is None
    assert Stream.created[-1].closed


@pytest.mark.asyncio
async def test_live_tv_file_capture_uses_single_tuner_without_receivers(setup,monkeypatch):
    _,c=setup;monkeypatch.setattr(module,'Stream',Stream)
    recorder=Recordings(c)
    await recorder.start({'mode':'hdhomerun','channel':'ABCDEF12:4.1','seconds':5})
    assert recorder.stream.config['source']['kind']=='hdhomerun'
    assert len(Stream.created)==1 and not c.targets and c.stream is None
    await recorder.close()


@pytest.mark.asyncio
async def test_recording_downloads_require_ingress_and_exact_private_file(tmp_path):
    async with ClientSession() as session:
        app=Application(Store(tmp_path),session)
        capture_id='c'*32
        path=app.recordings.root/(capture_id+'.mkv');path.write_bytes(b'fixture')
        url=f'/api/recordings/{capture_id}/mkv'
        async with TestClient(TestServer(make_app(app))) as client:
            assert (await client.get(url)).status==403
        async with TestClient(TestServer(make_app(app,standalone=True))) as client:
            response=await client.get(url)
            assert response.status==200 and await response.read()==b'fixture'
            assert 'attachment' in response.headers['Content-Disposition']
            assert (await client.get(f'/api/recordings/{capture_id}/settings')).status==409
            assert (await client.get('/api/recordings/not-an-id/mkv')).status==409
            app.recordings.current={'id':capture_id}
            assert (await client.get(url)).status==409
            app.recordings.current=None
            path.unlink();path.symlink_to(tmp_path/'settings.json')
            assert (await client.get(url)).status==409
