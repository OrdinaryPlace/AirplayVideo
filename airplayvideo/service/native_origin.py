"""A short-lived, exact-file media origin for an explicitly selected receiver.

The UI remains behind ingress. This separate listener exposes only prepared
media to allowlisted IPs, with byte ranges for native Apple TV playback.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import inspect
import ipaddress
import mimetypes
import os
from pathlib import Path
import secrets
import socket
import stat

from aiohttp import web


class MediaOrigin:
    def __init__(self, files: dict[str, Path], allowed_clients, *, bind_address,
                 lifetime=180):
        if not files or not 1 <= lifetime <= 600:
            raise ValueError('Choose media files and a bounded lifetime')
        address = ipaddress.ip_address(bind_address)
        if address.version != 4 or address.is_unspecified or address.is_multicast:
            raise ValueError('Bind to one local IPv4 interface')
        self.bind_address = str(address)
        clients = {ipaddress.ip_address(item) for item in allowed_clients}
        if any(item.version != 4 or item.is_unspecified or item.is_multicast for item in clients):
            raise ValueError('Choose explicit IPv4 clients')
        self.allowed = {str(item) for item in clients}
        if not self.allowed:
            raise ValueError('Choose allowed clients')
        self.files = {}
        self._identities = {}
        for name, path in files.items():
            if not name or Path(name).name != name or name in {'.', '..'} or '/' in name or '\\' in name:
                raise ValueError('Use a plain media filename')
            path = Path(path)
            if path.is_symlink() or not path.is_file():
                raise ValueError('Media must be an existing regular file')
            self.files[name] = path.resolve()
            info = self.files[name].stat()
            self._identities[name] = (info.st_dev, info.st_ino)
        self.lifetime = lifetime
        self.token = secrets.token_urlsafe(24)
        self.port = None
        self.runner = None
        self._timer = None
        self._close_task = None
        self.closed = asyncio.Event()
        self.requests = []

    def url(self, name):
        if self.port is None or self._close_task is not None or name not in self.files:
            raise ValueError('Start the origin with the requested media first')
        return f'http://{self.bind_address}:{self.port}/{self.token}/{name}'

    async def _serve(self, request):
        if (self._close_task is not None or request.remote not in self.allowed
                or request.match_info['token'] != self.token):
            raise web.HTTPNotFound()
        path = self.files.get(request.match_info['name'])
        if path is None or path.is_symlink():
            raise web.HTTPNotFound()
        # Do not put URL, query, headers, IPs or pairing information into
        # diagnostics. Never use FileResponse here: it can automatically choose
        # adjacent .gz/.br files which are outside this exact-file allowlist.
        self.requests.append({'method': request.method, 'file': request.match_info['name'],
                              'range': bool(request.headers.get('Range'))})
        self.requests[:] = self.requests[-256:]
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except OSError:
            raise web.HTTPNotFound() from None
        with os.fdopen(descriptor, 'rb') as media:
            info = os.fstat(media.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or (info.st_dev, info.st_ino) != self._identities[request.match_info['name']]):
                raise web.HTTPNotFound()
            size = info.st_size
            start, end, status = 0, size, 200
            headers = {'Cache-Control': 'private, no-store', 'Accept-Ranges': 'bytes'}
            # There are no advertised validators. An If-Range request therefore
            # gets the full representation rather than assuming its validator
            # matches this prepared file. A single ordinary byte range supports
            # initial probing, seeking and resumed reads without whole-file RAM.
            if 'Range' in request.headers and 'If-Range' not in request.headers:
                try:
                    selected = request.http_range
                    start, end = selected.start, selected.stop
                    if start is not None and start < 0:
                        start = max(size + start, 0)
                    if (start is None or start >= size or size == 0
                            or request.headers['Range'] == 'bytes=-0'):
                        raise ValueError('Unsatisfiable range')
                    end = min(end if end is not None else size, size)
                except ValueError:
                    raise web.HTTPRequestRangeNotSatisfiable(
                        headers={**headers, 'Content-Range': f'bytes */{size}'}) from None
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end - 1}/{size}'
            response = web.StreamResponse(status=status, headers=headers)
            response.content_type = mimetypes.guess_type(request.match_info['name'])[0] or 'application/octet-stream'
            response.content_length = end - start
            await response.prepare(request)
            if request.method != 'HEAD':
                media.seek(start)
                remaining = end - start
                while remaining:
                    chunk = await asyncio.to_thread(media.read, min(65536, remaining))
                    if not chunk:
                        raise ConnectionResetError('Prepared media changed during streaming')
                    await response.write(chunk)
                    remaining -= len(chunk)
            await response.write_eof()
            return response

    async def start(self):
        if self.runner is not None or self._close_task is not None or self.closed.is_set():
            raise ValueError('Origin already started or closed')
        app = web.Application(client_max_size=1024)
        app.router.add_get('/{token}/{name}', self._serve)
        # Debian's aiohttp 3.8 owns this option on the site. Passing it to that
        # release's AppRunner silently forwards it to RequestHandler, breaking
        # every accepted connection. Newer releases own it on BaseRunner.
        runner_options, site_options = {}, {}
        if 'shutdown_timeout' in inspect.signature(web.BaseRunner).parameters:
            runner_options['shutdown_timeout'] = 2
        else:
            site_options['shutdown_timeout'] = 2
        self.runner = web.AppRunner(app, access_log=None, **runner_options)
        try:
            await self.runner.setup()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.bind((self.bind_address, 0))
                listener.listen(16)
                listener.setblocking(False)
                self.port = listener.getsockname()[1]
                await web.SockSite(self.runner, listener, **site_options).start()
            except BaseException:
                listener.close()
                raise
            self._timer = asyncio.create_task(self._expire())
        except BaseException:
            await self.close()
            raise
        return self

    async def _expire(self):
        await asyncio.sleep(self.lifetime)
        await self.close()

    async def close(self):
        # All callers wait for the same cleanup, including callers arriving
        # while expiration is stopping the listener. Cancelling one waiter must
        # not strand a public socket or make closed signal completion early.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._close_task)

    async def _cleanup(self):
        try:
            timer, self._timer = self._timer, None
            if timer is not None:
                timer.cancel()
                with suppress(asyncio.CancelledError):
                    await timer
            if self.runner is not None:
                await self.runner.cleanup()
        finally:
            self.runner = None
            self.port = None
            self.closed.set()
