#!/usr/bin/env python3
"""Serve a verified public sample briefly; never sends a playback command.

Run with PYTHONPATH pointing to airplayvideo/. The selected receiver and Core
host are optional consumers of prepared media, never arbitrary LAN clients.
"""
import argparse
import asyncio
import json
from pathlib import Path
import signal
import socket

from service.native_origin import MediaOrigin


async def run(args):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        route.connect((args.receiver, 7000))
        bind = route.getsockname()[0]
    origin = MediaOrigin({'sample.mp4': Path(args.file)},
                         [args.receiver, bind, *args.allow], bind_address=bind,
                         lifetime=args.seconds, receiver_clients=[args.receiver])
    await origin.start()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, lambda: asyncio.create_task(origin.close()))
    # The temporary LAN URL is deliberately provided for the explicit playback
    # action. It is not a source URL and carries no upstream cookies or keys.
    print(json.dumps({'event': 'origin_ready', 'url': origin.url('sample.mp4'),
                      'expires_in_seconds': args.seconds}), flush=True)
    try:
        await origin.closed.wait()
    finally:
        await origin.close()
        result = {'event': 'origin_closed', 'requests': origin.requests,
                  'counters': origin.counters()}
        if args.report:
            Path(args.report).write_text(json.dumps(result, indent=2) + '\n')
        print(json.dumps(result), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--file', required=True)
    parser.add_argument('--receiver', required=True)
    parser.add_argument('--allow', action='append', default=[])
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--report')
    asyncio.run(run(parser.parse_args()))
