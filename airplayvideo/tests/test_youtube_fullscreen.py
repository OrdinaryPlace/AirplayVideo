"""Fullscreen uses one verified native click and never guesses through a prompt."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.browser import Browser
from service.model import UserError
from test_lifecycle import setup


@pytest.fixture
def browser(tmp_path):
    instance = Browser(tmp_path, None)
    instance.running = True
    instance.chrome_process = SimpleNamespace(pid=123)
    instance.companion = SimpleNamespace(call=AsyncMock())
    async def native(*args, **kwargs):
        if 'viewonly' in args:
            return 'viewonly:1'
        if 'getactivewindow' in args:
            return '456'
        if 'getwindowpid' in args:
            return '123'
        return ''
    instance.native_command = AsyncMock(side_effect=native)
    return instance


def intent(field='click'):
    return dict(ready=True, fullscreen=False, viewport=dict(width=1920, height=1080), **{field: dict(x=850, y=430)})


def clicks(browser):
    return [call.args for call in browser.native_command.await_args_list if 'click' in call.args]


@pytest.mark.asyncio
async def test_fullscreen_reveals_controls_then_clicks_once_and_verifies(browser):
    browser.companion.call.side_effect = [intent('reveal'), intent(), dict(ready=True, fullscreen=True)]
    await browser.control('fullscreen')
    assert clicks(browser) == [('xdotool', 'mousemove', '--sync', '850', '430', 'click', '1')]
    assert [call.args for call in browser.companion.call.await_args_list] == [('fullscreen',), ('fullscreen',), ('fullscreen_status',)]
    assert browser.youtube_status == 'Opened YouTube fullscreen'
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_existing_fullscreen_is_not_toggled_off(browser):
    browser.companion.call.return_value = dict(ready=True, fullscreen=True)
    await browser.control('fullscreen')
    assert not clicks(browser)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [
    dict(ready=False, interaction=True),
    dict(ready=False),
    dict(ready=True, viewport=dict(width=960, height=540), click=dict(x=5, y=5)),
    dict(ready=True, viewport=dict(width=1920, height=1080), click=dict(x=-1, y=5)),
    dict(ready=True, viewport=dict(width=1920, height=1080), click=dict(x=1920, y=5)),
    dict(ready=True, viewport=dict(width=1920, height=1080), click=dict(x=True, y=5)),
])
async def test_unsafe_or_unavailable_control_never_clicks(browser, result):
    browser.companion.call.return_value = result
    with pytest.raises(UserError, match='fullscreen control'):
        await browser.control('fullscreen')
    assert not clicks(browser)
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_foreign_active_window_never_receives_a_click(browser):
    native = browser.native_command.side_effect
    async def foreign(*args, **kwargs):
        return '999' if 'getwindowpid' in args else await native(*args, **kwargs)
    browser.native_command.side_effect = foreign
    with pytest.raises(UserError):
        await browser.control('fullscreen')
    browser.companion.call.assert_not_awaited()
    assert not clicks(browser)


@pytest.mark.asyncio
async def test_cancelled_click_verification_restores_preview_input(browser):
    entered = asyncio.Event()
    async def response(action, **kwargs):
        if action == 'fullscreen':
            return intent()
        entered.set()
        await asyncio.Event().wait()
    browser.companion.call.side_effect = response
    operation = asyncio.create_task(browser.control('fullscreen'))
    await entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert len(clicks(browser)) == 1
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_close_cancels_automatic_fullscreen_before_waiting_for_control_lock(browser):
    verifying, restoring, restored = asyncio.Event(), asyncio.Event(), asyncio.Event()
    release_restore = asyncio.Event()
    native = browser.native_command.side_effect
    async def response(action, **kwargs):
        if action == 'youtube_prepare':
            return dict(ready=True)
        if action == 'fullscreen':
            return intent()
        verifying.set()
        await asyncio.Event().wait()
    async def command(*args, **kwargs):
        if 'noviewonly' in args:
            restoring.set()
            await release_restore.wait()
            restored.set()
        return await native(*args, **kwargs)
    async def closed():
        assert restored.is_set()
        assert browser.control_lock.locked()
        assert automatic.done()
    browser.companion.call.side_effect = response
    browser.native_command.side_effect = command
    browser._close = AsyncMock(side_effect=closed)
    automatic = browser.youtube_task = asyncio.create_task(browser.prepare_youtube('1080p', False))
    closing = None
    try:
        await asyncio.wait_for(verifying.wait(), 1)
        closing = asyncio.create_task(browser.close())
        await asyncio.wait_for(restoring.wait(), 1)
        browser._close.assert_not_awaited()
        assert not closing.done()
        release_restore.set()
        await asyncio.wait_for(closing, 1)
        browser._close.assert_awaited_once()
        assert browser.youtube_task is None
        assert automatic.done()
    finally:
        release_restore.set()
        automatic.cancel()
        if closing is not None:
            closing.cancel()
        await asyncio.gather(automatic, *([closing] if closing is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_consent_stops_automatic_fullscreen_with_manual_status(browser):
    browser.companion.call.return_value = dict(ready=False, interaction=True)
    await browser.prepare_youtube('1080p', False)
    assert browser.youtube_status == 'Finish interacting with the page, then use Fullscreen video'
    browser.native_command.assert_not_awaited()


def test_controller_only_exposes_fixed_browser_status(setup):
    _, controller = setup
    controller.browser.youtube_status = 'Opened YouTube fullscreen'
    assert controller.status()['browser_status'] == 'Opened YouTube fullscreen'
    for value in ('private page text', {'unexpected': 'object'}, None):
        controller.browser.youtube_status = value
        assert controller.status()['browser_status'] == ''
    controller.browser.youtube_status = 'Opened YouTube fullscreen'
    controller.browser.running = False
    assert controller.status()['browser_status'] == ''
