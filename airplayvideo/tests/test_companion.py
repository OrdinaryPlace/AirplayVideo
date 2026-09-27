"""Private native controls fail closed and never replay ambiguous operations."""
import asyncio
import json
import os
from pathlib import Path
import struct
from unittest.mock import AsyncMock
import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest
from service.companion import Companion, CompanionVersionMismatch, read_message, install
from service import companion as companion_module
from service.main import Application, make_app
from service.model import Store, UserError


def frame(value):
    payload = json.dumps(value).encode()
    return struct.pack('=I', len(payload)) + payload


@pytest.mark.asyncio
async def test_ready_waits_for_current_extension_version(tmp_path):
    companion = Companion(tmp_path, 'a' * 32)
    companion.ready.set()
    expected = json.loads((companion_module.SOURCE / 'manifest.json').read_text())['version']
    companion.call = AsyncMock(side_effect=[{'ok': True}, {'ok': True, 'version': '1.0.1'}, {'ok': True, 'version': expected}])
    await companion.wait_ready()
    assert companion.call.await_count == 3
    assert all(call.args == ('ready',) for call in companion.call.await_args_list)


@pytest.mark.asyncio
async def test_ready_tolerates_extension_replacement_disconnect(tmp_path):
    companion = Companion(tmp_path, 'a' * 32)
    companion.ready.set()
    expected = json.loads((companion_module.SOURCE / 'manifest.json').read_text())['version']
    companion.call = AsyncMock(side_effect=[UserError('Browser controls disconnected'), {'ok': True, 'version': expected}])
    await companion.wait_ready()
    assert companion.call.await_count == 2


@pytest.mark.asyncio
async def test_ready_rejects_an_extension_that_never_updates(tmp_path, monkeypatch):
    companion = Companion(tmp_path, 'a' * 32)
    companion.ready.set()
    companion.call = AsyncMock(return_value={'ok': True, 'version': '1.0.1'})
    wait_for = asyncio.wait_for
    async def bounded(awaitable, timeout):
        assert timeout == 30
        return await wait_for(awaitable, 0.02)
    monkeypatch.setattr(companion_module.asyncio, 'wait_for', bounded)
    with pytest.raises(CompanionVersionMismatch, match='restart to finish updating'):
        await companion.wait_ready()
    companion.call.assert_awaited_once_with('ready')


@pytest.mark.asyncio
async def test_missing_companion_is_not_an_update_retry(tmp_path, monkeypatch):
    companion = Companion(tmp_path, 'a' * 32)
    companion.call = AsyncMock()
    wait_for = asyncio.wait_for
    async def bounded(awaitable, timeout):
        return await wait_for(awaitable, 0.02)
    monkeypatch.setattr(companion_module.asyncio, 'wait_for', bounded)
    with pytest.raises(UserError, match='Current browser controls did not connect') as failed:
        await companion.wait_ready()
    assert not isinstance(failed.value, CompanionVersionMismatch)
    companion.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_waiting_for_current_extension_remains_cancellable(tmp_path):
    companion = Companion(tmp_path, 'a' * 32)
    companion.call = AsyncMock()
    waiting = asyncio.create_task(companion.wait_ready())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    companion.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_companion_disconnect_does_not_repeat_navigation(tmp_path):
    companion = Companion(tmp_path, 'a' * 32)
    await companion.start()
    reader, writer = await asyncio.open_unix_connection(companion.path)
    writer.write(frame({'hello': 'a' * 32}))
    await writer.drain()
    await asyncio.wait_for(companion.ready.wait(), 2)
    command = asyncio.create_task(companion.call('navigate', url='https://example.com'))
    request = await read_message(reader)
    assert request['action'] == 'navigate'
    writer.close()
    await writer.wait_closed()
    with pytest.raises(UserError, match='disconnected'):
        await command
    assert companion.sequence == 1 and not companion.pending
    await companion.close()


@pytest.mark.asyncio
async def test_companion_rejects_wrong_identity_and_oversized_frames(tmp_path):
    companion = Companion(tmp_path, 'a' * 32)
    await companion.start()
    for message in (frame({'hello': 'b' * 32}), struct.pack('=I', 65537)):
        reader, writer = await asyncio.open_unix_connection(companion.path)
        writer.write(message)
        await writer.drain()
        assert await asyncio.wait_for(reader.read(), 2) == b''
        writer.close()
        await writer.wait_closed()
    assert not companion.ready.is_set()
    await companion.close()


def test_signed_companion_identity_and_narrow_permissions_survive_reinstall(tmp_path):
    root = tmp_path / 'browser'
    root.mkdir()
    identity = install(root, registration=tmp_path/'registration', packages=tmp_path/'packages')
    assert install(root, registration=tmp_path/'registration', packages=tmp_path/'packages') == identity
    key = tmp_path / 'browser-controls.pem'
    assert key.stat().st_mode & 0o777 == 0o600
    manifest = json.loads((Path(__file__).parent.parent/'companion/manifest.json').read_text())
    assert manifest['host_permissions'] == ['https://www.youtube.com/*']
    assert set(manifest['permissions']) == {'nativeMessaging', 'scripting'}
    assert not any(k in manifest for k in ('externally_connectable', 'content_scripts'))


@pytest.mark.asyncio
async def test_wizard_preview_before_completion_preserves_setup_and_existing_login(tmp_path):
    async with aiohttp.ClientSession() as session:
        application = Application(Store(tmp_path), session)
        browser = application.browser
        browser.navigate = AsyncMock()
        # The tuner step is unfinished; sign-in must still be available.
        application.store.data['setup']['modes']['hdhomerun'] = True
        before = json.dumps(application.store.data, sort_keys=True)
        async with TestClient(TestServer(make_app(application, standalone=True))) as client:
            async def open_preview():
                return await client.post('/api/setup/open_browser', json={'url':'https://www.youtube.com/'}, headers={'X-AirplayVideo':'1'})
            assert (await open_preview()).status == 200
            browser.navigate.assert_awaited_once()
            assert json.dumps(application.store.data, sort_keys=True) == before
            browser.running = True
            assert (await open_preview()).status == 200
            browser.navigate.assert_awaited_once()  # Do not interrupt a login.
            application.controller.stream = object()
            response = await open_preview()
            assert response.status == 409
            assert 'Stop playback' in (await response.json())['error']
