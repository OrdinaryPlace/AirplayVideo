import asyncio
import copy
import json
from pathlib import Path
import shutil
import tempfile

import pytest

from service import native_session as module
from service.native_session import NativeSession, SessionError
from service.native_stream import StreamError


TV = {'id': 'a' * 32, 'address': '192.168.250.10', 'model': 'AppleTV6,2', 'name': 'Saved TV'}
URL = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'


@pytest.fixture
def rig(tmp_path, monkeypatch):
    log, producers, origins, players = [], [], [], []
    gates = {}
    now = [100.0]

    class Producer:
        def __init__(self, **options):
            self.options = options
            self.plan = {'target': {'width': 3840, 'height': 2160, 'fps': '60000/1001',
                                    'video_codec': 'hevc', 'audio_codec': 'aac', 'hdr': 'sdr',
                                    'url': 'https://private-source/secret'},
                         'copy_video': False, 'copy_audio': True, 'headers': 'secret'}
            self.expected_duration = gates.get('duration', 100)
            self.directory = None
            self.closed = asyncio.Event()
            self.status = {'state': 'new', 'ready': False, 'finished': False,
                           'error': None, 'prepared_duration_seconds': 0}
            producers.append(self)
        async def start(self, url, model, **options):
            self.source = (url, model, options)
            log.append('prepare')
            self.directory = Path(tempfile.mkdtemp(prefix='native-stream-', dir=self.options['output_dir']))
            (self.directory / 'index.m3u8').write_text('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nsegment-00000000.m4s\n')
            (self.directory / 'init.mp4').write_bytes(b'init')
            (self.directory / 'segment-00000000.m4s').write_bytes(b'generated media')
            if gates.get('prepare'):
                await gates['prepare'].wait()
            if gates.get('prepare_error'):
                self.status['preparation_stage'] = gates.get('preparation_stage')
                self.status['source_probes'] = gates.get('source_probes', [])
                self.status['first_segment'] = gates.get('first_segment')
                raise gates['prepare_error']
            self.status.update(state='streaming', ready=True)
        async def close(self):
            log.append('producer_close')
            if self.directory and self.directory.exists():
                shutil.rmtree(self.directory)
            self.closed.set()
            self.status.update(state='closed', ready=False)

    class Origin:
        def __init__(self, directory, clients, **options):
            self.directory, self.clients, self.options = directory, clients, options
            self.closed = asyncio.Event()
            origins.append(self)
            self.start_failed = False
            self.beginning_fetched = True
        async def start(self):
            log.append('origin_start')
            if gates.get('origin'):
                await gates['origin'].wait()
        def url(self):
            return 'http://192.168.250.2:5432/random-private-token/index.m3u8'
        def counters(self):
            return {'receiver': {'requests': 2, 'bytes': 4096, 'segment_requests': 1, 'url': 'secret'},
                    'preflight': {'requests': 0, 'bytes': 0}, 'headers': 'secret'}
        async def close(self):
            assert self.directory.exists(), 'producer data was deleted before origin cleanup'
            log.append('origin_close')
            self.closed.set()

    class Player:
        def __init__(self, root, identifier, url, **options):
            self.root, self.identifier, self.url, self.options = root, identifier, url, options
            self.closed = asyncio.Event()
            self.failed = False
            players.append(self)
        async def start(self):
            log.append('player_start')
            if gates.get('connect'):
                await gates['connect'].wait()
            if gates.get('connect_error'):
                raise gates['connect_error']
            if not gates.get('no_ack'):
                self.options['on_event']({'event': 'native_stage', 'details': {
                    'stage': 'rate', 'status': 200, 'elapsed_ms': 27,
                    'headers': 'pairing-secret', 'url': self.url}})
        async def close(self):
            assert origins[0].directory.exists(), 'producer data was deleted before worker cleanup'
            log.append('player_close')
            if gates.get('close'):
                await gates['close'].wait()
            if gates.get('close_failure'):
                self.failed = True
            self.closed.set()

    monkeypatch.setattr(module, 'NativeHLSStream', Producer)
    monkeypatch.setattr(module, 'NativeHLSOrigin', Origin)
    monkeypatch.setattr(module, 'NativeEnginePlayer', Player)
    monkeypatch.setattr(module, 'bind_address', lambda _: '192.168.250.2')
    monkeypatch.setattr(module, 'monotonic', lambda: now[0])
    monkeypatch.setattr(module, '_POLL_SECONDS', .005)
    return tmp_path, log, producers, origins, players, gates, now


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.mark.asyncio
async def test_two_phase_start_preserves_best_quality_and_exact_saved_target(rig):
    root, log, producers, origins, players, _, _ = rig
    receiver = copy.deepcopy(TV)
    changed = []
    session = NativeSession(root, receiver, config={
        'fps': 30, 'resolution': '1080p', 'encoder': 'libopenh264', 'rate_control': 'vbr'},
        on_event=changed.append)
    receiver['address'] = '192.168.250.99'
    await session.prepare(URL)
    assert not players and not origins and session.status['phase'] == 'ready'
    producer = producers[0]
    assert producer.source == (URL, TV['model'], {'max_resolution': None})
    assert producer.options['managed_lifecycle'] and not producer.options['allow_gpl']
    assert producer.options['video_encoder'] is None
    assert producer.options['vaapi_device'] == '/dev/dri/renderD128'
    # Controller closes its old stream here. Only connect contacts the new TV.
    log.append('old_stream_closed')
    await session.connect()
    assert log.index('old_stream_closed') < log.index('player_start')
    assert origins[0].directory == producer.directory
    assert origins[0].clients == {TV['id']: TV['address']}
    assert origins[0].options == {'bind_address': '192.168.250.2', 'lifetime': 14400,
                                 'require_start_at_beginning': True}
    assert players[0].root == root and players[0].identifier == TV['id']
    assert players[0].options['receiver_address'] == TV['address']
    assert players[0].options['purpose'] == 'playback' and players[0].options['seconds'] == 14400
    status = session.status
    assert status['phase'] == 'playing' and status['quality'] == {
        'width': 3840, 'height': 2160, 'fps': '60000/1001', 'video_codec': 'hevc',
        'audio_codec': 'aac', 'hdr': 'sdr', 'copy_video': False, 'copy_audio': True}
    assert status['copy_video'] is False and status['copy_audio'] is True
    assert not status['physical_playback_verified'] and not status['seek_supported'] and not status['pause_supported']
    public = json.dumps([status, changed])
    for secret in ('private-source', '192.168.', 'random-private-token', 'pairing-secret', 'headers'):
        assert secret not in public
    status['quality']['width'] = 1
    status['events'].clear()
    assert session.status['quality']['width'] == 3840 and session.status['events']
    await session.close()
    assert log[-3:] == ['player_close', 'origin_close', 'producer_close']
    assert (await session.wait())['state'] == 'stopped' and not producer.directory.exists()


@pytest.mark.asyncio
async def test_explicit_resolution_limit_and_copy_decisions_are_passed_without_capture_fps(rig):
    root, _, producers, _, _, _, _ = rig
    session = NativeSession(root, TV, config={'max_resolution': '1080p'})
    await session.prepare(URL)
    assert producers[0].source[2] == {'max_resolution': '1080p'}
    await session.close()


@pytest.mark.parametrize('receiver', [[TV], [TV, TV], {}, {**TV, 'model': 'unknown'},
                                    {**TV, 'address': '8.8.8.8'}, {**TV, 'id': 'bad'}])
def test_only_one_known_saved_receiver_can_be_selected(rig, receiver):
    with pytest.raises(SessionError):
        NativeSession(rig[0], receiver)
    assert not rig[2] and not rig[3] and not rig[4]


@pytest.mark.parametrize('config', [{'session_timeout': 14401}, {'session_timeout': 30},
                                  {'startup_timeout': 181}, {'drain_seconds': 0},
                                  {'max_resolution': 'auto-private-url'}, {'session_timeout': True}])
def test_time_and_quality_configuration_bounds_are_explicit(rig, config):
    with pytest.raises(SessionError):
        NativeSession(rig[0], TV, config=config)


@pytest.mark.asyncio
async def test_stop_during_preparation_never_contacts_receiver(rig):
    root, log, producers, origins, players, gates, _ = rig
    gates['prepare'] = asyncio.Event()
    session = NativeSession(root, TV)
    preparing = asyncio.create_task(session.prepare(URL))
    await until(lambda: bool(producers) and producers[0].directory is not None)
    await session.close()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert not origins and not players and log == ['prepare', 'producer_close']
    assert session.closed.is_set() and not producers[0].directory.exists()
    assert session.status['state'] == 'stopped'


@pytest.mark.asyncio
async def test_cancelling_prepare_caller_closes_owned_producer(rig):
    root, _, producers, _, _, gates, _ = rig
    gates['prepare'] = asyncio.Event()
    session = NativeSession(root, TV)
    preparing = asyncio.create_task(session.prepare(URL))
    await until(lambda: bool(producers) and producers[0].directory is not None)
    preparing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await preparing
    assert session.closed.is_set() and producers[0].closed.is_set()


@pytest.mark.asyncio
async def test_stop_during_connect_reaps_worker_before_origin_and_producer(rig):
    root, log, producers, origins, players, gates, _ = rig
    session = NativeSession(root, TV)
    await session.prepare(URL)
    gates['connect'] = asyncio.Event()
    connecting = asyncio.create_task(session.connect())
    await until(lambda: bool(players))
    await session.close()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert log[-3:] == ['player_close', 'origin_close', 'producer_close']
    assert players[0].closed.is_set() and origins[0].closed.is_set() and producers[0].closed.is_set()


@pytest.mark.asyncio
async def test_connect_waits_for_playback_command_acceptance_and_remains_cancellable(rig):
    root, log, _, _, players, gates, _ = rig
    gates['no_ack'] = True
    session = NativeSession(root, TV)
    await session.prepare(URL)
    connecting = asyncio.create_task(session.connect())
    await until(lambda: 'player_start' in log)
    assert not connecting.done() and session.status['phase'] == 'connecting'
    players[0].options['on_event']({'event': 'native_stage', 'details': {'stage': 'feedback', 'status': 200}})
    await asyncio.sleep(.01)
    assert not connecting.done()
    await session.close()
    with pytest.raises(asyncio.CancelledError):
        await connecting
    assert session.closed.is_set() and log[-3:] == ['player_close', 'origin_close', 'producer_close']


@pytest.mark.asyncio
async def test_worker_failure_during_connection_returns_a_controlled_error(rig):
    root, log, _, _, players, gates, _ = rig
    gates['no_ack'] = True
    session = NativeSession(root, TV)
    await session.prepare(URL)
    connecting = asyncio.create_task(session.connect())
    await until(lambda: 'player_start' in log)
    players[0].failed = True
    players[0].closed.set()
    with pytest.raises(SessionError, match='connection failed'):
        await connecting
    assert session.status['failure'] == 'worker_failed'


@pytest.mark.asyncio
async def test_repeated_stop_and_cancelled_stop_caller_do_not_interrupt_cleanup(rig):
    root, log, producers, _, _, gates, _ = rig
    session = NativeSession(root, TV)
    await session.start(URL)
    gates['close'] = asyncio.Event()
    first = asyncio.create_task(session.close())
    await until(lambda: 'player_close' in log)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    second = asyncio.create_task(session.close())
    assert producers[0].directory.exists() and not session.closed.is_set()
    gates['close'].set()
    await second
    assert log.count('player_close') == log.count('origin_close') == log.count('producer_close') == 1


@pytest.mark.asyncio
async def test_verified_source_eof_waits_for_duration_and_final_drain(rig):
    root, _, producers, _, players, _, now = rig
    session = NativeSession(root, TV, config={'drain_seconds': 30})
    await session.start(URL)
    producer = producers[0]
    producer.status.update(state='finished', finished=True, prepared_duration_seconds=100)
    now[0] = 150
    await until(lambda: session.status['state'] == 'draining')
    assert session.status['source_finished'] and not session.status['completed']
    assert producer.directory.exists() and not players[0].closed.is_set()
    now[0] = 229
    await asyncio.sleep(.01)
    assert not session.closed.is_set()
    now[0] = 230
    result = await asyncio.wait_for(session.wait(), 1)
    assert result['completed'] and result['state'] == 'completed'
    assert not result['physical_playback_verified'] and result['prepared_duration_seconds'] == 100
    assert not producer.directory.exists()


@pytest.mark.asyncio
async def test_slow_source_gets_final_drain_after_actual_producer_eof(rig):
    root, _, producers, _, _, _, now = rig
    session = NativeSession(root, TV, config={'drain_seconds': 30})
    await session.start(URL)
    now[0] = 300
    producers[0].status.update(state='finished', finished=True, prepared_duration_seconds=100)
    await until(lambda: session.status['state'] == 'draining')
    now[0] = 329
    await asyncio.sleep(.01)
    assert not session.closed.is_set()
    now[0] = 330
    assert (await asyncio.wait_for(session.wait(), 1))['completed']


@pytest.mark.parametrize('worker_failed', [False, True])
@pytest.mark.asyncio
async def test_worker_completion_does_not_invent_source_eof(rig, worker_failed):
    root, _, producers, _, players, _, _ = rig
    session = NativeSession(root, TV)
    await session.start(URL)
    players[0].failed = worker_failed
    players[0].closed.set()
    result = await asyncio.wait_for(session.wait(), 1)
    assert result['state'] == 'failed' and not result['completed'] and not result['source_finished']
    assert result['failure'] == ('worker_failed' if worker_failed else 'worker_ended')
    assert not producers[0].directory.exists()


@pytest.mark.asyncio
async def test_producer_failure_is_redacted_and_retained_buffers_close_in_order(rig):
    root, log, producers, _, _, _, _ = rig
    session = NativeSession(root, TV)
    await session.start(URL)
    producers[0].status.update(state='failed', error='https://private-source/secret header=value')
    result = await asyncio.wait_for(session.wait(), 1)
    assert result['state'] == 'failed' and result['failure'] == 'producer_failed'
    assert 'private-source' not in json.dumps(result)
    assert log[-3:] == ['player_close', 'origin_close', 'producer_close']


@pytest.mark.asyncio
async def test_prepare_failure_never_falls_back_or_contacts_tv(rig):
    root, _, producers, origins, players, gates, _ = rig
    gates['prepare_error'] = StreamError('A compatible video encoder is unavailable; source quality was not reduced')
    session = NativeSession(root, TV)
    with pytest.raises(SessionError, match='unavailable; source quality was not reduced'):
        await session.start(URL)
    assert len(producers) == 1 and not origins and not players
    assert session.status['phase'] == 'failed' and session.closed.is_set()


@pytest.mark.asyncio
async def test_prepare_failure_retains_fixed_source_validation_reason_and_stage(rig):
    root, _, producers, origins, players, gates, _ = rig
    message = 'The media probe differs from the selected codec, dimensions or frame rate'
    gates.update(prepare_error=StreamError(message), preparation_stage='source_validation')
    session = NativeSession(root, TV)
    with pytest.raises(SessionError, match='selected codec'):
        await session.start(URL)
    result = session.status
    assert result['error'] == message
    assert result['failure_stage'] == result['preparation_stage'] == 'source_validation'
    assert result['stage'] == 'Direct video failed: Checking source quality and timing'
    assert producers[0].closed.is_set() and not origins and not players
    assert 'private-source' not in json.dumps(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('stage', ['https://private-source/secret', ['source_validation']])
async def test_prepare_diagnostics_never_admit_unrecognized_stage_or_raw_error(rig, stage):
    root, _, _, origins, players, gates, _ = rig
    gates.update(prepare_error=StreamError('private raw stderr https://secret'), preparation_stage=stage)
    session = NativeSession(root, TV)
    with pytest.raises(SessionError, match='Direct YouTube preparation failed'):
        await session.start(URL)
    result = session.status
    assert result['failure_stage'] is None and result['preparation_stage'] is None
    assert 'secret' not in json.dumps(result) and not origins and not players


@pytest.mark.asyncio
async def test_source_probe_and_segment_diagnostics_are_resanitized_for_public_report(rig):
    root, _, _, origins, players, gates, _ = rig
    message = 'The HEVC profile, pixel format, level or frame rate is not verified for the receiver'
    gates.update(prepare_error=StreamError(message), preparation_stage='segment_validation',
        source_probes=[{'role': 'output_metadata', 'result': 'ok', 'returncode': 0,
                        'elapsed_ms': 5, 'http_status': None, 'stderr': 'secret'},
                       {'role': 'https://secret', 'result': 'unknown', 'elapsed_ms': 0}],
        first_segment={'streams': [{'codec_type': 'video', 'codec_name': 'hevc', 'profile': 'Main',
                                    'level': 180, 'width': 3840, 'height': 2160, 'pix_fmt': 'yuv420p',
                                    'duration': '4.004', 'tags': {'secret': 'private'}}],
                       'format': {'filename': 'https://secret', 'duration': '4.004'},
                       'video_start_seconds': 0.0, 'audio_start_seconds': 0.0,
                       'source_audio_minus_video_start_seconds': 10**1000, 'source_fps': '30000/1001'})
    session = NativeSession(root, TV)
    with pytest.raises(SessionError, match='HEVC profile'):
        await session.start(URL)
    result = session.status
    assert result['error'] == message
    assert result['source_probes'] == [{'role': 'output_metadata', 'result': 'ok', 'returncode': 0,
                                      'elapsed_ms': 5, 'http_status': None}]
    assert result['first_segment']['streams'][0]['level'] == 180
    assert result['first_segment']['source_fps'] == '30000/1001'
    assert 'source_audio_minus_video_start_seconds' not in result['first_segment']
    assert 'secret' not in json.dumps(result) and 'private' not in json.dumps(result)
    assert not origins and not players


@pytest.mark.asyncio
async def test_connect_failure_closes_all_resources_without_raw_errors(rig):
    root, log, producers, _, _, gates, _ = rig
    gates['connect_error'] = RuntimeError('raw pairing headers and secret URL')
    session = NativeSession(root, TV)
    with pytest.raises(SessionError, match='selected TV'):
        await session.start(URL)
    assert log[-3:] == ['player_close', 'origin_close', 'producer_close']
    assert 'raw pairing' not in json.dumps(session.status) and not producers[0].directory.exists()


@pytest.mark.asyncio
async def test_session_deadline_stops_pending_ready_session_without_receiver_commands(rig):
    root, _, producers, origins, players, _, now = rig
    session = NativeSession(root, TV, config={'session_timeout': 300})
    await session.prepare(URL)
    now[0] = 400
    result = await asyncio.wait_for(session.wait(), 1)
    assert result['failure'] == 'time_limit' and not players and not origins
    assert not producers[0].directory.exists()


@pytest.mark.asyncio
async def test_closed_session_cannot_create_a_new_worker(rig):
    root, _, _, _, players, _, _ = rig
    session = NativeSession(root, TV)
    await session.close()
    for action in (session.prepare(URL), session.connect()):
        with pytest.raises(SessionError):
            await action
    assert not players


@pytest.mark.asyncio
async def test_worker_failure_discovered_during_final_close_overrides_completion(rig):
    root, _, producers, _, _, gates, now = rig
    session = NativeSession(root, TV)
    await session.start(URL)
    producers[0].status.update(state='finished', finished=True, prepared_duration_seconds=100)
    await until(lambda: session.status['state'] == 'draining')
    gates['close_failure'] = True
    now[0] = 230
    result = await asyncio.wait_for(session.wait(), 1)
    assert not result['completed'] and result['failure'] == 'worker_failed'


@pytest.mark.asyncio
async def test_expired_prepared_admission_fails_before_new_receiver_commands(rig):
    root, _, _, origins, players, _, now = rig
    session = NativeSession(root, TV, config={'session_timeout': 300})
    await session.prepare(URL)
    now[0] = 230
    with pytest.raises(SessionError, match='no longer fit'):
        await session.connect()
    assert not origins and not players and session.closed.is_set()


@pytest.mark.asyncio
async def test_origin_beginning_guard_failure_is_explicit_and_stops_every_resource(rig):
    root, log, _, origins, _, _, _ = rig
    session = NativeSession(root, TV)
    await session.start(URL)
    origins[0].start_failed = True
    result = await asyncio.wait_for(session.wait(), 1)
    assert result['failure'] == 'beginning_unavailable'
    assert log[-3:] == ['player_close', 'origin_close', 'producer_close']


@pytest.mark.asyncio
async def test_ui_callback_cancellation_cannot_cancel_the_session_monitor(rig):
    root, _, _, _, _, _, now = rig
    def observer(_):
        raise asyncio.CancelledError()
    session = NativeSession(root, TV, config={'session_timeout': 300}, on_event=observer)
    await session.prepare(URL)
    now[0] = 400
    assert (await asyncio.wait_for(session.wait(), 1))['failure'] == 'time_limit'


@pytest.mark.asyncio
async def test_prepared_playlist_that_advanced_cannot_contact_receiver(rig):
    root, _, producers, origins, players, _, _ = rig
    session = NativeSession(root, TV)
    await session.prepare(URL)
    directory = producers[0].directory
    (directory / 'segment-00000001.m4s').write_bytes(b'late segment')
    (directory / 'index.m3u8').write_text('#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:1\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:4.0,\nsegment-00000001.m4s\n')
    with pytest.raises(SessionError, match='beginning'):
        await session.connect()
    assert not origins and not players and session.closed.is_set()


@pytest.mark.asyncio
async def test_short_source_cannot_complete_without_receiver_fetching_beginning(rig):
    root, _, producers, origins, _, gates, now = rig
    gates['duration'] = 7
    session = NativeSession(root, TV)
    await session.start(URL)
    origins[0].beginning_fetched = False
    now[0] = 107
    producers[0].status.update(state='finished', finished=True, prepared_duration_seconds=7)
    await until(lambda: session.status['state'] == 'draining')
    now[0] = 138
    result = await asyncio.wait_for(session.wait(), 1)
    assert not result['completed'] and result['failure'] == 'beginning_not_fetched'
