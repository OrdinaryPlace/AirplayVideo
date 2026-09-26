import asyncio
import aiohttp
import pytest
from service.native_origin import MediaOrigin


@pytest.mark.asyncio
async def test_exact_file_ranges_scope_and_shutdown(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'0123456789')
    origin = await MediaOrigin({'clip.mp4': clip}, ['127.0.0.1'], bind_address='127.0.0.1').start()
    url = origin.url('clip.mp4')
    async with aiohttp.ClientSession() as client:
        async with client.get(url, headers={'Range': 'bytes=2-5'}) as response:
            assert response.status == 206
            assert response.headers['Content-Range'] == 'bytes 2-5/10'
            assert await response.read() == b'2345'
        async with client.head(url) as response:
            assert response.status == 200 and response.headers['Content-Length'] == '10'
            assert await response.read() == b''
        for bad in (url.replace(origin.token, 'wrong'), url.replace('clip.mp4', 'missing.mp4')):
            async with client.get(bad) as response:
                assert response.status == 404
        async with client.get(url, headers={'Range': 'bytes=100-200'}) as response:
            assert response.status == 416
        await origin.close()
        await origin.close()
        with pytest.raises(aiohttp.ClientError):
            await client.get(url)
    assert len(origin.requests) == 3
    assert all(origin.token not in str(item) for item in origin.requests)


@pytest.mark.asyncio
async def test_wrong_client_and_expiration(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'public sample')
    origin = await MediaOrigin({'clip.mp4': clip}, ['192.168.250.10'], bind_address='127.0.0.1', lifetime=1).start()
    async with aiohttp.ClientSession() as client:
        async with client.get(origin.url('clip.mp4')) as response:
            assert response.status == 404
    await asyncio.wait_for(origin.closed.wait(), 3)
    assert origin.requests == []
    assert origin.port is None and origin.runner is None


def test_no_broad_bind_path_escape_or_symlink(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'public sample')
    link = tmp_path / 'link.mp4'
    link.symlink_to(clip)
    for files, bind in (({'clip.mp4':clip},'0.0.0.0'), ({'../clip.mp4':clip},'127.0.0.1'), ({'clip.mp4':link},'127.0.0.1')):
        with pytest.raises(ValueError):
            MediaOrigin(files, ['127.0.0.1'], bind_address=bind)


@pytest.mark.parametrize('bind,clients', [('::1', ['127.0.0.1']), ('127.0.0.1', ['::1']),
                                        ('127.0.0.1', ['::ffff:127.0.0.1'])])
def test_only_explicit_ipv4_is_accepted(tmp_path, bind, clients):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'public sample')
    with pytest.raises(ValueError, match='IPv4'):
        MediaOrigin({'clip.mp4': clip}, clients, bind_address=bind)


@pytest.mark.asyncio
async def test_compressed_siblings_are_never_served_or_disclosed(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'0123456789')
    (tmp_path / 'sample.mp4.gz').write_bytes(b'private gzip sibling')
    (tmp_path / 'sample.mp4.br').write_bytes(b'private brotli sibling')
    origin = await MediaOrigin({'clip.mp4': clip}, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            for encoding in ('gzip', 'br', 'br, gzip'):
                async with client.get(origin.url('clip.mp4'), headers={'Accept-Encoding': encoding}) as response:
                    assert response.status == 200
                    assert 'Content-Encoding' not in response.headers
                    assert await response.read() == b'0123456789'
            for suffix in ('.gz', '.br'):
                async with client.get(origin.url('clip.mp4') + suffix) as response:
                    assert response.status == 404
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_suffix_open_ended_ranges_and_if_range_fallback(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'0123456789')
    origin = await MediaOrigin({'clip.mp4': clip}, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            for value, expected in [('bytes=-3', b'789'), ('bytes=7-', b'789'),
                                    ('bytes=7-100', b'789'), ('bytes=-100', b'0123456789')]:
                async with client.get(origin.url('clip.mp4'), headers={'Range': value}) as response:
                    assert response.status == 206 and await response.read() == expected
                    assert int(response.headers['Content-Length']) == len(expected)
            for value in ('bytes=-0', 'bytes=9-1', 'bytes=1-2,4-5', 'bytes=-'):
                async with client.get(origin.url('clip.mp4'), headers={'Range': value}) as response:
                    assert response.status == 416
                    assert response.headers['Content-Range'] == 'bytes */10'
            async with client.get(origin.url('clip.mp4'), headers={'Range': 'bytes=2-5', 'If-Range': '"other-file"'}) as response:
                assert response.status == 200 and await response.read() == b'0123456789'
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_file_replaced_after_allowlisting_is_not_exposed(tmp_path):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'allowed content')
    origin = await MediaOrigin({'clip.mp4': clip}, ['127.0.0.1'], bind_address='127.0.0.1').start()
    replacement = tmp_path / 'replacement.mp4'
    replacement.write_bytes(b'not allowlisted')
    replacement.replace(clip)
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url('clip.mp4')) as response:
                assert response.status == 404
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_close_completion_is_shared_and_survives_cancelled_waiter(tmp_path, monkeypatch):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'public sample')
    origin = await MediaOrigin({'clip.mp4': clip}, ['127.0.0.1'], bind_address='127.0.0.1').start()
    url, runner, timer = origin.url('clip.mp4'), origin.runner, origin._timer
    entered, release = asyncio.Event(), asyncio.Event()
    original_cleanup = type(runner).cleanup
    calls = 0

    async def delayed_cleanup(self):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        await original_cleanup(self)

    monkeypatch.setattr(type(runner), 'cleanup', delayed_cleanup)
    first = asyncio.create_task(origin.close())
    await asyncio.wait_for(entered.wait(), 2)
    second = asyncio.create_task(origin.close())
    await asyncio.sleep(0)
    assert not first.done() and not second.done() and not origin.closed.is_set()
    with pytest.raises(ValueError):
        origin.url('clip.mp4')
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert not origin.closed.is_set()
    release.set()
    await asyncio.wait_for(second, 2)
    await origin.close()
    assert calls == 1 and origin.closed.is_set() and timer.done()
    assert origin.port is None and origin.runner is None
    async with aiohttp.ClientSession() as client:
        with pytest.raises(aiohttp.ClientError):
            await client.get(url)


@pytest.mark.asyncio
@pytest.mark.parametrize('receiver,role', [(True, 'receiver'), (False, 'preflight')])
async def test_media_bytes_are_attributed_to_receiver_or_preflight(tmp_path, receiver, role):
    clip = tmp_path / 'sample.mp4'
    clip.write_bytes(b'0123456789')
    origin = await MediaOrigin({'sample.mp4': clip}, ['127.0.0.1'],
                               receiver_clients=['127.0.0.1'] if receiver else [],
                               bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url('sample.mp4'), headers={'Range': 'bytes=2-5'}) as response:
                assert await response.read() == b'2345'
            async with client.head(origin.url('sample.mp4')) as response:
                assert response.status == 200
        assert origin.counters()[role] == {'requests': 2, 'bytes_sent': 4}
        assert all(record['role'] == role and record['completed'] for record in origin.requests)
        assert origin.requests[0]['status'] == 206
        assert '127.0.0.1' not in str(origin.requests)
    finally:
        await origin.close()
