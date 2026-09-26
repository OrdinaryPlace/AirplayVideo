import asyncio
import json
import os

import aiohttp
import pytest

from service.native_hls_origin import NativeHLSOrigin


def directory(tmp_path):
    path = tmp_path / 'native-stream-fixture'
    path.mkdir(mode=0o700)
    (path / 'index.m3u8').write_bytes(b'#EXTM3U\nsegment-00000000.m4s\n')
    (path / 'init.mp4').write_bytes(b'init')
    (path / 'segment-00000000.m4s').write_bytes(b'0123456789')
    return path


@pytest.mark.asyncio
async def test_hls_ranges_head_mime_and_safe_counters(tmp_path):
    path = directory(tmp_path)
    origin = await NativeHLSOrigin(path, {'tv': '127.0.0.1'}, bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url()) as response:
                assert response.status == 200
                assert response.content_type == 'application/vnd.apple.mpegurl'
                assert await response.read() == (path / 'index.m3u8').read_bytes()
            async with client.get(origin.url('init.mp4')) as response:
                assert response.status == 200 and await response.read() == b'init'
            async with client.get(origin.url('segment-00000000.m4s'), headers={'Range': 'bytes=2-5'}) as response:
                assert response.status == 206 and await response.read() == b'2345'
                assert response.headers['Content-Range'] == 'bytes 2-5/10'
            async with client.head(origin.url('segment-00000000.m4s')) as response:
                assert response.status == 200 and response.headers['Content-Length'] == '10'
                assert await response.read() == b''
        result = origin.counters()
        assert result['receiver']['bytes'] == len((path / 'index.m3u8').read_bytes()) + 8
        assert result['receiver']['segment_bytes'] == 4
        assert result['receiver']['completed_requests'] == 4
        assert result['receiver']['head_requests'] == 1
        assert result['receiver'] == result['receivers']['tv']
        assert result['preflight']['bytes'] == 0
        assert all(value not in json.dumps(result) for value in ('127.0.0.1', origin.token, str(path)))
        result['receiver']['bytes'] = -1
        assert origin.counters()['receiver']['bytes'] > 0
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_preflight_fetches_never_count_as_receiver_fetches(tmp_path):
    origin = await NativeHLSOrigin(directory(tmp_path), {'tv': '192.168.250.10'}, bind_address='127.0.0.1',
                                   preflight_clients=['127.0.0.1']).start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url('segment-00000000.m4s')) as response:
                assert await response.read() == b'0123456789'
        assert origin.counters()['preflight']['bytes'] == 10
        assert origin.counters()['receiver']['bytes'] == 0
        assert origin.counters()['receivers']['tv']['requests'] == 0
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_atomic_playlist_replacement_new_segments_and_rolling_deletion(tmp_path):
    path = directory(tmp_path)
    origin = await NativeHLSOrigin(path, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            temporary = path / 'index.m3u8.tmp'
            temporary.write_bytes(b'#EXTM3U\nsegment-00000001.m4s\n')
            temporary.replace(path / 'index.m3u8')
            (path / 'segment-00000001.m4s.tmp').write_bytes(b'next')
            (path / 'segment-00000001.m4s.tmp').replace(path / 'segment-00000001.m4s')
            (path / 'segment-00000000.m4s').unlink()
            async with client.get(origin.url()) as response:
                assert await response.read() == b'#EXTM3U\nsegment-00000001.m4s\n'
            async with client.get(origin.url('segment-00000001.m4s')) as response:
                assert await response.read() == b'next'
            async with client.get(origin.url('segment-00000000.m4s')) as response:
                assert response.status == 404
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_only_whitelisted_finalized_regular_files_are_served(tmp_path):
    path = directory(tmp_path)
    secret = tmp_path / 'secret'
    secret.write_bytes(b'private')
    for name in ('index.m3u8.tmp', 'probe.json', 'other.mp4', 'segment-00000000.m4s.gz', 'segment-00000000.m4s.br'):
        (path / name).write_bytes(b'private')
    (path / 'segment-00000001.m4s').symlink_to(secret)
    os.link(secret, path / 'segment-00000002.m4s')
    (path / 'segment-00000003.m4s').mkdir()
    origin = await NativeHLSOrigin(path, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as client:
            async with client.get(origin.url('segment-00000000.m4s'), headers={'Accept-Encoding': 'br, gzip'}) as response:
                assert await response.read() == b'0123456789'
                assert 'Content-Encoding' not in response.headers
            base = origin.url().rsplit('/', 1)[0]
            for name in ('index.m3u8.tmp', 'probe.json', 'other.mp4', 'segment-00000000.m4s.gz',
                         'segment-00000000.m4s.br', 'segment-00000001.m4s', 'segment-00000002.m4s',
                         'segment-00000003.m4s', '..%2Fsecret', '%2Fetc%2Fpasswd'):
                async with client.get(base + '/' + name) as response:
                    assert response.status == 404
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_pinned_directory_descriptor_prevents_path_replacement(tmp_path):
    path = directory(tmp_path)
    origin = await NativeHLSOrigin(path, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        path.rename(tmp_path / 'old-directory')
        path.mkdir()
        (path / 'index.m3u8').write_bytes(b'private replacement')
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url()) as response:
                assert await response.read() == b'#EXTM3U\nsegment-00000000.m4s\n'
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_wrong_client_and_token_are_denied(tmp_path):
    origin = await NativeHLSOrigin(directory(tmp_path), ['192.168.250.10'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            for url in (origin.url(), origin.url().replace(origin.token, 'wrong')):
                async with client.get(url) as response:
                    assert response.status == 404
        assert origin.counters()['denied_requests'] == 2
        assert origin.counters()['receiver']['requests'] == 0
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_non_ascii_token_is_not_a_server_error(tmp_path):
    origin = await NativeHLSOrigin(directory(tmp_path), ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            async with client.get(origin.url().replace(origin.token, 'токен')) as response:
                assert response.status == 404
        assert origin.counters()['denied_requests'] == 1
        assert origin.counters()['receiver']['requests'] == 0
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_empty_suffix_open_ended_and_invalid_ranges(tmp_path):
    path = directory(tmp_path)
    (path / 'segment-00000001.m4s').write_bytes(b'')
    origin = await NativeHLSOrigin(path, ['127.0.0.1'], bind_address='127.0.0.1').start()
    try:
        async with aiohttp.ClientSession() as client:
            for value, expected in [('bytes=-3', b'789'), ('bytes=7-', b'789'), ('bytes=7-99', b'789')]:
                async with client.get(origin.url('segment-00000000.m4s'), headers={'Range': value}) as response:
                    assert response.status == 206 and await response.read() == expected
            for value in ('bytes=-0', 'bytes=100-', 'bytes=2-1', 'bytes=1-2,4-5'):
                async with client.get(origin.url('segment-00000000.m4s'), headers={'Range': value}) as response:
                    assert response.status == 416 and response.headers['Content-Range'] == 'bytes */10'
            async with client.get(origin.url('segment-00000001.m4s')) as response:
                assert response.status == 200 and await response.read() == b''
            async with client.get(origin.url('segment-00000001.m4s'), headers={'Range': 'bytes=0-'}) as response:
                assert response.status == 416
    finally:
        await origin.close()


@pytest.mark.asyncio
async def test_expiration_and_concurrent_close_release_socket_and_directory(tmp_path):
    origin = await NativeHLSOrigin(directory(tmp_path), ['127.0.0.1'], bind_address='127.0.0.1', lifetime=1).start()
    url, descriptor = origin.url(), origin._directory_fd
    await asyncio.wait_for(origin.closed.wait(), 3)
    await asyncio.gather(origin.close(), origin.close())
    assert origin.port is None and origin.runner is None and origin._directory_fd is None
    with pytest.raises(OSError):
        os.fstat(descriptor)
    async with aiohttp.ClientSession() as client:
        with pytest.raises(aiohttp.ClientError):
            await client.get(url)


@pytest.mark.asyncio
async def test_symlink_directory_and_failed_start_are_closed(tmp_path):
    path = directory(tmp_path)
    link = tmp_path / 'native-stream-link'
    link.symlink_to(path, target_is_directory=True)
    origin = NativeHLSOrigin(link, ['127.0.0.1'], bind_address='127.0.0.1')
    with pytest.raises(ValueError, match='unsafe'):
        await origin.start()
    assert origin.closed.is_set() and origin._directory_fd is None


@pytest.mark.parametrize('kwargs', [{'bind_address': '0.0.0.0'}, {'bind_address': '::1'},
                                    {'receiver_clients': ['::1']}, {'lifetime': 14401},
                                    {'lifetime': 10**400},
                                    {'lifetime': True}, {'lifetime': float('nan')},
                                    {'preflight_clients': ['127.0.0.1']}])
def test_configuration_is_explicit_and_bounded(tmp_path, kwargs):
    arguments = {'receiver_clients': ['127.0.0.1'], 'bind_address': '127.0.0.1', **kwargs}
    with pytest.raises(ValueError):
        NativeHLSOrigin(directory(tmp_path), **arguments)
