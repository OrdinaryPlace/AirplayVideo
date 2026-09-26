"""Bounded native playback worker using the engine's existing saved pairing.

The Python service never reads pairing keys. This adapter is experimental and
is not selected by the production controller until receiver playback is proven.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import ipaddress
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


class NativeEngineError(RuntimeError):
    """Fixed, safe errors: never copy child output or input URLs into logs."""


def validate_media_url(value):
    try:
        parsed = urlsplit(value)
        address = ipaddress.IPv4Address(parsed.hostname)
        if (parsed.scheme != 'http' or not parsed.port or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or address.is_unspecified or address.is_multicast
                or not (address.is_private or address.is_loopback)
                or not re.fullmatch(r'/[A-Za-z0-9_-]{16,128}/[A-Za-z0-9_.-]+', parsed.path)):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise NativeEngineError('Choose a scoped local media origin') from None
    return value


class NativeEnginePlayer:
    def __init__(self, root, receiver_id, media_url, *, seconds=120,
                 engine='airplayvideo-engine', on_event=None, close_timeout=5,
                 receiver_address=None):
        if not isinstance(receiver_id, str) or not re.fullmatch(r'[0-9a-f]{32}', receiver_id):
            raise NativeEngineError('Invalid saved receiver identifier')
        if type(seconds) is not int or not 1 <= seconds <= 600:
            raise NativeEngineError('Choose a native trial of 1 to 600 seconds')
        if not 0 < close_timeout <= 20:
            raise NativeEngineError('Invalid native worker close timeout')
        self.root, self.receiver_id = Path(root), receiver_id
        self.media_url = validate_media_url(media_url)
        self.seconds, self.engine = seconds, engine
        self.on_event, self.close_timeout = on_event, close_timeout
        self.process = self._reader = self._watchdog = None
        self._launching = None
        self._close_task = self._config = None
        self._started = False
        self.closed = asyncio.Event()
        self.events = []
        self.failed = False
        try:
            self.receiver_address = (str(ipaddress.IPv4Address(receiver_address))
                                     if receiver_address is not None else None)
        except (ValueError, TypeError):
            raise NativeEngineError('Invalid selected receiver address') from None

    def _event(self, payload):
        # Receiver-supplied bodies and arbitrary exception strings never cross
        # the process boundary. Unknown child events are deliberately ignored.
        if not isinstance(payload, dict):
            return
        event, details = payload.get('event'), payload.get('details')
        if event == 'fatal':
            self.failed = True
            safe = {'event': 'native_failed', 'details': {}}
        elif event == 'native_stage' and isinstance(details, dict):
            stage = details.get('stage')
            if stage not in {'connect', 'probe', 'verify', 'setup', 'events', 'record',
                             'peers', 'media', 'control', 'insert', 'property', 'rate',
                             'feedback', 'teardown', 'finished'}:
                return
            safe_details = {'stage': stage}
            for key in ('status', 'elapsed_ms'):
                if type(details.get(key)) is int and 0 <= details[key] <= 86400000:
                    safe_details[key] = details[key]
            safe = {'event': event, 'details': safe_details}
        else:
            return
        self.events.append(safe)
        self.events[:] = self.events[-256:]
        if self.on_event:
            self.on_event(safe)

    async def start(self):
        if self._started or self._close_task:
            raise NativeEngineError('Native worker already started or closed')
        self._started = True
        run = self.root / 'run'
        run.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, path = tempfile.mkstemp(prefix='native-', suffix='.json', dir=run)
        self._config = Path(path)
        try:
            with os.fdopen(descriptor, 'w') as config:
                request = {'receiver_id': self.receiver_id, 'media_url': self.media_url,
                           'seconds': self.seconds}
                if self.receiver_address is not None:
                    request['receiver_address'] = str(ipaddress.IPv4Address(self.receiver_address))
                json.dump(request, config)
            self._launching = asyncio.create_task(asyncio.create_subprocess_exec(
                self.engine, '--native', path, '--data', str(self.root / 'receivers'),
                env={key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LD_LIBRARY_PATH')
                     if key in os.environ},
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=8192))
            try:
                self.process = await asyncio.shield(self._launching)
            except asyncio.CancelledError:
                # A cancellation at the spawn boundary must still adopt and
                # reap a child that the OS may already have created.
                with suppress(Exception):
                    self.process = await self._launching
                raise
            if self._close_task is not None:
                await self.close()
                raise NativeEngineError('Native worker stopped during startup')
            self._reader = asyncio.create_task(self._read())
            # Bounds the whole worker, including setup and teardown. Native
            # transport also bounds requests; kill/reap is the final boundary.
            self._watchdog = asyncio.create_task(self._deadline())
        except asyncio.CancelledError:
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise NativeEngineError('Native worker could not start') from None
        return self

    async def _read(self):
        try:
            async for line in self.process.stdout:
                try:
                    self._event(json.loads(line))
                except (ValueError, TypeError):
                    continue
            if await self.process.wait() != 0:
                self.failed = True
        except (ValueError, OSError):
            self.failed = True
        finally:
            # Run cleanup separately so it can await/reap this worker task.
            asyncio.create_task(self.close())

    async def _deadline(self):
        await asyncio.sleep(self.seconds + 30)
        self.failed = True
        await self.close()

    async def wait(self):
        await self.closed.wait()
        if self.failed:
            raise NativeEngineError('Native playback failed; inspect safe stage events')

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._close_task)

    async def _cleanup(self):
        try:
            if self._watchdog:
                self._watchdog.cancel()
                with suppress(asyncio.CancelledError):
                    await self._watchdog
            if self._launching is not None and self.process is None:
                with suppress(Exception):
                    self.process = await asyncio.shield(self._launching)
            if self.process is not None:
                if self.process.returncode is None:
                    with suppress(ProcessLookupError):
                        self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), self.close_timeout)
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError):
                        self.process.kill()
                    await self.process.wait()
            if self._reader:
                self._reader.cancel()
                with suppress(asyncio.CancelledError):
                    await self._reader
        finally:
            if self._config:
                self._config.unlink(missing_ok=True)
            self.closed.set()
