"""Run one authorized native receiver trial inside the owning app container.

Uses an already verified public sample, reads saved pairing only in the C++
engine, and exposes only that sample for the bounded lifetime. This is a manual
experimental command, not a scheduled task or a production controller default.
"""
from __future__ import annotations

import argparse
import asyncio
import ipaddress
import json
from pathlib import Path
import signal
import socket

from .native_engine import NativeEngineError, NativeEnginePlayer
from .native_origin import MediaOrigin


def report(event, **details):
    print(json.dumps({'event': event, 'details': details}), flush=True)


async def run(args):
    address = ipaddress.IPv4Address(args.receiver_address)
    if not address.is_private or address.is_unspecified or address.is_multicast:
        raise NativeEngineError('Choose one local receiver')
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as route:
        # UDP connect determines a route without sending a datagram.
        route.connect((str(address), 7000))
        bind = route.getsockname()[0]
    origin = MediaOrigin({'sample.mp4': Path(args.file)}, [str(address)],
                         receiver_clients=[str(address)], bind_address=bind,
                         lifetime=args.seconds)
    player = None
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, stopping.set)
    waiters = []
    try:
        await origin.start()
        player = NativeEnginePlayer(args.root, args.receiver_id, origin.url('sample.mp4'),
                                    seconds=args.seconds, engine=args.engine,
                                    receiver_address=str(address),
                                    on_event=lambda value: report(value['event'], **value['details']))
        # Engine independently matches this address to its saved pairing before
        # contacting the TV, so a stale ID/address cannot operate another target.
        await player.start()
        report('native_trial_started', expires_in_seconds=args.seconds)
        waiters = [asyncio.create_task(player.wait()),
                   asyncio.create_task(origin.closed.wait()),
                   asyncio.create_task(stopping.wait())]
        done, _ = await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in waiters:
            task.cancel()
        await asyncio.gather(*waiters, return_exceptions=True)
        try:
            if player is not None:
                await player.close()
        finally:
            try:
                await origin.close()
            finally:
                for signum in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(signum)
                report('native_trial_closed', counters=origin.counters(),
                       failed=player.failed if player else True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', default='/data')
    parser.add_argument('--receiver-id', required=True)
    parser.add_argument('--receiver-address', required=True)
    parser.add_argument('--file', required=True)
    parser.add_argument('--seconds', type=int, choices=range(1, 601), metavar='1..600', default=30)
    parser.add_argument('--engine', default='airplayvideo-engine')
    args = parser.parse_args()
    try:
        asyncio.run(run(args))
    except (NativeEngineError, ValueError, OSError):
        report('native_trial_failed')
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
