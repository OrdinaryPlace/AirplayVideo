"""Real sandboxed Chrome and native controls in an isolated Linux container."""
import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import socket
import tempfile
import aiohttp
from aiohttp import web
from service.browser import Browser
from service.model import default_setup, UserError


async def main():
    os.umask(0o077)
    reports = asyncio.Queue()
    async def report(request):
        await reports.put(await request.json())
        return web.Response(text='ok')
    async def fixture(request):
        return web.Response(content_type='text/html', text='''<!doctype html><title>Control fixture</title>
        <form id="form"><input id="field" type="password" autofocus style="position:fixed;left:20vw;top:20vh;width:60vw;height:50vh"><button>Submit</button></form>
        <script>
        const prior=localStorage.getItem('retained');localStorage.setItem('retained','yes');
        const report=event=>fetch('/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:location.pathname,event,webdriver:navigator.webdriver,value:field.value,retained:prior})});
        addEventListener('pageshow',()=>{field.focus();requestAnimationFrame(()=>requestAnimationFrame(()=>report('ready')))});
        field.addEventListener('input',()=>report('input'));
        form.addEventListener('submit',e=>{e.preventDefault();report('submitted')});
        </script>''')
    app = web.Application()
    app.router.add_post('/report', report)
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
            for port in (5900, 9222):
                listener = resources.enter_context(socket.socket())
                listener.bind(('127.0.0.1', port))
                listener.listen()
            async with aiohttp.ClientSession() as session:
                browsers = [Browser(Path(directory) / name, session) for name in ('one', 'two')]
                try:
                    for number, browser in enumerate(browsers):
                        await browser.navigate(base + f'/{number}/first', default_setup())
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
                    await browsers[0].close()
                    assert not browsers[0].children and not browsers[0].running
                    await browsers[0].navigate(base + '/0/restored', default_setup())
                    assert (await expect('/0/restored'))['retained'] == 'yes'
                    assert browsers[0].companion.identity == identity
                    print('PASS: two sandboxed browsers; no automation/debugging flags; native Unicode paste, navigation, zoom, private previews and saved profile after restart', flush=True)
                finally:
                    for browser in browsers:
                        await browser.close()
    finally:
        await runner.cleanup()


asyncio.run(asyncio.wait_for(main(), 180))
