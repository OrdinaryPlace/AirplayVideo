"""One receiver's direct YouTube playback and the resources it exclusively owns.

Preparation resolves public VOD and verifies generated HLS before any receiver
commands. The controller can replace its old session between prepare/connect.
Transport feedback never claims that the physical picture or sound was observed.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import copy
from fractions import Fraction
import math
from pathlib import Path
import re
import socket
from time import monotonic

from .model import UserError, identifier, local_address
from .native_engine import NativeEnginePlayer
from .native_hls_origin import NativeHLSOrigin
from .native_plan import receiver_capabilities
from .native_prepare import youtube_url, safe_probe_summary
from .native_stream import NativeHLSStream, PREPARATION_STAGES, PROBE_FAILURES, PROBE_ROLES, playlist_segments


class SessionError(UserError):
    """Only fixed, public explanations cross the controller boundary."""


_STAGES = {'connect', 'probe', 'verify', 'setup', 'events', 'record', 'peers',
           'media', 'control', 'insert', 'property', 'rate', 'feedback', 'teardown', 'finished'}
_COUNTERS = ('requests', 'completed_requests', 'bytes', 'head_requests', 'range_requests',
             'errors', 'playlist_requests', 'init_requests', 'segment_requests',
             'playlist_bytes', 'init_bytes', 'segment_bytes')
_MESSAGES = {
    'prepare_failed': 'Direct YouTube preparation failed; source quality was not reduced.',
    'connect_failed': 'The selected TV could not start direct playback.',
    'producer_failed': 'Direct video preparation stopped before the complete source was ready.',
    'worker_failed': 'The selected TV connection failed during direct playback.',
    'worker_ended': 'The TV worker ended before the source and final playback buffer finished.',
    'origin_ended': 'The direct media listener ended before playback finished.',
    'beginning_unavailable': 'The beginning of this video is no longer available in the prepared buffer; start it again.',
    'beginning_not_fetched': 'The selected TV did not fetch the beginning of the direct video.',
    'time_limit': 'Direct playback reached its session time limit.',
    'cleanup_failed': 'Direct playback stopped, but a resource did not finish closing.',
}
_KNOWN_FAILURES = {
    'A compatible video encoder is unavailable; source quality was not reduced',
    'The selected VAAPI encoder requires an explicit render device and successful encode verification',
    'The selected encoder could not encode the requested profile, dimensions and frame rate',
    'A required source decoder is unavailable; source quality was not reduced',
    'The required AAC audio encoder is unavailable',
    'FFmpeg lacks the required HTTPS input protocols',
    'FFprobe lacks the required HTTPS input protocols',
    'FFmpeg lacks HLS and fragmented MP4 output support',
    'The complete video must fit within the bounded native HLS session',
    'The native HLS session reached its disk limit',
    'Native HLS generation reached its time limit before the reserved final drain',
    'Native HLS output did not cover the complete resolved video duration',
    'The complete source and final playback buffer no longer fit within the session limit',
    'The best-quality video is HDR or has unknown color range; HDR preservation is not yet verified',
    'A media command failed; no source quality fallback was attempted',
    'A media command failed or timed out',
    'A media probe returned invalid data',
    'The media probe differs from the selected codec, dimensions or frame rate',
    'The media probe is incomplete or invalid',
    'The media probe has an invalid frame rate',
    'The initial presentation timestamp could not be verified',
    'The source audio and video start at different presentation times; offset preservation is not implemented',
    'The prepared sample changed the relative audio and video presentation timestamps',
    'Output frame rate did not preserve the probed source frame rate',
    'A selected source track does not cover the resolved video duration',
    'The selected audio and video durations do not agree',
    'The prepared media did not retain the requested audio and video duration',
    'Native HLS conversion stopped before completion',
    'Native HLS startup failed or timed out',
    'The selected source HTTP headers are invalid',
    'The media probe must contain exactly one selected track of each kind',
    'The H.264 profile, pixel format or level is not verified for the receiver',
    'The HEVC profile, pixel format, level or frame rate is not verified for the receiver',
    'The AAC profile, channel count or sample rate is not verified for the receiver',
    'The audio probe differs from the selected codec',
    'HDR output is not verified by this experiment',
    'The first media segment has no valid video duration',
    'The native HLS session cannot verify its first segment within the disk limit',
    'The generated HLS playlist is invalid',
    'The generated HLS playlist contains an unexpected media path',
    'The generated HLS durations or media sequence are invalid',
    'The generated HLS media sequence skipped a segment',
    'Native HLS conversion ended without a complete playlist',
    'Native HLS monitoring failed',
    'A media command returned excessive data',
}
_KNOWN_FAILURES.update(PROBE_FAILURES.values())
_POLL_SECONDS = 0.25


def bind_address(receiver_address):
    # Asking the routing table through UDP connect sends no receiver datagram.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((receiver_address, 7000))
        return local_address(route.getsockname()[0])


def _integer(value, low, high, description):
    if type(value) is not int or not low <= value <= high:
        raise SessionError(description)
    return value


def _duration(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if 0 < value <= 14400 and math.isfinite(value):
        return float(value)
    return None


class NativeSession:
    def __init__(self, root, receiver, *, config=None, on_event=None):
        if not isinstance(receiver, dict):
            raise SessionError('Choose exactly one saved TV for direct playback')
        try:
            self.receiver_id = identifier(receiver.get('id'))
            self.receiver_address = local_address(receiver.get('address'))
            capabilities = receiver_capabilities(receiver.get('model'))
        except Exception:
            raise SessionError('Choose one saved TV with a verified model and local IPv4 address') from None
        self.receiver_model = capabilities['model']
        if config is not None and not isinstance(config, dict):
            raise SessionError('Choose valid native playback settings')
        config = config or {}
        self.root = Path(root)
        self.output_dir = self.root / 'native-media'
        self.startup_timeout = _integer(config.get('startup_timeout', 120), 10, 180,
                                        'Choose a startup limit from 10 to 180 seconds')
        self.session_timeout = _integer(config.get('session_timeout', 14400), self.startup_timeout + 121, 14400,
                                        'Choose a session limit that includes startup and final playback buffering')
        self.drain_seconds = _integer(config.get('drain_seconds', 30), 1, 120,
                                      'Choose final playback buffering from 1 to 120 seconds')
        self.max_resolution = config.get('max_resolution')
        if self.max_resolution not in (None, '720p', '1080p', '2160p'):
            raise SessionError('Choose a supported explicit native resolution limit')
        # Capture resolution/FPS/bitrate/VBR and browser quality do not apply.
        self._producer_options = {
            'ffmpeg': config.get('ffmpeg', '/usr/local/bin/ffmpeg'),
            'ffprobe': config.get('ffprobe', '/usr/local/bin/ffprobe'),
            'startup_timeout': self.startup_timeout, 'session_timeout': self.session_timeout,
            'disk_limit': config.get('disk_limit', 512 * 1024 * 1024),
            'video_encoder': config.get('video_encoder'),
            'vaapi_device': config.get('vaapi_device', '/dev/dri/renderD128'),
            'allow_gpl': False, 'managed_lifecycle': True,
        }
        self.on_event = on_event
        self.producer = self.origin = self.player = None
        self._prepare_task = self._connect_task = self._monitor_task = self._close_task = None
        self._deadline = self._connected_at = self._source_finished_at = None
        self._source_finished = False
        self._state, self._stage, self._error, self._failure = 'new', 'Not started', None, None
        self._failure_stage = None
        self._quality = None
        self._copy_video = self._copy_audio = None
        self._expected_duration = None
        self._prepared_duration = 0.0
        self._events = []
        self._delivery = {}
        self._receiver_ready = asyncio.Event()
        self.closed = asyncio.Event()

    @property
    def status(self):
        self._read_counters()
        return copy.deepcopy({
            'kind': 'native', 'state': self._state, 'stage': self._stage,
            'phase': {'new': 'preparing', 'prepared': 'ready', 'streaming': 'playing',
                      'draining': 'playing', 'stopping': 'stopped'}.get(self._state, self._state),
            'receiver_id': self.receiver_id, 'receiver_model': self.receiver_model,
            'quality': self._quality, 'copy_video': self._copy_video, 'copy_audio': self._copy_audio,
            'expected_duration_seconds': self._expected_duration,
            'prepared_duration_seconds': self._prepared_duration,
            'source_finished': self._source_finished, 'completed': self._state == 'completed',
            'error': self._error, 'failure': self._failure, 'events': self._events,
            'preparation_stage': self._preparation_stage(), 'failure_stage': self._failure_stage,
            'source_probes': self._source_probes(),
            'first_segment': self._first_segment(),
            'delivery': self._delivery, 'physical_playback_verified': False,
            'seek_supported': False, 'pause_supported': False,
        })

    def _notify(self):
        if self.on_event:
            # A UI observer cannot break process ownership or expose its errors.
            with suppress(Exception, asyncio.CancelledError):
                self.on_event(self.status)

    def _set_state(self, state, stage):
        self._state, self._stage = state, stage
        self._notify()

    def _fail(self, code, error=None):
        self._failure = code
        message = str(error) if error is not None else None
        self._error = message if message in _KNOWN_FAILURES or message in _MESSAGES.values() else _MESSAGES[code]
        if code == 'prepare_failed':
            self._failure_stage = self._preparation_stage()
        stage = PREPARATION_STAGES.get(self._failure_stage)
        self._set_state('failed', 'Direct video failed: ' + stage if stage else 'Direct playback stopped')

    def _preparation_stage(self):
        if self.producer is None:
            return None
        stage = self.producer.status.get('preparation_stage')
        return stage if isinstance(stage, str) and stage in PREPARATION_STAGES else None

    def _source_probes(self):
        if self.producer is None:
            return []
        rows = self.producer.status.get('source_probes', [])
        if not isinstance(rows, list) or len(rows) > len(PROBE_ROLES):
            return []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            role, reason = row.get('role'), row.get('result')
            elapsed, code, http = row.get('elapsed_ms'), row.get('returncode'), row.get('http_status')
            if (not isinstance(role, str) or role not in PROBE_ROLES
                    or not isinstance(reason, str) or reason not in {*PROBE_FAILURES, 'ok'}
                    or type(elapsed) is not int or not 0 <= elapsed <= 2**31 - 1
                    or (code is not None and (type(code) is not int or not -255 <= code <= 255))
                    or (http is not None and (type(http) is not int or not 100 <= http <= 599))):
                continue
            result.append({'role': role, 'result': reason, 'elapsed_ms': elapsed,
                           'returncode': code, 'http_status': http})
        return result

    def _first_segment(self):
        if self.producer is None:
            return None
        value = self.producer.status.get('first_segment')
        if not isinstance(value, dict):
            return None
        result = safe_probe_summary(value)
        for key in ('video_start_seconds', 'audio_start_seconds', 'source_audio_minus_video_start_seconds'):
            number = value.get(key)
            if type(number) in (int, float) and -14400 <= number <= 14400 and math.isfinite(number):
                result[key] = number
        rate = value.get('source_fps')
        if isinstance(rate, str) and len(rate) <= 32 and re.fullmatch(r'\d+/[1-9]\d*', rate):
            parsed = Fraction(rate)
            if 0 < parsed <= 120 and parsed.denominator <= 1000000:
                result['source_fps'] = f'{parsed.numerator}/{parsed.denominator}'
        return result

    def _read_counters(self):
        if self.origin is None:
            return
        counts = self.origin.counters()
        result = {}
        for role in ('receiver', 'preflight'):
            values = counts.get(role, {})
            result[role] = {key: values[key] for key in _COUNTERS
                            if type(values.get(key)) is int and 0 <= values[key] <= 2**63 - 1}
        self._delivery = result

    def _read_plan(self):
        plan = self.producer.plan
        target = plan.get('target', {}) if isinstance(plan, dict) else {}
        try:
            width = _integer(target.get('width'), 1, 8192, 'Invalid prepared video width')
            height = _integer(target.get('height'), 1, 8192, 'Invalid prepared video height')
            rate = target.get('fps')
            if (not isinstance(rate, str) or len(rate) > 32
                    or not re.fullmatch(r'\d+(?:/\d+|\.\d+)?', rate)
                    or not 0 < Fraction(rate) <= 120
                    or target.get('video_codec') not in ('h264', 'hevc')
                    or target.get('audio_codec') != 'aac' or target.get('hdr') != 'sdr'
                    or type(plan.get('copy_video')) is not bool or type(plan.get('copy_audio')) is not bool):
                raise ValueError
            self._expected_duration = _duration(self.producer.expected_duration)
            if self._expected_duration is None:
                raise ValueError
        except (ValueError, TypeError, ZeroDivisionError):
            raise SessionError('Prepared media has no verified native playback plan') from None
        self._quality = {'width': width, 'height': height, 'fps': rate,
                         'video_codec': target['video_codec'], 'audio_codec': 'aac', 'hdr': 'sdr',
                         'copy_video': plan['copy_video'], 'copy_audio': plan['copy_audio']}
        producer_status = self.producer.status
        encoder = producer_status.get('video_encoder')
        if encoder in ('libopenh264', 'h264_vaapi', 'hevc_vaapi'):
            self._quality['video_encoder'] = encoder
        vaapi_mode = producer_status.get('vaapi_mode')
        if vaapi_mode in ('default', 'low_power'):
            self._quality['vaapi_mode'] = vaapi_mode
        self._copy_video, self._copy_audio = plan['copy_video'], plan['copy_audio']

    async def prepare(self, url):
        if self._state != 'new' or self._close_task is not None:
            raise SessionError('The direct playback session can only prepare once')
        try:
            url = youtube_url(url)
        except Exception:
            raise SessionError('Supply an HTTPS link to one public YouTube video') from None
        self._deadline = monotonic() + self.session_timeout
        self._set_state('preparing', 'Preparing and verifying direct video')
        self._prepare_task = asyncio.create_task(self._prepare(url))
        try:
            await asyncio.shield(self._prepare_task)
        except asyncio.CancelledError:
            await self.close()
            if self._error:
                raise SessionError(self._error) from None
            raise
        except Exception as error:
            self._fail('prepare_failed', error)
            await self.close()
            raise SessionError(self._error) from None
        if self._close_task is not None:
            await self.close()
            raise SessionError('Direct playback stopped during preparation')
        self._set_state('prepared', 'Direct video is ready')
        self._monitor_task = asyncio.create_task(self._monitor())
        return self

    async def _prepare(self, url):
        self.output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.producer = NativeHLSStream(output_dir=self.output_dir, **self._producer_options)
        await self.producer.start(url, self.receiver_model, max_resolution=self.max_resolution)
        if not self.producer.status.get('ready'):
            raise SessionError('The first direct media segment was not verified')
        self._read_plan()

    async def connect(self):
        if self._state != 'prepared' or self._connect_task is not None or self._close_task is not None:
            raise SessionError('Prepare and verify direct video before connecting the TV')
        self._set_state('connecting', 'Connecting the selected TV')
        self._connect_task = asyncio.create_task(self._connect())
        try:
            await asyncio.shield(self._connect_task)
        except asyncio.CancelledError:
            await self.close()
            if self._error:
                raise SessionError(self._error) from None
            raise
        except Exception as error:
            self._fail('connect_failed', error)
            await self.close()
            raise SessionError(self._error) from None
        if self._close_task is not None:
            await self.close()
            raise SessionError('Direct playback stopped during connection')
        return self

    async def _connect(self):
        if not self.producer.status.get('ready') or self.producer.closed.is_set():
            raise SessionError('Prepared direct media is no longer available')
        names = playlist_segments(self.producer.directory)
        if not names or names[0] != 'segment-00000000.m4s':
            raise SessionError(_MESSAGES['beginning_unavailable'])
        remaining = math.floor(self._deadline - monotonic())
        if remaining < self._expected_duration + self.drain_seconds + 45:
            raise SessionError('The complete source and final playback buffer no longer fit within the session limit')
        self.origin = NativeHLSOrigin(self.producer.directory,
                                      {self.receiver_id: self.receiver_address},
                                      bind_address=bind_address(self.receiver_address), lifetime=remaining,
                                      require_start_at_beginning=True)
        await self.origin.start()
        self.player = NativeEnginePlayer(self.root, self.receiver_id, self.origin.url(),
                                         seconds=remaining, purpose='playback',
                                         receiver_address=self.receiver_address, on_event=self._event)
        await self.player.start()
        # Process creation is not acceptance of playback. The worker reports
        # only this fixed protocol stage, without receiver response contents.
        await asyncio.wait_for(self._receiver_ready.wait(), min(45, remaining))
        self._connected_at = monotonic()

    async def start(self, url):
        await self.prepare(url)
        return await self.connect()

    def _event(self, payload):
        if not isinstance(payload, dict):
            return
        name, details = payload.get('event'), payload.get('details')
        if name == 'native_failed':
            safe = {'event': name, 'details': {}}
        elif (name == 'native_stage' and isinstance(details, dict)
              and isinstance(details.get('stage'), str) and details['stage'] in _STAGES):
            fields = {'stage': details['stage']}
            for key in ('status', 'elapsed_ms'):
                if type(details.get(key)) is int and 0 <= details[key] <= 86400000:
                    fields[key] = details[key]
            safe = {'event': name, 'details': fields}
            if fields.get('stage') == 'rate' and fields.get('status') == 200 and self._state == 'connecting':
                self._receiver_ready.set()
                self._set_state('streaming', 'Sending direct video')
        else:
            return
        self._events.append(safe)
        self._events[:] = self._events[-256:]
        self._notify()

    async def _monitor(self):
        try:
            while self._close_task is None:
                state = self.producer.status
                prepared = _duration(state.get('prepared_duration_seconds'))
                if prepared is not None:
                    self._prepared_duration = prepared
                if state.get('error') or state.get('state') == 'failed':
                    self._fail('producer_failed', state.get('error'))
                    break
                if self.producer.closed.is_set():
                    self._fail('producer_failed')
                    break
                now = monotonic()
                if state.get('finished') and state.get('state') == 'finished' and not self._source_finished:
                    self._source_finished = True
                    self._source_finished_at = now
                if self.player is not None and self.player.closed.is_set():
                    self._fail('worker_failed' if self.player.failed else 'worker_ended')
                    break
                if self.origin is not None and self.origin.closed.is_set():
                    self._fail('origin_ended')
                    break
                if self.origin is not None and self.origin.start_failed:
                    self._fail('beginning_unavailable')
                    break
                if (self._connected_at is not None and now - self._connected_at > 45
                        and not self.origin.beginning_fetched):
                    self._fail('beginning_not_fetched')
                    break
                if now >= self._deadline:
                    self._fail('time_limit')
                    break
                if self._source_finished and self._connected_at is not None:
                    if self._state != 'draining':
                        self._set_state('draining', 'Finishing the prepared video')
                    # EOF means source preparation ended, not that the TV rendered
                    # its last frame. Retain at least a source-duration timeline
                    # from receiver startup plus a final buffer after producer EOF.
                    end = max(self._connected_at + self._expected_duration,
                              self._source_finished_at) + self.drain_seconds
                    if now >= end:
                        if not self.origin.beginning_fetched:
                            self._fail('beginning_not_fetched')
                        else:
                            self._set_state('completed', 'Direct video session finished')
                        break
                self._notify()
                await asyncio.sleep(_POLL_SECONDS)
        except asyncio.CancelledError:
            return
        except Exception:
            self._fail('producer_failed')
        # Cleanup runs separately: it cancels and awaits this monitor itself.
        asyncio.create_task(self.close())

    async def wait(self):
        await self.closed.wait()
        return self.status

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._close_task)

    async def _cleanup(self):
        if self._state not in ('failed', 'completed'):
            self._set_state('stopping', 'Stopping direct playback')
        tasks = [task for task in (self._prepare_task, self._connect_task, self._monitor_task)
                 if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Each owned component has bounded process/listener cleanup. Continue
        # through all three if one fails; never remove served media first.
        try:
            for resource in (self.player, self.origin, self.producer):
                if resource is not None:
                    try:
                        await resource.close()
                        if resource is self.player and self.player.failed and self._state != 'failed':
                            self._fail('worker_failed')
                    except Exception:
                        self._fail('cleanup_failed')
            self._read_counters()
        finally:
            if self._state not in ('failed', 'completed'):
                self._set_state('stopped', 'Direct playback stopped')
            self.closed.set()
            self._notify()
