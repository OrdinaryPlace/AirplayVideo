"""Fullscreen uses one guarded native shortcut and never types through a prompt."""
import asyncio
from pathlib import Path
import shutil
import subprocess
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


def intent():
    return dict(ready=True, fullscreen=False, shortcut='f')


def shortcuts(browser):
    return [call.args for call in browser.native_command.await_args_list if 'key' in call.args]


@pytest.mark.asyncio
async def test_fullscreen_sends_one_native_shortcut_and_verifies(browser):
    browser.companion.call.side_effect = [intent(), dict(ready=True, fullscreen=True)]
    await browser.control('fullscreen')
    assert shortcuts(browser) == [('xdotool', 'key', '--clearmodifiers', 'f')]
    assert [call.args for call in browser.companion.call.await_args_list] == [('fullscreen',), ('fullscreen_status',)]
    assert browser.youtube_status == 'Opened YouTube fullscreen'
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_existing_fullscreen_is_not_toggled_off(browser):
    browser.companion.call.return_value = dict(ready=True, fullscreen=True)
    await browser.control('fullscreen')
    assert not shortcuts(browser)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', [
    dict(ready=False, interaction=True),
    dict(ready=False),
    dict(ready=True, shortcut='F'),
    dict(ready=True, shortcut='x'),
    dict(ready=True, shortcut=None),
    dict(ready=True, shortcut='f', interaction=True),
    dict(ready=False, interaction=True, shortcut='f'),
])
async def test_unsafe_or_unavailable_control_never_types(browser, result):
    browser.companion.call.return_value = result
    with pytest.raises(UserError, match='fullscreen control'):
        await browser.control('fullscreen')
    assert not shortcuts(browser)
    assert not any('windowfocus' in call.args for call in browser.native_command.await_args_list)
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_foreign_active_window_never_receives_a_shortcut(browser):
    native = browser.native_command.side_effect
    async def foreign(*args, **kwargs):
        return '999' if 'getwindowpid' in args else await native(*args, **kwargs)
    browser.native_command.side_effect = foreign
    with pytest.raises(UserError):
        await browser.control('fullscreen')
    browser.companion.call.assert_not_awaited()
    assert not shortcuts(browser)


@pytest.mark.asyncio
async def test_window_change_after_page_check_does_not_receive_shortcut(browser):
    native = browser.native_command.side_effect
    windows = iter(('456', '789'))
    async def changed(*args, **kwargs):
        return next(windows) if 'getactivewindow' in args else await native(*args, **kwargs)
    browser.native_command.side_effect = changed
    browser.companion.call.return_value = intent()
    with pytest.raises(UserError):
        await browser.control('fullscreen')
    assert not shortcuts(browser)


@pytest.mark.asyncio
async def test_failed_fullscreen_confirmation_never_repeats_toggle(browser):
    browser.companion.call.side_effect = [intent()] + [dict(ready=True, fullscreen=False)] * 20
    with pytest.raises(UserError, match='fullscreen control'):
        await browser.control('fullscreen')
    assert shortcuts(browser) == [('xdotool', 'key', '--clearmodifiers', 'f')]
    assert 'noviewonly' in browser.native_command.await_args_list[-1].args


@pytest.mark.asyncio
async def test_cancelled_shortcut_verification_restores_preview_input(browser):
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
    assert len(shortcuts(browser)) == 1
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


def test_companion_shortcut_requires_focused_watch_video_without_text_entry():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for companion intent checks')
    source = (Path(__file__).parents[1] / 'companion/youtube.js').read_text()
    harness = r'''
const assert = require('node:assert/strict');
const element = (tag='div', editable=false) => ({
  isConnected:true, parentElement:null, isContentEditable:editable,
  matches:selectors=>selectors.split(',').map(value=>value.trim()).includes(tag),
  getBoundingClientRect:()=>({left:0,top:0,width:1920,height:1080}),
});
const video = {...element('video'), readyState:2};
let prompts=[], focused=true;
const player = {...element(), querySelector:selector=>selector==='video'?video:null,
  contains:child=>child===video};
global.location={origin:'https://www.youtube.com',pathname:'/watch'};
global.innerWidth=1920; global.innerHeight=1080;
global.getComputedStyle=()=>({display:'block',visibility:'visible',opacity:'1'});
global.document={querySelector:()=>player,querySelectorAll:()=>prompts,
  activeElement:element('body'),fullscreenEnabled:true,fullscreenElement:null,
  hasFocus:()=>focused};
const inspect=()=>youtubeControl({action:'fullscreen'});
assert.deepEqual(inspect(),{ready:true,fullscreen:false,shortcut:'f'});
for(const tag of ['input','textarea','select','iframe']){
  document.activeElement=element(tag);
  assert.equal(inspect().interaction,true,tag+' focus allowed a shortcut');
  assert.equal(inspect().shortcut,undefined);
}
document.activeElement=element('div',true);
assert.equal(inspect().interaction,true,'Editable content allowed a shortcut');
document.activeElement=element('body'); focused=false;
assert.equal(inspect().interaction,true,'Unfocused page allowed typing');
focused=true; prompts=[element()];
assert.equal(inspect().interaction,true,'Visible prompt allowed a shortcut');
prompts=[]; video.readyState=1;
assert.equal(inspect().ready,false,'Unready video allowed a shortcut');
video.readyState=2; location.pathname='/playlist';
assert.equal(inspect().interaction,true,'Non-watch page allowed a shortcut');
location.pathname='/watch'; location.origin='https://example.com';
assert.equal(inspect().interaction,true,'Foreign origin allowed a shortcut');
location.origin='https://www.youtube.com'; document.fullscreenEnabled=false;
assert.equal(inspect().interaction,true,'Unavailable fullscreen allowed a shortcut');
document.fullscreenEnabled=true; document.fullscreenElement=player;
assert.deepEqual(inspect(),{ready:true,fullscreen:true});
'''
    result = subprocess.run([node], input=source + '\n' + harness,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
