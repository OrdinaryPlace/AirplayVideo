#!/usr/bin/python3
"""Chrome native-messaging transport. Never log message contents or arguments."""
import json
import os
import selectors
import socket
import struct
import sys


def main():
    identity = os.environ.get('AIRPLAYVIDEO_COMPANION_ID', '')
    if len(identity) != 32 or any(c not in 'abcdefghijklmnop' for c in identity):
        return
    if len(sys.argv) < 2 or sys.argv[1] != 'chrome-extension://' + identity + '/':
        return
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(os.environ['AIRPLAYVIDEO_COMPANION_SOCKET'])
        greeting = json.dumps({'hello': identity}).encode()
        connection.sendall(struct.pack('=I', len(greeting)) + greeting)
        with selectors.DefaultSelector() as selector:
            selector.register(sys.stdin.buffer, selectors.EVENT_READ, connection.sendall)
            selector.register(connection, selectors.EVENT_READ, lambda chunk: (sys.stdout.buffer.write(chunk), sys.stdout.buffer.flush()))
            buffers = {sys.stdin.buffer: bytearray(), connection: bytearray()}
            while True:
                for key, _ in selector.select():
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        return
                    pending = buffers[key.fileobj]
                    pending.extend(chunk)
                    while len(pending) >= 4:
                        size = struct.unpack('=I', pending[:4])[0]
                        if not 0 < size <= 65536:
                            return
                        if len(pending) < 4 + size:
                            break
                        key.data(bytes(pending[:4 + size]))
                        del pending[:4 + size]


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Chrome sees a disconnected native host; no account/page data escapes.
        sys.exit(1)
