"""Exercise the real numeric probe on private synthetic X11 pixels, never a TV."""
import json
import os
from pathlib import Path
import secrets
import select
import subprocess
import tempfile
import threading
import time

from Xlib import X, display


BINARY = 'airplayvideo-frame-probe'


def main():
    os.umask(0o077)
    # Invalid inputs fail before any display/source is opened and never echo it.
    for value in (b'', b'{' + b'x' * 17000, json.dumps({
            'source': {'kind': 'fixture', 'url': 'https://invalid.example/'}}).encode()):
        rejected = subprocess.run([BINARY], input=value, capture_output=True, timeout=5)
        assert rejected.returncode and not rejected.stdout and not rejected.stderr

    with tempfile.TemporaryDirectory(prefix='frame-probe-fixture-') as temporary:
        root = Path(temporary)
        authority = root / 'Xauthority'
        cookie = secrets.token_hex(16)
        authority.touch(mode=0o600)

        def authorize(number):
            subprocess.run(['xauth', '-f', str(authority), 'source', '-'],
                           input=f'add :{number} MIT-MAGIC-COOKIE-1 {cookie}\n'.encode(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

        authorize(99)
        read_fd, write_fd = os.pipe()
        server = subprocess.Popen(['Xvfb', '-displayfd', str(write_fd), '-screen', '0',
                                   '1920x1080x24', '-nolisten', 'tcp', '-auth', str(authority)],
                                  pass_fds=(write_fd,), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        os.close(write_fd)
        connection = None
        drawing = None
        ending = threading.Event()
        try:
            assert select.select([read_fd], [], [], 10)[0], 'Private test display did not start'
            number = os.read(read_fd, 32).strip().decode('ascii')
            assert number.isdigit()
            authorize(number)
            os.environ['DISPLAY'] = ':' + number + '.0'
            os.environ['XAUTHORITY'] = str(authority)
            connection = display.Display(os.environ['DISPLAY'])
            screen = connection.screen()
            window = screen.root.create_window(0, 0, 1920, 1080, 0, screen.root_depth,
                                               X.InputOutput, X.CopyFromParent,
                                               background_pixel=screen.black_pixel)
            window.map()
            brush = window.create_gc(foreground=screen.white_pixel)
            connection.sync()

            def draw():
                started = time.monotonic()
                old_x = 0
                while not ending.is_set():
                    x = int((time.monotonic() - started) * 300) % 1520
                    brush.change(foreground=screen.black_pixel)
                    window.fill_rectangle(brush, old_x, 180, 400, 700)
                    brush.change(foreground=screen.white_pixel)
                    window.fill_rectangle(brush, x, 180, 400, 700)
                    connection.sync()
                    old_x = x
                    ending.wait(1 / 60)

            drawing = threading.Thread(target=draw, daemon=True)
            drawing.start()
            config = {'source': {'kind': 'browser', 'display': os.environ['DISPLAY']},
                      'width': 1920, 'height': 1080, 'fps': 30, 'encoder': 'libopenh264',
                      'bitrate': 8000000, 'rate_control': 'auto', 'max_bitrate': 16000000,
                      'deinterlace': False, 'audio': False, 'latency_ms': 500}
            env = {key: os.environ[key] for key in ('PATH', 'LD_LIBRARY_PATH', 'LANG',
                                                    'DISPLAY', 'XAUTHORITY') if key in os.environ}
            before = set(root.iterdir())
            measured = subprocess.run([BINARY], input=json.dumps(config).encode(),
                                      capture_output=True, env=env, timeout=45)
            assert measured.returncode == 0, 'Production stage probe did not complete'
            assert 0 < len(measured.stdout) <= 32768
            result = json.loads(measured.stdout)
            assert result['version'] == 1 and result['warmup_seconds'] == 2
            assert result['duration_seconds'] == 15 and result['requested_fps'] == 30
            for stage in ('capture', 'converted', 'encoded'):
                assert 420 <= result[stage]['frames'] <= 480, (stage, result[stage]['frames'])
                assert result[stage]['fps'] == result[stage]['frames'] / 15
            assert result['capture']['nonmonotonic_pts'] == 0
            assert result['gate']['rejected'] == 0
            assert abs(result['gate']['accepted'] - result['capture']['frames']) <= 2
            assert result['timings_ms']['upload']['count'] == 0
            assert result['timings_ms']['observation']['count'] > 400
            assert any(region['exact_updates'] > 30 for region in result['raw_picture']['regions'])
            assert set(root.iterdir()) == before, 'The numeric probe must not create media or receiver files'
            print(json.dumps({'fixture': 'private synthetic X11; no browser/profile/receiver',
                              'capture_fps': result['capture']['fps'],
                              'encoded_fps': result['encoded']['fps'],
                              'observation_max_ms': result['timings_ms']['observation']['max']}))
        finally:
            ending.set()
            if drawing:
                drawing.join(timeout=3)
            if connection:
                connection.close()
            os.close(read_fd)
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == '__main__':
    main()
