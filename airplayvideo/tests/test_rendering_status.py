"""Graphics evidence never exports argv, profile data or unverified process claims."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from service import browser as browser_module
from service import rendering_status as module
from service.browser import Browser
from service.model import UserError, default_setup


def arguments(mode='opengl', node='renderD128'):
    values = ['--use-gl=angle', '--use-angle=' + ('gl' if mode == 'opengl' else 'vulkan'),
              '--enable-features=AcceleratedVideoDecoder,AcceleratedVideoDecodeLinuxGL']
    return values + ['--render-node-override=/dev/dri/' + node]


def process(root, pid, parent=1, started=100, *, uid=1000, namespace='pid:[41]', gpu=False, fds=()):
    directory = root / str(pid)
    directory.mkdir()
    fields = ['S', str(parent)] + ['0'] * 17 + [str(started)]
    (directory / 'stat').write_text(f'{pid} (chrome (fixture)) ' + ' '.join(fields))
    (directory / 'status').write_text('Uid:\t' + '\t'.join([str(uid)] * 4) + '\n')
    (directory / 'ns').mkdir()
    (directory / 'ns' / 'pid').symlink_to(namespace)
    (directory / 'cmdline').write_bytes(b'chrome\0' +
        (b'--type=gpu-process\0' if gpu else b'--type=renderer\0') +
        b'--fixture-private-auth=must-not-be-exported\0')
    (directory / 'fd').mkdir()
    for index, target in enumerate(fds):
        (directory / 'fd' / str(index)).symlink_to(target)
    return directory


@pytest.fixture
def proc(tmp_path):
    root = tmp_path / 'proc'
    (root / 'self' / 'ns').mkdir(parents=True)
    (root / 'self' / 'ns' / 'pid').symlink_to('pid:[41]')
    process(root, 100)
    return root


def owner(proc):
    value = module.capture_identity(100, proc)
    assert value is not None
    return value


@pytest.mark.parametrize('mode', ['opengl','vulkan'])
def test_launch_projection_exports_only_fixed_modes_and_render_basename(mode):
    result = module.launch_status('sportsnet', mode, arguments(mode) + [
        '--fixture-private-secret=must-not-be-exported', 'https://fixture-private-account.invalid/'])
    assert result == {'active_profile':'sportsnet', 'launch_rendering':mode,
                      'gpu_flags_applied':True, 'render_node':'renderD128'}
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('node', ['../../private', 'renderD128?private', 'renderD１２８', 'card0', 'renderD1234567'])
def test_invalid_device_names_never_appear_in_readback(node):
    result = module.launch_status('sportsnet', 'opengl', arguments(node=node))
    assert result['render_node'] is None and result['gpu_flags_applied'] is False


def test_no_usable_device_is_reported_as_no_applied_gpu_flags():
    assert module.launch_status('sportsnet', 'opengl', []) == {
        'active_profile':'sportsnet', 'launch_rendering':'opengl',
        'gpu_flags_applied':False, 'render_node':None}


def test_gpu_evidence_is_restricted_to_owned_descendants_and_never_leaks_argv(proc):
    process(proc, 101, parent=100, gpu=True, fds=['/dev/dri/renderD128','/fixture-private-file'])
    process(proc, 102, parent=100)
    # Unrelated argv/FD files need not even be readable or present.
    unrelated = process(proc, 200, gpu=True, uid=0, namespace='pid:[99]')
    (unrelated / 'cmdline').unlink()
    result = module.gpu_process_status(owner(proc), proc)
    assert result == {'gpu_process_count':1, 'gpu_render_device_open':True}
    assert not any(token in json.dumps(result) for token in ('private','renderD128','cmdline','100','101'))


def test_exact_gpu_token_is_required_and_complete_empty_scan_can_return_false(proc):
    child = process(proc, 101, parent=100, fds=['/dev/dri/renderD128'])
    (child / 'cmdline').write_bytes(b'chrome\0--fixture=--type=gpu-process\0')
    assert module.gpu_process_status(owner(proc), proc) == {
        'gpu_process_count':0, 'gpu_render_device_open':False}


@pytest.mark.parametrize('mismatch', ['uid','namespace','starttime','root_namespace'])
def test_identity_or_namespace_mismatch_never_claims_no_gpu(proc, mismatch):
    child = process(proc, 101, parent=100, gpu=True, fds=['/dev/dri/renderD128'])
    identity = owner(proc)
    if mismatch == 'uid':
        (child / 'status').write_text('Uid:\t0\t0\t0\t0\n')
    elif mismatch == 'namespace':
        (child / 'ns' / 'pid').unlink()
        (child / 'ns' / 'pid').symlink_to('pid:[42]')
    elif mismatch == 'starttime':
        (proc / '100' / 'stat').write_text('100 (other) ' + ' '.join(['S','1']+['0']*17+['101']))
    else:
        (proc / 'self' / 'ns' / 'pid').unlink()
        (proc / 'self' / 'ns' / 'pid').symlink_to('pid:[42]')
    assert module.gpu_process_status(identity, proc) == {
        'gpu_process_count':None, 'gpu_render_device_open':None}


def test_permission_denied_fd_read_is_unknown_not_false(proc, monkeypatch):
    process(proc, 101, parent=100, gpu=True, fds=['/dev/dri/renderD128'])
    readlink = Path.readlink
    def restricted(path):
        if path.parent.name == 'fd':
            raise PermissionError('fixture-private-kernel-detail')
        return readlink(path)
    monkeypatch.setattr(Path, 'readlink', restricted)
    assert module.gpu_process_status(owner(proc), proc) == {
        'gpu_process_count':None, 'gpu_render_device_open':None}


def test_pid_reuse_during_fd_scan_invalidates_evidence(proc, monkeypatch):
    child = process(proc, 101, parent=100, gpu=True, fds=['/dev/dri/renderD128'])
    readlink = Path.readlink
    def replaced(path):
        value = readlink(path)
        if path.parent.name == 'fd':
            (child / 'stat').write_text('101 (replacement) ' + ' '.join(['S','100']+['0']*17+['101']))
        return value
    monkeypatch.setattr(Path, 'readlink', replaced)
    assert module.gpu_process_status(owner(proc), proc) == {
        'gpu_process_count':None, 'gpu_render_device_open':None}


def test_pid_budget_does_not_materialize_unbounded_process_list(proc, monkeypatch):
    for pid in range(101, 230):
        process(proc, pid)
    identity, observed = owner(proc), set()
    stat = module._stat
    def counted(root, pid):
        observed.add(pid)
        return stat(root, pid)
    monkeypatch.setattr(module, '_stat', counted)
    assert module.gpu_process_status(identity, proc)['gpu_process_count'] is None
    assert len(observed) <= module.MAX_PROCESSES


def test_fd_budget_is_global_across_gpu_processes(proc, monkeypatch):
    process(proc, 101, parent=100, gpu=True, fds=['/fixture-private-file'] * 129)
    process(proc, 102, parent=100, gpu=True, fds=['/dev/dri/renderD128'] * 129)
    readlink, count = Path.readlink, 0
    def counted(path):
        nonlocal count
        if path.parent.name == 'fd': count += 1
        return readlink(path)
    monkeypatch.setattr(Path, 'readlink', counted)
    assert module.gpu_process_status(owner(proc), proc) == {
        'gpu_process_count':None, 'gpu_render_device_open':None}
    assert count == module.MAX_FDS


def test_unsupported_proc_and_invalid_owned_pid_are_unknown(tmp_path):
    for pid in (None, False, 0, -1, 2**31, '100'):
        assert module.capture_identity(pid, tmp_path) is None
    assert module.gpu_process_status(None, tmp_path) == {
        'gpu_process_count':None, 'gpu_render_device_open':None}


@pytest.mark.parametrize('pid,code,alive', [(100,None,True),(100,0,False),(100,-9,False),(False,None,False),(0,None,False)])
def test_browser_status_uses_owned_process_liveness_and_fixed_fields(tmp_path, monkeypatch, pid, code, alive):
    browser = Browser(tmp_path, None)
    browser.chrome_process = SimpleNamespace(pid=pid, returncode=code)
    browser._graphics_launch = module.launch_status('sportsnet','opengl',arguments())
    monkeypatch.setattr(browser_module, 'gpu_process_status', lambda identity: {
        'gpu_process_count':None,'gpu_render_device_open':None})
    result = browser.graphics_status()
    assert result['chrome_alive'] is alive
    assert result['launch_rendering'] == ('opengl' if alive else None)
    assert set(result) == {'chrome_alive','active_profile','launch_rendering','gpu_flags_applied',
                           'render_node','gpu_process_count','gpu_render_device_open'}


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False,True])
async def test_launch_records_same_gpu_invocation_and_cleanup_clears_evidence(tmp_path, monkeypatch, failure):
    browser = Browser(tmp_path, None)
    monkeypatch.setattr(browser_module.os, 'chown', lambda *args: None)
    monkeypatch.setattr(browser_module, 'install_companion', lambda root:'a'*32)
    monkeypatch.setattr(browser_module.asyncio, 'create_subprocess_exec', AsyncMock(
        return_value=SimpleNamespace(wait=AsyncMock(return_value=0))))
    companion = SimpleNamespace(path=tmp_path/'controls.sock', start=AsyncMock(), close=AsyncMock(), call=AsyncMock(),
        wait_ready=AsyncMock(side_effect=UserError('fixture startup failed') if failure else None))
    monkeypatch.setattr(browser_module, 'Companion', lambda *args:companion)
    gpu = Mock(return_value=arguments())
    monkeypatch.setattr(browser_module, 'chrome_gpu_arguments', gpu)
    monkeypatch.setattr(browser_module, 'capture_identity', lambda pid:None)
    browser._private_file = lambda *args:None
    browser.launch = AsyncMock(return_value=SimpleNamespace(pid=2**31-1, returncode=None, wait=AsyncMock(return_value=0)))
    browser.start_display, browser.wait_port = AsyncMock(), AsyncMock()
    settings = default_setup()
    settings['browser']['sportsnet_rendering'] = 'opengl'
    if failure:
        with pytest.raises(UserError):
            await browser.start(settings, profile='sportsnet')
        assert browser._graphics_launch is None and browser._chrome_identity is None
        assert browser.graphics_status()['launch_rendering'] is None
    else:
        await browser.start(settings, profile='sportsnet')
        chrome = next(call.args for call in browser.launch.await_args_list if call.args[0]=='google-chrome')
        assert all(value in chrome for value in arguments())
        gpu.assert_called_once_with(rendering='opengl')
        assert browser.graphics_status()['launch_rendering'] == 'opengl'
        gpu.assert_called_once()  # Status did not recompute current device selection.
        await browser._close()
        assert browser._graphics_launch is None and browser._chrome_identity is None
        assert browser.graphics_status()['launch_rendering'] is None


def test_reused_main_pid_removes_active_launch_claims(proc, tmp_path, monkeypatch):
    browser = Browser(tmp_path, None)
    browser.chrome_process = SimpleNamespace(pid=100, returncode=None)
    browser._chrome_identity = owner(proc)
    browser._graphics_launch = module.launch_status('sportsnet','opengl',arguments())
    (proc / '100' / 'stat').write_text('100 (replacement) ' + ' '.join(['S','1']+['0']*17+['101']))
    monkeypatch.setattr(browser_module, 'owner_current', lambda identity:module.owner_current(identity,proc))
    result = browser.graphics_status()
    assert result['chrome_alive'] is False and result['launch_rendering'] is None
    assert result['gpu_process_count'] is None and result['gpu_render_device_open'] is None
