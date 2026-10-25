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
