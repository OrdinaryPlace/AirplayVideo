"""Real sandboxed Chrome and native controls in an isolated Linux container."""
import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
from unittest.mock import patch
import aiohttp
from aiohttp import web
from service.browser import Browser
from service.companion import install as install_companion
from service.model import default_setup, UserError


async def main():
    os.umask(0o077)
    reports = asyncio.Queue()
    latest_video = {}
    async def report(request):
        value = await request.json()
        if value.get('path') == '/watch' and value.get('sequence', 0) >= latest_video.get('sequence', 0):
            latest_video.clear()
            latest_video.update(value)
        await reports.put(value)
        return web.Response(text='ok')
    async def fixture(request):
        return web.Response(content_type='text/html', text='''<!doctype html><title>Control fixture</title>
        <form id="form"><input id="field" type="password" autofocus style="position:fixed;left:20vw;top:20vh;width:60vw;height:50vh"><button>Submit</button></form>
        <script>
        const prior=localStorage.getItem('retained');localStorage.setItem('retained','yes');
        const report=event=>fetch('/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:location.pathname,event,webdriver:navigator.webdriver,value:field.value,retained:prior,fullscreen_element:!!document.fullscreenElement})});
        addEventListener('pageshow',()=>{field.focus();requestAnimationFrame(()=>requestAnimationFrame(()=>report('ready')))});
        field.addEventListener('input',()=>report('input'));
        form.addEventListener('submit',e=>{e.preventDefault();report('submitted')});
        </script>''')
    async def video_fixture(request):
        # The account-free fixture uses the production read-only shortcut intent;
        # only its origin guard is adapted. Native X input must supply the keypress.
        script = (Path(__file__).parents[1] / 'companion/youtube.js').read_text()
        assert not any(term in script for term in ('__airplayVideoFit', 'style.setProperty', 'setInterval(', '.requestFullscreen('))
        script = script.replace("location.origin !== 'https://www.youtube.com'", "location.origin !== new URL(location.href).origin")
        return web.Response(content_type='text/html', text='''<!doctype html><html style="overflow:auto"><body style="margin:0;overflow:scroll">
        <style>#movie_player{position:relative;width:640px;height:360px;background:#183849}video{width:100%;height:100%}.ytp-fullscreen-button{position:absolute;right:0;bottom:0;width:96px;height:48px;opacity:0}</style>
        <input id="search" aria-label="Search fixture"><main><div id="movie_player"><video></video><button class="ytp-fullscreen-button">Fullscreen</button></div></main><aside style="height:2400px">Scrollable page</aside>
        <script>''' + script + '''
        let keyTrusted=false, replaced=false, sequence=0;
        const assert=(value,message)=>{if(!value)throw new Error(message)};
        const roots=[document.documentElement,document.body,document.querySelector('main')];
        const original=roots.map(element=>element.getAttribute('style'));
        const inspect=()=>{
          const player=document.querySelector('#movie_player'), before=player.getAttribute('style');
          const control=youtubeControl({action:'fullscreen'});
          assert(roots.every((element,index)=>element.getAttribute('style')===original[index]),'Fullscreen control changed page styles');
          assert(player.getAttribute('style')===before,'Fullscreen control changed player styles');
          return control;
        };
        const report=(event,error='')=>{
          const player=document.querySelector('#movie_player'), rect=player.getBoundingClientRect();
          return fetch('/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:'/watch',event,sequence:++sequence,webdriver:navigator.webdriver,error,control:inspect(),trusted:keyTrusted,fullscreen_element:!!document.fullscreenElement,covers_display:Math.round(rect.width*devicePixelRatio)===screen.width&&Math.round(rect.height*devicePixelRatio)===screen.height,visible:getComputedStyle(player).visibility==='visible',styles_unchanged:roots.every((element,index)=>element.getAttribute('style')===original[index])})});
        };
        function wirePlayer(){
          const player=document.querySelector('#movie_player');
          Object.defineProperty(player.querySelector('video'),'readyState',{value:2});
        }
        wirePlayer();
        document.addEventListener('keydown',event=>{
          if(event.key!=='f'||event.altKey||event.ctrlKey||event.metaKey)return;
          if(inspect().shortcut!=='f')return;
          keyTrusted=event.isTrusted;
          document.querySelector('#movie_player').requestFullscreen().catch(()=>report('fullscreen-error','Native fullscreen request failed'));
        });
        document.addEventListener('fullscreenchange',()=>requestAnimationFrame(async()=>{
          if(document.fullscreenElement)report('fullscreen');
          else{
            await report('exit');
            if(!replaced){
              const prior=document.querySelector('#movie_player'), next=prior.cloneNode(true);
              next.querySelector('button').style.opacity='0';prior.replaceWith(next);replaced=true;wirePlayer();report('replacement');
            }
          }
        }));
        requestAnimationFrame(()=>{
          let error='';
          try{
            const dialog=document.createElement('div');dialog.setAttribute('role','dialog');dialog.setAttribute('aria-modal','true');dialog.textContent='Consent fixture';document.body.append(dialog);
            assert(inspect().interaction===true,'Consent did not block fullscreen intent');dialog.remove();
            search.focus();assert(inspect().interaction===true,'Text entry did not block fullscreen intent');search.blur();
            assert(inspect().shortcut==='f','Hidden native controls blocked the keyboard shortcut');
          }catch(caught){error=caught.message}
          report('intent',error);
        });
        </script></body></html>''')
    app = web.Application()
    app.router.add_post('/report', report)
    app.router.add_get('/watch', video_fixture)
    app.router.add_get('/{browser}/{page}', fixture)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
    async def expect(path, event='ready', value=None):
        async def read():
            while True:
                result = await reports.get()
                assert result['webdriver'] is False, 'Chrome must be an ordinary browser'
                assert result['event'] != 'submitted', 'Paste must never submit a form'
                if result['path'] == path and result['event'] == event and (value is None or value == result['value']):
                    return result
        return await asyncio.wait_for(read(), 15)
    try:
        with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
            Path(directory).chmod(0o755)
            old_companion = Path(directory) / 'old-companion'
            shutil.copytree(Path(__file__).parents[1] / 'companion', old_companion)
            old_manifest = json.loads((old_companion / 'manifest.json').read_text())
            old_manifest['version'] = '1.0.1'
            (old_companion / 'manifest.json').write_text(json.dumps(old_manifest))
            # Version 1.0.1 kept its native port alive without handling updates;
            # the upgrade must exercise that real installed lifecycle.
            old_background = (old_companion / 'background.js').read_text()
            old_background = old_background.replace("chrome.runtime.onUpdateAvailable.addListener(() => chrome.runtime.reload());\n", '')
            (old_companion / 'background.js').write_text(old_background)
            for port in (5900, 9222):
                listener = resources.enter_context(socket.socket())
                listener.bind(('127.0.0.1', port))
                listener.listen()
            async with aiohttp.ClientSession() as session:
                browsers = [Browser(Path(directory) / name, session) for name in ('one', 'two')]
                try:
                    for number, browser in enumerate(browsers):
                        with ExitStack() as initial_version:
                            if number == 0:
                                initial_version.enter_context(patch('service.browser.install_companion', lambda root: install_companion(root, source=old_companion)))
                                initial_version.enter_context(patch('service.companion.SOURCE', old_companion))
                            await browser.navigate(base + f'/{number}/first', default_setup())
                        expected_version = '1.0.1' if number == 0 else '1.0.4'
                        assert (await browser.companion.call('ready'))['version'] == expected_version
                        await expect(f'/{number}/first')
                        argv = Path(f'/proc/{browser.chrome_process.pid}/cmdline').read_bytes().split(b'\0')
                        assert not any(arg.startswith((b'--remote-debugging', b'--enable-automation', b'--no-sandbox', b'--headless')) for arg in argv)
                        assert not (browser.root / 'profile/DevToolsActivePort').exists()
                        reader, writer = await browser.preview_connection()
                        writer.close()
                        await writer.wait_closed()
                        # Unicode and punctuation go to the selected native field,
                        # never a command line or a page-evaluation endpoint.
                        sample = 'Test-é-日本-☀️-"-$`'
                        await browser.native_command('xdotool', 'mousemove', '960', '540', 'click', '1')
                        await browser.paste(sample)
                        await expect(f'/{number}/first', 'input', sample)
                        assert await browser.native_command('python3', '-c', "from Xlib import display; d=display.Display(); print(bool(d.get_selection_owner(d.intern_atom('CLIPBOARD'))))") == 'False'
                        assert 'viewonly:0' in await browser.native_command('x11vnc', '-display', browser.environment['DISPLAY'], '-auth', browser.environment['XAUTHORITY'], '-Q', 'viewonly', '-sync')
                        try:
                            await browser.paste('must not submit\n')
                            raise AssertionError('Control characters were accepted')
                        except UserError:
                            pass
                        print(f'Browser {number + 1}: native Unicode paste, cleared clipboard and restored preview input passed', flush=True)
                        await browser.navigate(base + f'/{number}/second', default_setup())
                        await expect(f'/{number}/second')
                        await browser.control('back')
                        await expect(f'/{number}/first')
                        await browser.control('forward')
                        await expect(f'/{number}/second')
                        await browser.control('reload')
                        await expect(f'/{number}/second')
                        await browser.control('zoom_in')
                        await browser.control('zoom_reset')
                    assert browsers[0].environment['DISPLAY'] != browsers[1].environment['DISPLAY']
                    assert len({5900, 9222, *(b.vnc_port for b in browsers)}) == 4
                    identity = browsers[0].companion.identity
                    previous_chrome = browsers[0].chrome_process
                    await browsers[0].close()
                    assert not browsers[0].children and not browsers[0].running
                    assert previous_chrome.returncode is not None, 'Previous Chrome did not finish closing'
                    await browsers[0].navigate(base + '/0/restored', default_setup())
                    assert (await expect('/0/restored'))['retained'] == 'yes'
                    assert browsers[0].companion.identity == identity
                    assert (await browsers[0].companion.call('ready'))['version'] == '1.0.4', 'Retained profile did not run the updated companion'
                    assert browsers[0].chrome_process.pid != previous_chrome.pid
                    assert (await browsers[1].companion.call('ready'))['version'] == '1.0.4', 'Updating one profile interrupted the other browser'
                    await browsers[0].navigate(base + '/watch', default_setup())
                    assert not (await expect('/watch', 'intent'))['error'], 'Fullscreen intent guard failed'
                    real_call = browsers[0].companion.call
                    async def fixture_call(action, **fields):
                        if action in {'fullscreen', 'fullscreen_status'}:
                            return dict(latest_video['control'])
                        return await real_call(action, **fields)
                    with patch.object(browsers[0].companion, 'call', fixture_call):
                        await browsers[0].control('fullscreen')
                        full = await expect('/watch', 'fullscreen')
                        assert full['trusted'] and full['fullscreen_element'] and full['covers_display'] and full['styles_unchanged'], 'Native fullscreen was not verified'
                        await browsers[0].native_command('xdotool', 'key', 'Escape')
                        exited = await expect('/watch', 'exit')
                        assert not exited['fullscreen_element'] and exited['styles_unchanged'], 'Manual fullscreen exit changed the page'
                        replaced = await expect('/watch', 'replacement')
                        assert replaced['visible'] and replaced['styles_unchanged'], 'Replacement player was hidden'
                        await browsers[0].control('fullscreen')
                        assert (await expect('/watch', 'fullscreen'))['fullscreen_element']
                        await browsers[0].navigate(base + '/0/after-fullscreen', default_setup())
                        assert not (await expect('/0/after-fullscreen'))['fullscreen_element'], 'Navigation did not exit video fullscreen'
                    print('PASS: updated companion in retained profile; trusted native fullscreen covers display; consent, text entry, manual exit and navigation are preserved', flush=True)
                    print('PASS: two sandboxed browsers; no automation/debugging flags; native Unicode paste, navigation, zoom, private previews and saved profile after restart', flush=True)
                finally:
                    for browser in browsers:
                        await browser.close()
    finally:
        await runner.cleanup()


asyncio.run(asyncio.wait_for(main(), 180))
