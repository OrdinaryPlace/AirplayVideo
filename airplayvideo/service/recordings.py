"""Bounded, explicit captures from the same pipeline as AirPlay playback."""
import asyncio
import contextlib
import json
from pathlib import Path
import shutil
import uuid
from .browser import child_environment
from .controller import Stream
from .model import UserError, check, identifier, atomic_json


class Recordings:
    def __init__(self, controller):
        self.controller = controller
        self.root = controller.store.root / 'captures'
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.current = None
        self.stream = None
        self.owned = False
        self.timer = None
        self.tasks = set()
        self.receiver_timing = {}

    def state(self):
        rows = []
        for path in sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:8]:
            try:
                data = json.loads(path.read_text())
                identifier(path.stem)
                if data.get('id') == path.stem:
                    rows.append(data)
            except (OSError, ValueError, UserError):
                continue
        return {'active': self.current, 'items': rows}

    async def start(self, request):
        c = self.controller
        async with c.lock:
            check(self.current is None, 'A recording is already running')
            check(c.pending is None, 'Wait for playback to finish starting')
            check(len(list(self.root.glob('*.json'))) < 8, 'Download and remove an older recording first (eight retained samples maximum)')
            check(shutil.disk_usage(self.root).free > 256 * 1024 * 1024, 'Not enough free space for a recording')
            seconds = request.get('seconds', 15)
            check(type(seconds) is int and 5 <= seconds <= 30, 'Record 5 to 30 seconds')
            self.current = {'id': uuid.uuid4().hex, 'status': 'recording', 'seconds_requested': seconds}
            self.receiver_timing = {}
            self.stream, self.owned = c.stream, not bool(c.stream)
            try:
                if self.owned:
                    mode = request.get('mode', 'browser')
                    c.require_mode(mode)
                    check(mode in {'browser', 'hdhomerun'}, 'Generated videos are silent; choose browser or live TV')
                    if mode == 'browser':
                        check(c.browser.running, 'Open the browser on the content you want to record first')
                        source = {'kind': 'browser', 'pulse': 'airplayvideo.monitor', 'display': c.browser.environment['DISPLAY']}
                        environment = dict(c.browser.environment)
                    else:
                        _, source = c.requested_source(request)
                        environment = child_environment()
                    self.stream = Stream(c.store.root, c.source_config(source), environment, self.event)
                    await self.stream.start()
                check(self.stream is not None, 'The source stopped before recording began')
                await self.stream.command({'action': 'record', 'id': self.current['id'], 'seconds': seconds})
                self.timer = asyncio.create_task(self.timeout(seconds + 12))
            except BaseException:
                await self.finish()
                raise
            c.notify()

    async def event(self, stream, name, fields):
        if stream is not self.stream or not self.current:
            return
        finished = name == 'recording_finished' and fields.get('id') == self.current['id']
        failed = name in {'process_exit', 'source_error', 'fatal', 'command_error'}
        if name == 'streaming' and fields.get('id') in self.controller.targets:
            allowed = ('video_frames','audio_packets','video_age_us','audio_age_us','video_queue_us',
                       'audio_queue_us','video_setup_latency_ms','presentation_lead_ms','audio_timing')
            self.receiver_timing[fields['id']] = {key:fields[key] for key in allowed if key in fields}
        if finished:
            report = dict(fields)
            report['receiver_timing'] = self.receiver_timing
            atomic_json(self.root / (self.current['id'] + '.json'), report)
        if finished or failed:
            # Do not await Stream.close inside its own output reader.
            task = asyncio.create_task(self.finish())
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def timeout(self, seconds):
        await asyncio.sleep(seconds)
        await self.finish()

    async def finish(self):
        stream, owned = self.stream, self.owned
        self.stream, self.current, self.owned = None, None, False
        timer, self.timer = self.timer, None
        if timer and timer is not asyncio.current_task():
            timer.cancel()
        if owned and stream:
            await stream.close()
        self.controller.notify()

    def file(self, capture_id, extension):
        capture_id = identifier(capture_id)
        check(extension in {'mkv', 'json', 'csv'}, 'Unknown recording file')
        check(not self.current or self.current['id'] != capture_id, 'Wait for recording to finish')
        path = self.root / (capture_id + '.' + extension)
        check(path.is_file() and not path.is_symlink(), 'Recording file not found')
        return path

    def remove(self, capture_id):
        capture_id = identifier(capture_id)
        check(not self.current or self.current['id'] != capture_id, 'Wait for recording to finish')
        for extension in ('mkv', 'csv', 'json'):
            path = self.root / (capture_id + '.' + extension)
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    async def close(self):
        await self.finish()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)
