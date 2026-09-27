"""Uncertain or history navigation cannot reuse a stale requested URL."""
import asyncio
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from service.browser import Browser
from service import browser as browser_module
from service.companion import CompanionVersionMismatch
from service.model import UserError, default_setup


@pytest.fixture
def browser(tmp_path):
    instance = Browser(tmp_path, None)
    instance.running = True
    instance.url = "https://example.com/prior"
    instance.initial_navigation = "ready"
    instance.start = AsyncMock()
    instance.companion = SimpleNamespace(call=AsyncMock(return_value={"ok": True}))
    return instance


@pytest.mark.asyncio
async def test_navigation_tracks_url_only_after_acknowledgement(browser):
    entered, release = asyncio.Event(), asyncio.Event()

    async def call(action, **fields):
        assert action == "navigate"
        assert fields == {"url": "https://example.com/next", "new_tab": False}
        entered.set()
        await release.wait()
        return {"ok": True}

    browser.companion.call.side_effect = call
    navigation = asyncio.create_task(browser.navigate("https://example.com/next", default_setup()))
    await entered.wait()
    assert browser.url == ""
    release.set()
    await navigation
    assert browser.url == "https://example.com/next"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [UserError("Browser controls disconnected"), asyncio.CancelledError()])
async def test_uncertain_navigation_invalidates_previous_url(browser, error):
    browser.companion.call.side_effect = error
    with pytest.raises(type(error)):
        await browser.navigate("https://example.com/next", default_setup())
    assert browser.url == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["back", "forward"])
@pytest.mark.parametrize("failed", [False, True])
async def test_history_navigation_invalidates_url_even_when_acknowledgement_fails(browser, action, failed):
    if failed:
        browser.companion.call.side_effect = UserError("Browser controls disconnected")
        with pytest.raises(UserError):
            await browser.control(action)
    else:
        await browser.control(action)
    browser.companion.call.assert_awaited_once_with(action)
    assert browser.url == ""


@pytest.mark.asyncio
async def test_reload_retains_requested_address(browser):
    await browser.control("reload")
    assert browser.url == "https://example.com/prior"


@pytest.mark.asyncio
async def test_rejected_url_does_not_invalidate_current_page(browser):
    with pytest.raises(UserError):
        await browser.navigate("about:blank", default_setup())
    browser.start.assert_not_awaited()
    browser.companion.call.assert_not_awaited()
    assert browser.url == "https://example.com/prior"


@pytest.mark.asyncio
async def test_cold_navigation_creates_once_then_preserves_active_tab_history(browser):
    browser.initial_navigation = "new"
    await browser.navigate('https://example.com/first', default_setup())
    await browser.navigate('https://example.com/second', default_setup())
    assert [call.kwargs for call in browser.companion.call.await_args_list] == [
        dict(url='https://example.com/first', new_tab=True),
        dict(url='https://example.com/second', new_tab=False),
    ]
    assert browser.initial_navigation == 'ready'


@pytest.mark.asyncio
@pytest.mark.parametrize('error', [UserError('Acknowledgement lost'), asyncio.CancelledError()])
async def test_uncertain_cold_create_cannot_replay_after_companion_reconnect(browser, error):
    browser.initial_navigation = 'new'
    browser.companion.call.side_effect = error
    with pytest.raises(type(error)):
        await browser.navigate('https://example.com/first', default_setup())
    browser.companion.call.assert_awaited_once_with('navigate', url='https://example.com/first', new_tab=True)
    assert browser.initial_navigation == 'uncertain' and browser.url == ''
    browser.companion = SimpleNamespace(call=AsyncMock(return_value={'ok': True}))
    with pytest.raises(UserError, match='close and reopen'):
        await browser.navigate('https://example.com/retry', default_setup())
    browser.companion.call.assert_not_awaited()


def test_companion_creates_cold_foreground_tab_without_closing_startup_tabs():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node is required for companion navigation checks')
    source = (Path(__file__).parents[1] / 'companion/background.js').read_text()
    source = source[source.index('async function activeTab()'):source.index('function connect()')]
    harness = r'''
const assert=require('node:assert/strict');
let active=7;
const created=[], updated=[];
global.chrome={tabs:{
  query:async()=>[{id:active,windowId:11}],
  create:async options=>{created.push(options);active=9;return {id:9}},
  update:async(id,options)=>updated.push({id,options}),
}};
(async()=>{
  await command({action:'navigate',url:'https://example.com/first',new_tab:true});
  assert.deepEqual(created,[{windowId:11,url:'https://example.com/first',active:true}]);
  assert.equal(updated.length,0,'Cold navigation overwrote the startup tab');
  await command({action:'navigate',url:'https://example.com/second',new_tab:false});
  assert.deepEqual(updated,[{id:9,options:{url:'https://example.com/second'}}]);
  active=12; // A user's later tab selection retains ordinary navigation semantics.
  await command({action:'navigate',url:'https://example.com/third',new_tab:false});
  assert.equal(updated[1].id,12);
  assert.equal(created.length,1);
  await assert.rejects(command({action:'navigate',url:'javascript:alert(1)',new_tab:true}));
  assert.equal(created.length,1);
})().catch(error=>{console.error(error);process.exitCode=1});
'''
    result = subprocess.run([node], input=source + '\n' + harness,
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.asyncio
async def test_start_retries_stale_companion_exactly_once_before_navigation(tmp_path):
    browser = Browser(tmp_path, None)
    browser._start_once = AsyncMock(side_effect=[CompanionVersionMismatch("Outdated companion"), None])
    await browser.start(default_setup())
    assert browser._start_once.await_count == 2
    assert browser.url == ""


@pytest.mark.asyncio
async def test_second_stale_companion_stops_instead_of_restarting_again(tmp_path):
    browser = Browser(tmp_path, None)
    browser._start_once = AsyncMock(side_effect=CompanionVersionMismatch("Outdated companion"))
    with pytest.raises(CompanionVersionMismatch):
        await browser.start(default_setup())
    assert browser._start_once.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [UserError("Controls did not connect"), OSError("Startup failed"), asyncio.CancelledError()])
async def test_other_start_failures_do_not_restart(tmp_path, error):
    browser = Browser(tmp_path, None)
    browser._start_once = AsyncMock(side_effect=error)
    with pytest.raises(type(error)):
        await browser.start(default_setup())
    assert browser._start_once.await_count == 1


@pytest.mark.asyncio
async def test_upgrade_close_is_graceful_before_browser_is_ready(browser):
    browser.running = False
    browser.companion.close = AsyncMock()
    companion = browser.companion
    browser.chrome_process = SimpleNamespace(returncode=None, wait=AsyncMock(return_value=0))
    chrome = browser.chrome_process
    await browser._close(graceful=True)
    companion.call.assert_awaited_once_with("close")
    chrome.wait.assert_awaited_once()
    companion.close.assert_awaited_once()
    assert browser.companion is None and browser.chrome_process is None
    assert browser.initial_navigation == 'new'


@pytest.mark.asyncio
@pytest.mark.parametrize('cancellations', [1, 2])
async def test_cancel_during_upgrade_close_finishes_cleanup_without_restart(tmp_path, monkeypatch, cancellations):
    browser = Browser(tmp_path, None)
    monkeypatch.setattr(browser_module.os, 'chown', lambda *args: None)
    monkeypatch.setattr(browser_module, 'install_companion', lambda root: 'a' * 32)
    probe = SimpleNamespace(wait=AsyncMock(return_value=0))
    monkeypatch.setattr(browser_module.asyncio, 'create_subprocess_exec', AsyncMock(return_value=probe))
    companion = SimpleNamespace(path=tmp_path/'controls.sock', start=AsyncMock(),
                                wait_ready=AsyncMock(side_effect=CompanionVersionMismatch('Outdated companion')))
    monkeypatch.setattr(browser_module, 'Companion', lambda *args: companion)
    browser._private_file = lambda *args: None
    browser.launch = AsyncMock(return_value=SimpleNamespace())
    browser.start_display = AsyncMock()
    browser.wait_port = AsyncMock()
    browser._start_once = AsyncMock(wraps=browser._start_once)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    async def close(*, graceful=False):
        assert graceful
        entered.set()
        await release.wait()
        browser.children.clear()
        browser.companion = None
        finished.set()
    browser._close = AsyncMock(side_effect=close)
    starting = asyncio.create_task(browser.start(default_setup()))
    await entered.wait()
    for _ in range(cancellations):
        starting.cancel()
        await asyncio.sleep(0)
        assert not starting.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await starting
    assert finished.is_set() and not browser.children and browser.companion is None
    assert browser._start_once.await_count == 1
