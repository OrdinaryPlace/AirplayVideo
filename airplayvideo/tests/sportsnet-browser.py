"""Real Chrome/native-input fixture with synthetic sign-in pages only.

This qualifies the private companion, native input and fail-closed controls. The
fixture's in-page suggestion deliberately stands in for Chrome's password popup;
it does not claim that a real password store or Sportsnet account was tested.
"""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch

import aiohttp
from aiohttp import web

from service.browser import Browser
from service.companion import install as install_companion
from service.model import default_setup, UserError


async def main():
    os.umask(0o077)
    reports = []
    origins = {}

    async def report(request):
        value = await request.json()
        # Even synthetic credential strings stay in the page. Only a tiny
        # account-free result crosses into test reporting or assertion output.
        assert set(value) <= {'stage', 'event', 'trusted', 'filled', 'submitted', 'webdriver'}
        assert value.get('stage') in {'email', 'password', 'verification', 'home', 'watch'}
        assert value.get('event') in {'ready', 'click', 'arrow-down', 'tab', 'select', 'advance', 'unexpected-submit'}
        assert value.get('webdriver') is False
        for key in ('trusted', 'filled', 'submitted'):
            assert key not in value or type(value[key]) is bool
        reports.append(value)
        return web.Response(text='ok')

    async def fixture(request):
        stage = request.match_info.get('stage', 'watch')
        saved = request.query.get('saved', 'yes') == 'yes'
        if stage not in {'email', 'password', 'verification', 'home', 'watch'}:
            raise web.HTTPNotFound()
        if stage in {'email', 'password'}:
            # Chrome may restore values across multi-step forms that reuse an
            # identifier. Each synthetic scenario has its own field/form name;
            # password also uses a separate origin below. This fixture requires
            # the native selection path, not an already-filled fast path.
            variant = stage + ('-suggestion' if saved else '-missing')
            identity = 'fixture-' + variant + '-field'
            field = ('<input data-fixture-field id="' + identity + '" name="' + identity + '" type="email" autocomplete="username" aria-label="Email">'
                     if stage == 'email' else
                     '<input data-fixture-field id="' + identity + '" name="' + identity + '" type="password" autocomplete="current-password" aria-label="Password">')
            label = 'Continue' if stage == 'email' else 'Sign in'
            content = '<h1>Sign in</h1><form id="fixture-' + variant + '-form" name="fixture-' + variant + '-form">' + field + '<button>' + label + '</button></form><div id="suggestion" hidden>Saved login fixture</div>'
        elif stage == 'verification':
            content = '<h1>Enter verification code</h1><input type="text" autocomplete="one-time-code"><button>Continue</button>'
        elif stage == 'home':
            content = '<nav class="account-nav--signed-in"><button class="account-nav__profile" aria-label="Account">Account</button></nav><h1>Signed-in fixture</h1>'
        else:
            content = '<nav class="account-nav--signed-in"><button class="account-nav__profile" aria-label="Account">Account</button></nav><video autoplay muted></video><canvas hidden width="32" height="32"></canvas>'
        next_url = (origins['auth'] + '/password' if stage == 'email'
                    else origins['watch'] + '/home')
        script = r'''
        const stage=STAGE, saved=SAVED, next=NEXT;
        const credential=document.querySelector('[data-fixture-field]');
        const login=document.querySelector('form'),suggestion=document.querySelector('#suggestion');
        const report=(event,extra={})=>fetch('/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({stage,event,webdriver:navigator.webdriver,...extra})});
        let clicked=false,selected=false,shownAt=0;
        if(stage==='email'||stage==='password'){
          credential.addEventListener('click',event=>{
            clicked=event.isTrusted;
            if(saved&&clicked){suggestion.hidden=false;shownAt=performance.now();}
            report('click',{trusted:event.isTrusted,filled:credential.value.length>0});
          });
          credential.addEventListener('keydown',event=>{
            if(event.key==='ArrowDown')report('arrow-down',{trusted:event.isTrusted,filled:credential.value.length>0});
            if(event.key==='Tab')report('tab',{trusted:event.isTrusted,filled:credential.value.length>0});
            if(event.key==='ArrowDown'&&event.isTrusted&&clicked&&saved)selected=true;
            if(event.key==='Tab'&&event.isTrusted&&selected&&performance.now()-shownAt>=500){
              // Synthetic suggestion UI: the real native click and keys are
              // required, while no real Chrome password store is accessed.
              credential.value=stage==='email'?'fixture@example.invalid':'synthetic-fixture-only';
              credential.dispatchEvent(new Event('input',{bubbles:true}));
              suggestion.hidden=true;
              report('select',{trusted:true,filled:true});
            }
          });
          login.addEventListener('submit',event=>{
            event.preventDefault();
            if(!credential.value.length){report('unexpected-submit',{submitted:true});return;}
            report('advance',{filled:true,submitted:true}).then(()=>location.href=next);
          });
        }
        if(stage==='watch'){
          const canvas=document.querySelector('canvas'),context=canvas.getContext('2d'),video=document.querySelector('video');
          let frame=0;
          const paint=()=>{context.fillStyle=++frame%2?'#338855':'#884433';context.fillRect(0,0,32,32);requestAnimationFrame(paint)};
          paint();video.srcObject=canvas.captureStream(12);video.play();
        }
        requestAnimationFrame(()=>requestAnimationFrame(()=>report('ready',{filled:!!credential&&credential.value.length>0})));
        '''
        script = script.replace('STAGE', json.dumps(stage)).replace('SAVED', json.dumps(saved)).replace('NEXT', json.dumps(next_url))
        return web.Response(content_type='text/html', text='''<!doctype html><meta charset="utf-8"><title>Synthetic sign-in fixture</title>
        <style>body{font:24px sans-serif;margin:0;padding:8vh 12vw}input{display:block;width:60vw;height:80px;margin:40px 0}button{font:inherit;padding:20px}#suggestion{position:absolute;top:38vh;left:20vw;background:#ddd;padding:16px}video{width:80vw;height:60vh}</style>'''+content+'<script>'+script+'</script>')

    app = web.Application()
    app.router.add_post('/report', report)
    app.router.add_get('/watch/fixture', fixture)
    app.router.add_get('/{stage}', fixture)
    runner = web.AppRunner(app)
    await runner.setup()
    # CI runs this container with --network none and no published ports. Distinct
    # loopback aliases preserve the production watch/account-origin distinction.
    site = web.TCPSite(runner, '0.0.0.0', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    origins.update(watch=f'http://127.0.0.1:{port}', account=f'http://127.0.0.2:{port}', auth=f'http://127.0.0.3:{port}')

    async def state_is(browser, expected, **fields):
        async def wait():
            while True:
                result = await browser.sportsnet_status()
                if result.get('stage') == expected and all(result.get(key) == value for key, value in fields.items()):
                    return result
                await asyncio.sleep(0.1)
        return await asyncio.wait_for(wait(), 15)

    async def navigate(browser, origin, path):
        with patch('service.browser.browser_profile', return_value='sportsnet'):
            await browser.navigate(origins[origin] + path, default_setup())

    def diagnostic(point, result=None):
        # Fixed enum/boolean/count projection only: never field contents, URLs,
        # screenshots, browser errors or arbitrary request data.
        events = {}
        for row in reports:
            key = row['stage'] + ':' + row['event']
            detail = events.setdefault(key, {'count': 0, 'trusted': False, 'filled': False})
            detail['count'] += 1
            detail['trusted'] |= row.get('trusted') is True
            detail['filled'] |= row.get('filled') is True
        stages = {'email', 'password', 'verification', 'signed_in', 'playing', 'unknown', 'loading', 'other', 'error', 'permission', 'signin_choice'}
        state = None if result is None else {
            'stage': result.get('stage') if result.get('stage') in stages else 'unknown',
            'filled': result.get('filled') is True,
            'has_rect': isinstance(result.get('rect'), dict),
        }
        print('Synthetic sign-in evidence: ' + json.dumps({'point': point, 'state': state, 'events': events}, sort_keys=True), flush=True)

    async def trusted_selection(stage, result):
        diagnostic(stage + '_after_autofill', result)
        async def wait():
            while not any(row['stage'] == stage and row['event'] == 'select' and row['trusted'] for row in reports):
                await asyncio.sleep(0.05)
        try:
            await asyncio.wait_for(wait(), 5)
        except TimeoutError:
            diagnostic(stage + '_selection_missing', result)
            raise

    try:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            root = Path(directory)
            root.chmod(0o755)
            source = root / 'fixture-companion'
            shutil.copytree(Path(__file__).parents[1] / 'companion', source)
            replacements = {
                'https://watch.sportsnet.ca': origins['watch'],
                'https://account.sportsnet.ca': origins['account'],
                'https://rogers-prd.transmit.security': origins['auth'],
            }
            for name in ('sportsnet.js', 'background.js'):
                text = (source / name).read_text()
                for original, replacement in replacements.items():
                    assert original in text, 'Fixture origin adaptation no longer matches production guard'
                    text = text.replace(original, replacement)
                (source / name).write_text(text)
            manifest = json.loads((source / 'manifest.json').read_text())
            manifest['host_permissions'] = ['http://127.0.0.1/*', 'http://127.0.0.2/*', 'http://127.0.0.3/*']
            # This fixture exercises already-authorized page controls. It does
            # not auto-accept or qualify the real optional-permission prompt.
            manifest.pop('optional_host_permissions', None)
            (source / 'manifest.json').write_text(json.dumps(manifest))
            resources.enter_context(patch('service.browser.install_companion', lambda path: install_companion(path, source=source)))
            resources.enter_context(patch('service.companion.SOURCE', source))
            async with aiohttp.ClientSession() as session:
                browser = Browser(root / 'data', session)
                try:
                    await navigate(browser, 'account', '/email')
                    initial = await state_is(browser, 'email', filled=False)
                    diagnostic('email_before_autofill', initial)
                    argv = Path(f'/proc/{browser.chrome_process.pid}/cmdline').read_bytes().split(b'\0')
                    assert not any(arg.startswith((b'--remote-debugging', b'--enable-automation', b'--no-sandbox', b'--headless')) for arg in argv)
                    assert not (browser.root / 'profile/DevToolsActivePort').exists()
                    result = await browser.sportsnet_autofill('email')
                    diagnostic('email_autofill_returned', result)
                    assert result['stage'] == 'email' and result['filled']
                    await trusted_selection('email', result)
                    assert not any(row['event'] in {'advance', 'unexpected-submit'} for row in reports), 'Autofill submitted a form'
                    await browser.sportsnet_step('email')
                    initial = await state_is(browser, 'password', filled=False)
                    diagnostic('password_before_autofill', initial)
                    result = await browser.sportsnet_autofill('password')
                    diagnostic('password_autofill_returned', result)
                    assert result['stage'] == 'password' and result['filled']
                    await trusted_selection('password', result)
                    await browser.sportsnet_step('password')
                    await state_is(browser, 'signed_in')
                    assert sum(row['event'] == 'advance' for row in reports) == 2
                    print('PASS: sandboxed Chrome companion; trusted native field click and Down/Tab; separate guarded email/password actions; no credential values in replies', flush=True)

                    # No suggestion: Down/Tab must leave the field empty and must
                    # not accidentally submit, even with the form focused.
                    before = sum(row['event'] == 'advance' for row in reports)
                    await navigate(browser, 'account', '/email?saved=no')
                    await state_is(browser, 'email', filled=False)
                    try:
                        result = await browser.sportsnet_autofill('email')
                    except UserError:
                        result = await browser.sportsnet_status()
                    assert result['stage'] == 'email' and not result['filled']
                    try:
                        await browser.sportsnet_step('email')
                    except UserError:
                        pass
                    assert (await browser.sportsnet_status())['stage'] == 'email'
                    assert sum(row['event'] == 'advance' for row in reports) == before
                    assert not any(row['event'] == 'unexpected-submit' for row in reports)
                    print('PASS: missing synthetic suggestion cannot submit an empty form', flush=True)

                    await navigate(browser, 'account', '/verification')
                    await state_is(browser, 'verification')
                    try:
                        await browser.sportsnet_autofill('email')
                    except UserError:
                        pass
                    assert (await browser.sportsnet_status())['stage'] == 'verification'
                    assert sum(row['event'] == 'advance' for row in reports) == before
                    assert 'viewonly:0' in await browser.native_command('x11vnc', '-display', browser.environment['DISPLAY'], '-auth', browser.environment['XAUTHORITY'], '-Q', 'viewonly', '-sync')
                    print('PASS: verification blocks autofill and preview input is restored', flush=True)

                    await navigate(browser, 'watch', '/watch/fixture')
                    first = await state_is(browser, 'playing')
                    await asyncio.sleep(0.4)
                    second = await state_is(browser, 'playing')
                    assert second['video_time'] > first['video_time']
                    assert second['paused'] is False
                    print('PASS: account-free synthetic canvas video advances after returning to watch origin', flush=True)
                    print('LIMIT: synthetic suggestion exercises native input; real Chrome password-store and Sportsnet account acceptance remain untested', flush=True)
                finally:
                    await browser.close()
    finally:
        await runner.cleanup()


asyncio.run(asyncio.wait_for(main(), 150))
