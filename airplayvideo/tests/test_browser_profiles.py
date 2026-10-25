"""Persistent fixed profile roots switch only after the previous desktop closes."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from service.browser import Browser
from service.model import UserError, default_setup


@pytest.mark.asyncio
async def test_profile_switch_closes_old_desktop_before_changing_all_private_paths(tmp_path):
    browser = Browser(tmp_path, None)
    default_root = tmp_path / 'browser'
    assert browser.profile == 'default' and browser.root == default_root
    browser.running = True
    steps = []
    async def close(*, graceful=False):
        assert graceful
        assert browser.root == default_root and browser.environment['HOME'] == str(default_root)
        steps.append('closed')
        browser.running = False
    async def start(setup):
        assert steps == ['closed'] and not browser.running
        steps.append('started')
        browser.running = True
    browser._close = AsyncMock(side_effect=close)
    browser._start_once = AsyncMock(side_effect=start)
    await browser.start(default_setup(), profile='sportsnet')
    root = tmp_path / 'browser-sportsnet'
    assert browser.root == root and browser.profile == 'sportsnet'
    assert browser.environment['HOME'] == str(root)
    assert browser.environment['XAUTHORITY'] == str(root / 'Xauthority')
    assert browser.environment['XDG_RUNTIME_DIR'] == str(root / 'runtime')
    assert browser.environment['PULSE_SERVER'] == 'unix:' + str(root / 'pulse.sock')
    assert browser.environment['PULSE_COOKIE'] == str(root / 'pulse-cookie')
    assert browser.root.parent == default_root.parent  # Existing signing-key parent.
    assert not default_root.exists() and not root.exists()  # No import/copy in selection.


@pytest.mark.asyncio
async def test_same_profile_reuses_desktop_and_invalid_profile_cannot_change_paths(tmp_path):
    browser = Browser(tmp_path, None)
    browser._close = AsyncMock()
    browser._start_once = AsyncMock()
    await browser.start(default_setup(), profile='default')
    browser._close.assert_not_awaited()
    old_environment = dict(browser.environment)
    for value in ('../outside', '/tmp/other', 'Sportsnet', ''):
        with pytest.raises(UserError):
            await browser.start(default_setup(), profile=value)
    assert browser.root == tmp_path / 'browser' and browser.environment == old_environment
    browser._close.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_switch_finishes_old_close_without_changing_profile_or_starting(tmp_path):
    browser = Browser(tmp_path, None)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def close(*, graceful=False):
        assert graceful
        entered.set()
        await release.wait()
        finished.set()
    browser._close = AsyncMock(side_effect=close)
    browser._start_once = AsyncMock()
    switching = asyncio.create_task(browser.start(default_setup(), profile='sportsnet'))
    await entered.wait()
    switching.cancel()
    await asyncio.sleep(0)
    assert not switching.done() and browser.profile == 'default'
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await switching
    assert finished.is_set() and browser.profile == 'default' and browser.root == tmp_path / 'browser'
    browser._start_once.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_profile_switches_never_overlap_desktops(tmp_path):
    browser = Browser(tmp_path, None)
    active = [1]
    browser.running = True
    async def close(*, graceful=False):
        assert graceful and active[0] == 1
        await asyncio.sleep(0)
        active[0] = 0
        browser.running = False
    async def start(setup):
        assert active[0] == 0
        active[0] = 1
        browser.running = True
    browser._close = AsyncMock(side_effect=close)
    browser._start_once = AsyncMock(side_effect=start)
    await asyncio.gather(browser.start(default_setup(), profile='sportsnet'),
                         browser.start(default_setup(), profile='default'))
    assert browser.profile == 'default' and active[0] == 1


@pytest.mark.asyncio
async def test_sportsnet_rendering_change_closes_before_restart_without_touching_profile_data(tmp_path):
    browser = Browser(tmp_path, None)
    browser._select_profile('sportsnet')
    root = browser.root
    root.mkdir()
    retained = root / 'retained-profile-fixture'
    retained.write_text('keep')
    browser.running = True
    steps = []
    async def close(*, graceful=False):
        assert graceful and browser.rendering == 'vulkan' and browser.root == root
        steps.append('closed')
        browser.running = False
    async def start(setup):
        assert not browser.running and browser.rendering == 'opengl' and browser.root == root
        assert steps == ['closed']
        browser.running = True
    browser._close = AsyncMock(side_effect=close)
    browser._start_once = AsyncMock(side_effect=start)
    settings = default_setup()
    settings['browser']['sportsnet_rendering'] = 'opengl'
    await browser.start(settings)
    assert retained.read_text() == 'keep'
    assert browser.environment['HOME'] == str(root)
    browser._start_once.side_effect = None
    await browser.start(settings)
    assert browser._close.await_count == 1  # Identical mode reuses its desktop.
    settings['browser']['sportsnet_rendering'] = 'arbitrary-flags'
    with pytest.raises(UserError, match='Sportsnet graphics'):
        await browser.start(settings)
    assert browser._close.await_count == 1 and browser.rendering == 'opengl'


@pytest.mark.asyncio
async def test_sportsnet_setting_does_not_restart_or_change_default_profile(tmp_path):
    browser = Browser(tmp_path, None)
    browser.running = True
    browser._close = AsyncMock()
    browser._start_once = AsyncMock()
    original_environment = dict(browser.environment)
    settings = default_setup()
    settings['browser']['sportsnet_rendering'] = 'opengl'
    await browser.start(settings, profile='default')
    assert browser.rendering == 'vulkan' and browser.root == tmp_path / 'browser'
    assert browser.environment == original_environment
    browser._close.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('profile,setting,expected', [
    ('default', 'vulkan', 'vulkan'), ('default', 'opengl', 'vulkan'),
    ('sportsnet', 'vulkan', 'vulkan'), ('sportsnet', 'opengl', 'opengl'),
])
async def test_chrome_launch_uses_selected_profile_graphics_and_retains_route_and_sandbox(tmp_path, monkeypatch, profile, setting, expected):
    from types import SimpleNamespace
    from service import browser as browser_module
    from service.browser_proxy import chrome_proxy_arguments
    browser = Browser(tmp_path, None)
    monkeypatch.setattr(browser_module.os, 'chown', lambda *args: None)
    monkeypatch.setattr(browser_module, 'install_companion', lambda root: 'a' * 32)
    probe = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(browser_module.asyncio, 'create_subprocess_exec', AsyncMock(return_value=probe))
    companion = SimpleNamespace(path=tmp_path/'controls.sock', start=AsyncMock(), wait_ready=AsyncMock())
    monkeypatch.setattr(browser_module, 'Companion', lambda *args: companion)
    chosen = []
    def graphics(*, rendering):
        chosen.append(rendering)
        return ['--fixture-graphics=' + rendering]
    monkeypatch.setattr(browser_module, 'chrome_gpu_arguments', graphics)
    browser._private_file = lambda *args: None
    browser.launch = AsyncMock(return_value=SimpleNamespace())
    browser.start_display = AsyncMock()
    browser.wait_port = AsyncMock()
    settings = default_setup()
    settings['browser'].update(sportsnet_rendering=setting, http_proxy='http://gateway:8888', http_proxy_enabled=True)
    await browser.start(settings, profile=profile)
    assert browser.running and chosen == [expected]
    chrome = next(call.args for call in browser.launch.await_args_list if call.args[0] == 'google-chrome')
    root = tmp_path / ('browser-sportsnet' if profile == 'sportsnet' else 'browser')
    assert chrome == ('google-chrome', '--no-first-run', '--no-default-browser-check',
        '--password-store=basic', '--disable-dev-shm-usage', '--autoplay-policy=no-user-gesture-required',
        '--user-data-dir=' + str(root / 'profile'), '--window-size=1920,1080', '--start-fullscreen',
        '--fixture-graphics=' + expected, *chrome_proxy_arguments('http://gateway:8888'), 'about:blank')
    assert browser.environment['HOME'] == str(root)
    assert not any('no-sandbox' in flag or 'disable-gpu-sandbox' in flag or
                   'remote-debugging' in flag or 'user-agent' in flag for flag in chrome)
