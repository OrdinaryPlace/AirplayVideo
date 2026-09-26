"""Opt-in, bounded measurement of generated content in the installed container."""
import asyncio
import contextlib
import copy
import json
from pathlib import Path
import shutil
import socket
import tempfile
import time
import uuid
from aiohttp import web
from .browser import Browser, child_environment
from .controller import Stream
from .model import VERSION, UserError, atomic_json, check, local_address, private_json
from .native_engine import NativeEnginePlayer
from .native_origin import MediaOrigin


PAGE = '''<!doctype html><title>AirplayVideo sync reference</title>
<style>html,body{margin:0;width:100%;height:100%;overflow:hidden;background:black}
video{width:100%;height:100%;object-fit:fill}</style>
<video autoplay loop playsinline src="reference.mp4"></video>
<script>document.querySelector('video').addEventListener('playing',()=>fetch('ready',{method:'POST'}),{once:true});</script>'''

NATIVE_SECONDS = 30
NATIVE_STAGES = {'connect', 'probe', 'verify', 'setup', 'events', 'record', 'peers',
                 'media', 'control', 'insert', 'property', 'rate', 'feedback',
                 'teardown', 'finished'}


def native_bind_address(receiver_address):
    # Route selection only: UDP connect does not send a packet to the receiver.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((receiver_address, 7000))
        return local_address(route.getsockname()[0])


class MeasurementFailed(UserError):
    def __init__(self, code, output):
        super().__init__('The measurement did not complete')
        self.code, self.results = code, reports(output)


async def command(*args, environment=None, timeout=50):
    launching = asyncio.create_task(asyncio.create_subprocess_exec(
        *map(str, args), env=environment or child_environment(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
    process = None
    try:
        try:
            process = await asyncio.shield(launching)
        except asyncio.CancelledError:
            # Adopt a child created at the spawn boundary before cancelling it.
            with contextlib.suppress(Exception):
                process = await launching
            raise
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
        check(len(output) < 256 * 1024, 'Diagnostic output exceeded its limit')
        if process.returncode != 0:
            raise MeasurementFailed(process.returncode, output)
        return output
    finally:
        if process is not None and process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()


def reports(output):
    return [json.loads(line) for line in output.splitlines() if line.startswith(b'{')]


class Diagnostics:
    def __init__(self, controller, session):
        self.controller, self.session = controller, session
        self.path = controller.store.root / 'sync-report.json'
        self.task = None
        self.targets = set()
        self.report = None
        if self.path.is_file():
            with contextlib.suppress(OSError, ValueError, UserError):
                self.report = private_json(self.path)
                if self.report.get('status') == 'running':
                    self.report['status'] = 'interrupted'

    @property
    def active(self):
        return self.task is not None and not self.task.done()

    def state(self):
        return {'active': self.active, 'report': self.report}

    def save(self):
        atomic_json(self.path, self.report)
        self.controller.notify()

    def stage(self, name):
        self.report['stage'] = name
        self.save()

    async def start(self, request):
        c = self.controller
        async with c.lock:
            kind = request.get('kind', 'sync')
            check(kind in ('sync', 'native'), 'Choose a diagnostic test')
            check(not self.active, 'A sync measurement is already running')
            check(not c.stream and not c.pending, 'Stop playback before measuring sync')
            check(not c.recordings or not c.recordings.current, 'Wait for the recording to finish')
            check(not c.browser.running, 'Close the browser before measuring sync')
            check(c.store.data['setup']['complete'], 'Finish Setup first')
            wanted = request.get('receivers', [])
            check(isinstance(wanted, list) and len(wanted) <= 8
                  and all(isinstance(item, str) for item in wanted)
                  and len(set(wanted)) == len(wanted), 'Choose saved TVs for the test')
            if kind == 'native':
                check(len(wanted) == 1, 'Choose exactly one saved TV for the direct video test')
            receivers = [copy.deepcopy(c.store.receiver(item)) for item in wanted]
            if kind == 'native':
                receivers[0]['address'] = local_address(receivers[0].get('address'))
            check(shutil.disk_usage('/tmp').free > 256 * 1024 * 1024, 'Not enough space for the temporary test')
            config = None
            if kind == 'sync':
                config = c.source_config({'kind': 'fixture'})
                # A fixed reference permits comparisons across runs without saving settings.
                config.update(width=1920, height=1080, fps=30, audio=True)
            self.report = {'version': VERSION, 'kind': kind, 'status': 'running', 'started_at': int(time.time()),
                           'stage': 'Starting', 'width': 1920, 'height': 1080, 'fps': 30,
                           'targets': [r['name'] for r in receivers], 'stages': {},
                           'physical_output_measured': False}
            if kind == 'sync':
                self.report.update(encoder=config['encoder'], buffer_ms=config['latency_ms'],
                                   rate_control=config['rate_control'], bitrate=config['bitrate'],
                                   max_bitrate=config['max_bitrate'])
            else:
                self.report.update(trial_seconds=NATIVE_SECONDS, physical_observation='pending')
            self.targets = set(wanted)
            self.save()
            self.task = asyncio.create_task(self.run(config, receivers))

    async def run(self, config, receivers):
        try:
            async with asyncio.timeout(180):
                with tempfile.TemporaryDirectory(prefix='airplayvideo-sync-') as temporary:
                    if self.report['kind'] == 'native':
                        await self.measure_native(Path(temporary), receivers[0])
                    else:
                        await self.measure(Path(temporary), config, receivers)
            self.report['status'] = 'complete'
        except asyncio.CancelledError:
            self.report['status'] = 'cancelled'
        except MeasurementFailed as exc:
            self.report['status'] = 'failed'
            self.report['error'] = 'Measurement failed during ' + self.report['stage'] + '.'
            self.report['failed_process'] = {'exit_code': exc.code}
            if self.report['kind'] == 'sync':
                self.report['failed_process']['partial_results'] = exc.results
        except (UserError, asyncio.TimeoutError, OSError, ValueError):
            self.report['status'] = 'failed'
            self.report['error'] = 'Measurement stopped during ' + self.report['stage'] + '. Completed stages are retained.'
        except Exception:
            # No paths, page contents, credentials or raw child errors in reports.
            self.report['status'] = 'failed'
            self.report['error'] = ('The direct video trial could not finish.'
                                    if self.report['kind'] == 'native'
                                    else 'The sync measurement could not finish.')
        finally:
            self.report['finished_at'] = int(time.time())
            self.save()

    def native_event(self, event):
        """Persist only bounded transport stages, never arbitrary child output."""
        if not isinstance(event, dict):
            return
        name, details = event.get('event'), event.get('details')
        if name == 'native_failed':
            safe = {'event': name, 'details': {}}
        elif (name == 'native_stage' and isinstance(details, dict)
              and isinstance(details.get('stage'), str) and details['stage'] in NATIVE_STAGES):
            safe_details = {'stage': details['stage']}
            for key in ('status', 'elapsed_ms'):
                if type(details.get(key)) is int and 0 <= details[key] <= 86400000:
                    safe_details[key] = details[key]
            safe = {'event': name, 'details': safe_details}
        else:
            return
        events = self.report['stages'].setdefault('native_events', [])
        events.append(safe)
        events[:] = events[-256:]
        self.save()

    async def measure_native(self, root, receiver):
        self.stage('Preparing the direct video reference')
        fixture, movie = root / 'reference.mkv', root / 'reference.mp4'
        await command('airplayvideo-sync-probe', '--fixture', fixture)
        await command('ffmpeg', '-v', 'error', '-nostdin', '-i', fixture,
                      '-map', '0:v:0', '-map', '0:a:0', '-c:v', 'copy', '-c:a', 'aac',
                      '-b:a', '192k', '-movflags', '+faststart', movie)
        # Verify the generated content independently; no raw probe output is saved.
        await command('airplayvideo-sync-probe', '--reference', movie)
        self.report['stages']['native_reference'] = {
            'generated': True, 'verified': True, 'video_codec': 'h264', 'audio_codec': 'aac'}
        address = receiver['address']
        origin = MediaOrigin({'reference.mp4': movie}, {address},
                             bind_address=native_bind_address(address),
                             receiver_clients={address}, lifetime=NATIVE_SECONDS)
        player = None
        waiters = []
        ended = 'interrupted'
        try:
            # Includes origin startup and engine setup, independent of child timers.
            async with asyncio.timeout(NATIVE_SECONDS):
                await origin.start()
                player = NativeEnginePlayer(
                    self.controller.store.root, receiver['id'], origin.url('reference.mp4'),
                    seconds=NATIVE_SECONDS, receiver_address=address, on_event=self.native_event)
                self.stage('Testing direct video on the selected TV')
                await player.start()
                waiters = [asyncio.create_task(player.wait()), asyncio.create_task(origin.closed.wait())]
                done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                ended = 'time_limit' if waiters[1] in done else 'worker_finished'
                if waiters[0] in done:
                    await waiters[0]
        except asyncio.TimeoutError:
            ended = 'time_limit'
        finally:
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            # Always close both resources before the temporary directory disappears.
            async def cleanup():
                try:
                    if player is not None:
                        await player.close()
                finally:
                    await origin.close()
            cleanup_task = asyncio.create_task(cleanup())
            try:
                await asyncio.shield(cleanup_task)
            except asyncio.CancelledError:
                await cleanup_task
                raise
            finally:
                counters = origin.counters()
                safe_counters = {}
                for role in ('receiver', 'preflight'):
                    values = counters.get(role, {})
                    safe_counters[role] = {key: values.get(key, 0)
                                           for key in ('requests', 'bytes_sent')
                                           if type(values.get(key, 0)) is int
                                           and 0 <= values.get(key, 0) <= 2**63 - 1}
                self.report['stages']['native_delivery'] = {
                    'counters': safe_counters, 'ended': ended,
                    'engine_failed': bool(player and player.failed)}

    async def measure(self, root, config, receivers):
        probe = 'airplayvideo-sync-probe'
        self.stage('Measuring the encoder and AirPlay packet formats')
        self.report['stages']['encoder'] = reports(await command(probe, '--encoder', config['encoder'], timeout=65))
        self.stage('Preparing the 1080p browser reference')
        fixture, movie = root / 'reference.mkv', root / 'reference.mp4'
        await command(probe, '--fixture', fixture)
        await command('ffmpeg', '-v', 'error', '-nostdin', '-i', fixture, '-c:v', 'copy',
                      '-c:a', 'aac', '-b:a', '192k', '-movflags', '+faststart', movie)
        self.report['stages']['browser_reference'] = reports(await command(probe, '--reference', movie))
        # Only generated content is served, on loopback, for this job's lifetime.
        ready = asyncio.Event()
        async def loaded(_):
            ready.set()
            return web.Response(text='ready')
        async def page(_):
            return web.Response(text=PAGE, content_type='text/html')
        async def media(_):
            return web.FileResponse(movie)
        app = web.Application()
        app.router.add_get('/', page)
        app.router.add_get('/reference.mp4', media)
        app.router.add_post('/ready', loaded)
        runner = web.AppRunner(app)
        await runner.setup()
        browser = Browser(root, self.session)
        identity = None
        try:
            site = web.TCPSite(runner, '127.0.0.1', 0)
            await site.start()
            settings = copy.deepcopy(self.controller.store.data['setup'])
            settings['video']['resolution'] = '1080p'
            await browser.navigate('http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1]) + '/', settings)
            identity = browser.companion.identity
            await asyncio.wait_for(ready.wait(), 20)
            config['source'] = {'kind': 'browser', 'pulse': 'airplayvideo.monitor', 'display': browser.environment['DISPLAY']}
            for lead in (500, 1500):
                self.stage(f'Measuring browser capture with {lead} ms buffer')
                path = root / 'probe.json'
                atomic_json(path, {**config, 'latency_ms': lead})
                self.report['stages']['browser_' + str(lead)] = reports(await command(
                    probe, '--config', path, environment=browser.environment, timeout=30))
            if receivers:
                self.stage('Playing the 15-second flash and beep test on selected TVs')
                await self.trial(root, config, browser.environment, receivers)
        finally:
            if browser.companion:
                identity = browser.companion.identity
            await browser.close()
            await runner.cleanup()
            # This job used a new signing key/profile. Remove only its registration.
            if identity:
                for path in (Path('/usr/share/google-chrome/extensions') / (identity + '.json'),
                             Path('/opt/airplayvideo/installed') / (identity + '.crx')):
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()

    async def trial(self, root, config, environment, receivers):
        # Use existing pairings in place; temporary generated captures are separate
        # from the eight user-managed recordings and are removed with this job.
        (root / 'receivers').symlink_to(self.controller.store.root / 'receivers', target_is_directory=True)
        (root / 'captures').mkdir(mode=0o700)
        finished = asyncio.get_running_loop().create_future()
        telemetry = {r['id']: [] for r in receivers}
        async def event(_stream, name, fields):
            if name == 'streaming' and fields.get('id') in telemetry:
                allowed = ('video_frames', 'audio_packets', 'timing_replies', 'video_age_us', 'audio_age_us',
                           'video_queue_us', 'audio_queue_us', 'min_video_send_margin_us', 'min_audio_send_margin_us',
                           'video_setup_latency_ms', 'presentation_lead_ms', 'audio_timing')
                telemetry[fields['id']].append({k: fields[k] for k in allowed if k in fields})
            if name == 'recording_finished' and not finished.done():
                finished.set_result(fields)
            if name in ('receiver_error', 'source_error', 'fatal', 'command_error') and not finished.done():
                finished.set_exception(UserError('The test stream ended'))
        stream = Stream(root, config, environment, event)
        capture_id = uuid.uuid4().hex
        try:
            await stream.start()
            for receiver in receivers:
                await stream.command({'action': 'add', 'id': receiver['id'], 'slot': receiver['slot']})
            await stream.command({'action': 'record', 'id': capture_id, 'seconds': 15})
            capture = await asyncio.wait_for(finished, 30)
            check(capture['status'] == 'complete', 'The test capture did not complete')
        finally:
            await stream.close()
            if finished.done() and not finished.cancelled():
                finished.exception()
            self.report['stages']['delivery'] = [{'name': r['name'], 'samples': telemetry[r['id']]} for r in receivers]
        check(all(telemetry.values()), 'A selected TV did not report sending')
        self.report['stages']['during_delivery'] = reports(await command(
            'airplayvideo-sync-probe', '--reference', root / 'captures' / (capture_id + '.mkv')))

    async def close(self):
        if self.active:
            # Repeated Stop requests must not interrupt the first request's cleanup.
            if not self.task.cancelling():
                self.task.cancel()
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if not self.task.cancelled():
                    raise
                # The background coroutine may not have entered its try/finally yet.
                self.report.update(status='cancelled', finished_at=int(time.time()))
                self.save()
