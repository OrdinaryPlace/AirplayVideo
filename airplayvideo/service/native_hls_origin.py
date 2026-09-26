"""A scoped HLS origin for one private native-stream-* working directory.

Only finalized playlist/init/segment basenames are readable. The directory is
pinned by descriptor; each file is opened relative to it without following a
symlink. Playlist replacement and rolling segment deletion remain supported.
Counters measure completed sender writes, not receiver rendering or sound.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
import hmac
import inspect
import ipaddress
import os
from pathlib import Path
import re
import secrets
import socket
import stat

from aiohttp import web


_SEGMENT = re.compile(r"segment-[0-9]{8}\.m4s\Z")
_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _address(value):
    address = ipaddress.ip_address(value)
    if address.version != 4 or address.is_unspecified or address.is_multicast:
        raise ValueError('Use an explicit IPv4 address')
    return str(address)


def _kind(name):
    if name == 'index.m3u8':
        return 'playlist'
    if name == 'init.mp4':
        return 'init'
    return 'segment' if isinstance(name, str) and _SEGMENT.fullmatch(name) else None


def _counts():
    return dict.fromkeys(('requests', 'completed_requests', 'bytes', 'head_requests',
                          'range_requests', 'errors', 'playlist_requests', 'init_requests',
                          'segment_requests', 'playlist_bytes', 'init_bytes', 'segment_bytes'), 0)


class NativeHLSOrigin:
    def __init__(self, directory, receiver_clients, *, bind_address,
                 preflight_clients=(), lifetime=14400):
        """Receiver clients may be ``{safe_receiver_id: IPv4}`` or an IP iterable.

        Preflight IPs must be separate from receiver IPs, so fetching readiness
        from this host cannot appear as a TV fetching media. No IPs, bearer URLs,
        source URLs or headers appear in the public counters.
        """
        directory = Path(directory)
        if not re.fullmatch(r'native-stream-[A-Za-z0-9_-]{1,100}', directory.name):
            raise ValueError('Use a private native stream directory')
        if (isinstance(lifetime, bool) or not isinstance(lifetime, (int, float))
                or not 1 <= lifetime <= 14400):
            raise ValueError('Choose an origin lifetime from one second to four hours')
        self.directory = directory
        self.bind_address = _address(bind_address)
        self.clients = {}
        if isinstance(receiver_clients, dict):
            receivers = receiver_clients.items()
        else:
            receivers = ((f'receiver_{number}', address) for number, address in
                         enumerate(sorted(set(receiver_clients)), 1))
        for identifier, address in receivers:
            if not isinstance(identifier, str) or not _CLIENT_ID.fullmatch(identifier):
                raise ValueError('Use a safe receiver identifier')
            address = _address(address)
            if address in self.clients:
                raise ValueError('Receiver addresses must be distinct')
            self.clients[address] = identifier
        if not self.clients:
            raise ValueError('Choose at least one receiver address')
        self.preflight_clients = {_address(address) for address in preflight_clients}
        if self.preflight_clients.intersection(self.clients):
            raise ValueError('Preflight and receiver addresses must be distinct')
        self.lifetime = lifetime
        self.token = secrets.token_urlsafe(24)
        self.port = None
        self.runner = None
        self._directory_fd = None
        self._timer = None
        self._close_task = None
        self.closed = asyncio.Event()
        self._counts = {'receiver': _counts(), 'preflight': _counts(),
                        'receivers': {identifier: _counts() for identifier in self.clients.values()},
                        'denied_requests': 0}

    def counters(self):
        return copy.deepcopy(self._counts)

    def url(self, name='index.m3u8'):
        if self.port is None or self._close_task is not None or _kind(name) is None:
            raise ValueError('Start the origin and choose a finalized HLS filename')
        return f'http://{self.bind_address}:{self.port}/{self.token}/{name}'

    async def _serve(self, request):
        identifier = self.clients.get(request.remote)
        role = 'receiver' if identifier is not None else 'preflight' if request.remote in self.preflight_clients else None
        if (role is None or self._close_task is not None
                or not hmac.compare_digest(request.match_info['token'].encode('utf-8'), self.token.encode('ascii'))):
            self._counts['denied_requests'] += 1
            raise web.HTTPNotFound()
        counters = [self._counts[role]]
        if identifier is not None:
            counters.append(self._counts['receivers'][identifier])

        def count(key, amount=1):
            for item in counters:
                item[key] += amount

        count('requests')
        if request.method == 'HEAD':
            count('head_requests')
        if 'Range' in request.headers:
            count('range_requests')
        name = request.match_info['name']
        kind = _kind(name)
        if kind is None or self._directory_fd is None:
            count('errors')
            raise web.HTTPNotFound()
        count(kind + '_requests')
        try:
            descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=self._directory_fd)
        except OSError:
            count('errors')
            raise web.HTTPNotFound() from None
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError('Unsafe media file')
            media = os.fdopen(descriptor, 'rb')
        except OSError:
            with suppress(OSError):
                os.close(descriptor)
            count('errors')
            raise web.HTTPNotFound() from None
        with media:
            size = info.st_size
            start, end, status = 0, size, 200
            headers = {'Cache-Control': 'private, no-store', 'Accept-Ranges': 'bytes'}
            # Without an advertised validator, If-Range conservatively requests
            # the full representation. Ordinary single ranges support seeking.
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
                    count('errors')
                    raise web.HTTPRequestRangeNotSatisfiable(
                        headers={**headers, 'Content-Range': f'bytes */{size}'}) from None
                status = 206
                headers['Content-Range'] = f'bytes {start}-{end - 1}/{size}'
            response = web.StreamResponse(status=status, headers=headers)
            response.content_type = {'playlist': 'application/vnd.apple.mpegurl',
                                     'init': 'video/mp4', 'segment': 'video/iso.segment'}[kind]
            response.content_length = end - start
            try:
                await response.prepare(request)
                if request.method != 'HEAD':
                    media.seek(start)
                    remaining = end - start
                    while remaining:
                        chunk = await asyncio.to_thread(media.read, min(65536, remaining))
                        if not chunk:
                            raise ConnectionResetError('Finalized HLS media changed during streaming')
                        await response.write(chunk)
                        count('bytes', len(chunk))
                        count(kind + '_bytes', len(chunk))
                        remaining -= len(chunk)
                await response.write_eof()
                count('completed_requests')
            except BaseException:
                count('errors')
                raise
            return response

    async def start(self):
        if self.runner is not None or self._close_task is not None or self.closed.is_set():
            raise ValueError('Origin already started or closed')
        app = web.Application(client_max_size=1024)
        app.router.add_get('/{token}/{name}', self._serve)
        runner_options, site_options = {}, {}
        if 'shutdown_timeout' in inspect.signature(web.BaseRunner).parameters:
            runner_options['shutdown_timeout'] = 2
        else:
            site_options['shutdown_timeout'] = 2
        self.runner = web.AppRunner(app, access_log=None, **runner_options)
        try:
            try:
                self._directory_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            except OSError:
                raise ValueError('Native media directory is unavailable or unsafe') from None
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
            if self._directory_fd is not None:
                os.close(self._directory_fd)
                self._directory_fd = None
            self.runner = None
            self.port = None
            self.closed.set()
