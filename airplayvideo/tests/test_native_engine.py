import asyncio
import json
import sys

import pytest

from service.native_engine import NativeEngineError, NativeEnginePlayer, validate_media_url


URL = 'http://127.0.0.1:4567/abcdefghijklmnopqrstuvwx/sample.mp4'
RECEIVER = 'a' * 32


def fake_engine(tmp_path, body):
    path = tmp_path / 'engine'
    path.write_text('#!' + sys.executable + '\n' + body)
    path.chmod(0o700)
    return str(path)


@pytest.mark.parametrize('url', [
    'https://example.com/video.mp4', 'http://127.0.0.1/video.mp4',
    'http://0.0.0.0:1234/abcdefghijklmnop/file.mp4',
    'http://127.0.0.1:1234/abcdefghijklmnop/../secret',
    'http://user:password@127.0.0.1:1234/abcdefghijklmnop/file.mp4',
    URL + '?key=secret', URL + '#fragment',
])
def test_scoped_media_url_only(url):
    with pytest.raises(NativeEngineError):
        validate_media_url(url)


@pytest.mark.asyncio
async def test_engine_reads_pairing_in_place_and_keeps_url_out_of_arguments(tmp_path, monkeypatch):
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'private-supervisor-value')
    engine = fake_engine(tmp_path, '''import json, os, stat, sys
path = sys.argv[sys.argv.index('--native') + 1]
assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
request = json.load(open(path))
assert set(request) == {'receiver_id', 'media_url', 'seconds'}
assert request['media_url'] not in sys.argv
assert sys.argv[-1].endswith('/receivers')
assert 'SUPERVISOR_TOKEN' not in os.environ
print(json.dumps({'event':'native_stage','details':{'stage':'insert','status':200,'secret':'discard'}}), flush=True)
print(json.dumps({'event':'native_stage','details':{'stage':'finished','elapsed_ms':1}}), flush=True)
''')
    player = await NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine).start()
    await asyncio.wait_for(player.wait(), 5)
    assert player.events == [
        {'event': 'native_stage', 'details': {'stage': 'insert', 'status': 200}},
        {'event': 'native_stage', 'details': {'stage': 'finished', 'elapsed_ms': 1}},
    ]
    assert list((tmp_path / 'run').iterdir()) == []
    assert player.process.returncode == 0


@pytest.mark.asyncio
async def test_child_failure_does_not_expose_diagnostics(tmp_path):
    engine = fake_engine(tmp_path, '''import json, sys
print(json.dumps({'event':'fatal','details':{'message':'secret signed URL'}}), flush=True)
print('secret stderr', file=sys.stderr)
sys.exit(1)
''')
    player = await NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine).start()
    with pytest.raises(NativeEngineError, match='safe stage events'):
        await asyncio.wait_for(player.wait(), 5)
    assert player.events == [{'event': 'native_failed', 'details': {}}]
    assert 'secret' not in json.dumps(player.events)
    assert list((tmp_path / 'run').iterdir()) == []


@pytest.mark.asyncio
async def test_close_reaps_unresponsive_worker_and_concurrent_waiters(tmp_path):
    engine = fake_engine(tmp_path, '''import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'event':'native_stage','details':{'stage':'connect'}}), flush=True)
while True: time.sleep(0.01)
''')
    player = await NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine,
                                      close_timeout=0.05).start()
    async def ready():
        while not player.events:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(ready(), 3)
    await asyncio.wait_for(asyncio.gather(player.close(), player.close()), 3)
    assert player.closed.is_set() and player.process.returncode is not None
    assert list((tmp_path / 'run').iterdir()) == []
    await player.close()


@pytest.mark.asyncio
async def test_start_failure_cleans_temporary_config(tmp_path):
    player = NativeEnginePlayer(tmp_path, RECEIVER, URL, engine='/nonexistent/engine')
    with pytest.raises(NativeEngineError, match='could not start'):
        await player.start()
    assert player.closed.is_set()
    assert list((tmp_path / 'run').iterdir()) == []


def test_event_schema_is_closed(tmp_path):
    player = NativeEnginePlayer(tmp_path, RECEIVER, URL)
    for item in [[], None, {'event': 'native_stage', 'details': {'stage': 'secret'}},
                 {'event': 'unknown', 'details': {}},
                 {'event': 'native_stage', 'details': {'stage': 'verify', 'status': True,
                                                    'elapsed_ms': -1, 'url': URL}}]:
        player._event(item)
    assert player.events == [{'event': 'native_stage', 'details': {'stage': 'verify'}}]


@pytest.mark.asyncio
async def test_selected_address_is_included_for_engine_identity_match(tmp_path):
    engine = fake_engine(tmp_path, '''import json, sys
request = json.load(open(sys.argv[2]))
assert request['receiver_address'] == '192.168.10.2'
''')
    player = await NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine,
                                      receiver_address='192.168.10.2').start()
    await asyncio.wait_for(player.wait(), 5)


@pytest.mark.asyncio
async def test_cancelled_close_waiter_cannot_abandon_worker(tmp_path):
    engine = fake_engine(tmp_path, '''import json, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print(json.dumps({'event':'native_stage','details':{'stage':'connect'}}), flush=True)
while True: time.sleep(0.01)
''')
    player = await NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine,
                                      close_timeout=0.1).start()
    async def ready():
        while not player.events:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(ready(), 3)
    closing = asyncio.create_task(player.close())
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await asyncio.wait_for(player.close(), 3)
    assert player.process.returncode is not None and player.closed.is_set()


@pytest.mark.asyncio
async def test_cancellation_during_spawn_adopts_and_reaps_child(tmp_path, monkeypatch):
    engine = fake_engine(tmp_path, 'import time\ntime.sleep(30)\n')
    spawned, release = asyncio.Event(), asyncio.Event()
    create = asyncio.create_subprocess_exec
    children = []

    async def delayed_create(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_create)
    player = NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine)
    starting = asyncio.create_task(player.start())
    await asyncio.wait_for(spawned.wait(), 3)
    starting.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(starting, 3)
    assert children[0].returncode is not None and player.closed.is_set()
    assert list((tmp_path / 'run').iterdir()) == []


@pytest.mark.asyncio
async def test_close_during_spawn_cannot_finish_before_child_is_reaped(tmp_path, monkeypatch):
    engine = fake_engine(tmp_path, 'import time\ntime.sleep(30)\n')
    spawned, release = asyncio.Event(), asyncio.Event()
    create = asyncio.create_subprocess_exec
    children = []

    async def delayed_create(*args, **kwargs):
        child = await create(*args, **kwargs)
        children.append(child)
        spawned.set()
        await release.wait()
        return child

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', delayed_create)
    player = NativeEnginePlayer(tmp_path, RECEIVER, URL, engine=engine)
    starting = asyncio.create_task(player.start())
    await asyncio.wait_for(spawned.wait(), 3)
    closing = asyncio.create_task(player.close())
    await asyncio.sleep(0)
    assert not player.closed.is_set()
    release.set()
    await asyncio.wait_for(closing, 3)
    with pytest.raises(NativeEngineError):
        await starting
    assert children[0].returncode is not None and player.closed.is_set()
    assert player._reader is None and player._watchdog is None
    assert list((tmp_path / 'run').iterdir()) == []
