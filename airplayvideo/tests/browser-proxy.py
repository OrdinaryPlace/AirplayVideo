"""Real Chrome proxy traffic, using only disposable local fixtures and profiles.

Build from airplayvideo/: docker build --platform linux/amd64 \
  -f tests/proxy-container.Dockerfile -t airplayvideo:proxy-test .
Run: docker run --rm --network none --cap-add SYS_ADMIN --shm-size=256m \
  airplayvideo:proxy-test
"""
import asyncio
import contextlib
import os
from pathlib import Path
import tempfile
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web

from service.browser import Browser
from service.model import default_setup


class ProxyFixture:
    """Never forward traffic: serve fixture pages or a deliberate HTTP 502."""

    def __init__(self):
        self.server = None
        self.port = 0
        self.requests = []
        self.changed = asyncio.Event()
        self.clients = set()
        self.tasks = set()

    async def start(self):
        self.server = await asyncio.start_server(self.accept, '127.0.0.1', self.port)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.clients):
            writer.close()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def accept(self, reader, writer):
        self.clients.add(writer)
        self.tasks.add(asyncio.current_task())
        try:
            async with asyncio.timeout(5):
                line = await reader.readline()
                method, target, _ = line.decode('ascii').strip().split(' ', 2)
                await reader.readuntil(b'\r\n\r\n')
                self.requests.append((method, target))
                self.changed.set()
                destination = urlsplit(target)
                allowed = method == 'GET' and destination.hostname in {'origin.invalid', '127.0.0.1'}
                status = '200 OK' if allowed and destination.path != '/proxy-502' else '502 Bad Gateway'
                body = b'<!doctype html><title>Fixture proxy refused request</title>'
                if status.startswith('200'):
                    # This callback proves a real page executed after its HTTP
                    # response traversed the proxy, without a debugger or eval.
                    body = b'''<!doctype html><title>Proxy fixture</title><script>
                      if(location.pathname !== '/report') fetch('/report?case=' +
                        encodeURIComponent(location.pathname) + '&webdriver=' +
                        navigator.webdriver, {cache:'no-store'});
                    </script>'''
                writer.write((f'HTTP/1.1 {status}\r\nContent-Type: text/html\r\n'
                    f'Content-Length: {len(body)}\r\nConnection: close\r\n'
                    'Cache-Control: no-store\r\n\r\n').encode() + body)
                await writer.drain()
        except (OSError, ValueError, UnicodeError, asyncio.IncompleteReadError, asyncio.TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            self.clients.discard(writer)
            self.tasks.discard(asyncio.current_task())

    async def expect(self, predicate):
        async with asyncio.timeout(15):
            while True:
                self.changed.clear()
                if any(predicate(method, target) for method, target in self.requests):
                    return
                await self.changed.wait()

    async def loaded(self, path):
        def matches(method, target):
            parsed = urlsplit(target)
            query = parse_qs(parsed.query)
            if method != 'GET' or parsed.path != '/report' or query.get('case') != [path]:
                return False
            assert query.get('webdriver') == ['false'], 'Chrome was launched as an automated browser'
            return True
        await self.expect(matches)


def check_browser(browser):
    argv = Path(f'/proc/{browser.chrome_process.pid}/cmdline').read_bytes().split(b'\0')
    assert not any(argument.startswith((b'--no-sandbox', b'--remote-debugging',
        b'--enable-automation', b'--headless')) for argument in argv), 'Browser sandbox/control flags changed'
    status = Path(f'/proc/{browser.chrome_process.pid}/status').read_text()
    assert next(line for line in status.splitlines() if line.startswith('Uid:')).split()[1:] == ['1000'] * 4, 'Chrome did not use the production unprivileged user'
    assert not (browser.root / 'profile/DevToolsActivePort').exists(), 'A debugger endpoint was created'


async def main():
    os.umask(0o077)
    direct_requests = []

    async def direct_origin(request):
        if request.path != '/fixture-health':
            direct_requests.append(request.path)
        return web.Response(text='direct origin fixture')

    app = web.Application()
    app.router.add_get('/{path:.*}', direct_origin)
    runner = web.AppRunner(app)
    await runner.setup()
    origin = web.TCPSite(runner, '127.0.0.1', 0)
    await origin.start()
    origin_base = f'http://127.0.0.1:{origin._server.sockets[0].getsockname()[1]}'
    proxy = ProxyFixture()
    browsers = []
    try:
        await proxy.start()
        setup = default_setup()
        setup['browser']['http_proxy'] = f'http://127.0.0.1:{proxy.port}'
        setup['browser']['http_proxy_enabled'] = True
        with tempfile.TemporaryDirectory() as directory:
            Path(directory).chmod(0o755)
            async with aiohttp.ClientSession() as session, contextlib.AsyncExitStack() as cleanup:
                async with session.get(origin_base + '/fixture-health') as response:
                    assert response.status == 200, 'Direct-origin sentinel is not reachable'
                browser = Browser(Path(directory) / 'warm', session)
                browsers.append(browser)
                cleanup.push_async_callback(browser.close)
                await browser.navigate('http://origin.invalid/through-proxy', setup)
                check_browser(browser)
                try:
                    await proxy.expect(lambda method, target:
                        method == 'GET' and target == 'http://origin.invalid/through-proxy')
                except asyncio.TimeoutError:
                    raise AssertionError('Fixture proxy did not receive the HTTP navigation') from None
                await proxy.loaded('/through-proxy')
                assert any(method == 'GET' and target == 'http://origin.invalid/through-proxy'
                    for method, target in proxy.requests), 'HTTP did not use absolute-form proxy requests'
                print('PASS: production sandboxed Chrome loaded an unresolved origin through the HTTP proxy', flush=True)

                await browser.navigate('https://secure-origin.invalid/connect', setup)
                await proxy.expect(lambda method, target: method == 'CONNECT' and target == 'secure-origin.invalid:443')
                print('PASS: HTTPS requested CONNECT at the configured proxy', flush=True)

                loopback = origin_base + '/proxy-502'
                await browser.navigate(loopback, setup)
                await proxy.expect(lambda method, target: method == 'GET' and target == loopback)
                await asyncio.sleep(2)
                assert not direct_requests, 'Chrome bypassed the proxy for a loopback origin'
                print('PASS: implicit loopback bypass disabled; HTTP 502 caused no direct-origin request', flush=True)

                await proxy.stop()
                warm_target = origin_base + '/proxy-stopped'
                await browser.navigate(warm_target, setup)
                await asyncio.sleep(3)
                assert not direct_requests, 'Chrome fell back directly after the proxy stopped'
                await proxy.start()
                await browser.control('reload')
                await proxy.loaded('/proxy-stopped')
                assert not direct_requests, 'Warm reload bypassed the proxy'
                print('PASS: stopped proxy caused no direct fallback; restart and reload used the proxy', flush=True)
                await browser.close()

                await proxy.stop()
                cold = Browser(Path(directory) / 'cold', session)
                browsers.append(cold)
                cleanup.push_async_callback(cold.close)
                await cold.navigate(origin_base + '/cold-unavailable', setup)
                check_browser(cold)
                await asyncio.sleep(3)
                assert not direct_requests, 'Cold Chrome bypassed its unavailable proxy'
                await proxy.start()
                await cold.control('reload')
                await proxy.loaded('/cold-unavailable')
                assert not direct_requests, 'Cold reload bypassed the proxy'
                print('PASS: cold unavailable proxy caused no direct fallback; recovery reload used the proxy', flush=True)
                await cold.close()
    finally:
        for browser in browsers:
            await browser.close()
        await proxy.stop()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(asyncio.wait_for(main(), 240))
