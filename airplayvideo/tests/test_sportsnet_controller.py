"""Scoped Stop wins while Sportsnet auth is preparing before stream creation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from service.model import UserError
from test_capitals import A, B, Stream, context, live


@pytest.mark.asyncio
async def test_scoped_stop_cancels_auth_preparation_before_any_receiver_command(context):
    x = context
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare(_target):
        started.set()
        await release.wait()

    auth = SimpleNamespace(prepare=prepare, playback_stopped=Mock())
    x.control.sportsnet_auth = auth
    playing = asyncio.create_task(x.control.play({'mode': 'browser', 'browser_source': 'url',
        'url': live().watch_url, 'receivers': [A]}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert x.control.pending_targets == {A}
        assert x.control.pending is None and x.control.stream is None and not Stream.created
        generation = x.control.generation
        await x.control.stop(A)
        assert x.control.generation == generation + 1
        assert not x.control.pending_targets
        auth.playback_stopped.assert_called_once_with()
        release.set()
        with pytest.raises(UserError, match='cancelled'):
            await playing
        assert not Stream.created and not x.control.targets and x.control.stream is None
    finally:
        release.set()
        await asyncio.gather(playing, return_exceptions=True)
        await x.control.close()


@pytest.mark.asyncio
async def test_unrelated_receiver_stop_does_not_cancel_authorized_auth_preparation(context):
    x = context
    started, release = asyncio.Event(), asyncio.Event()

    async def prepare(_target):
        started.set()
        await release.wait()

    auth = SimpleNamespace(prepare=prepare, playback_stopped=Mock())
    x.control.sportsnet_auth = auth
    playing = asyncio.create_task(x.control.play({'mode': 'browser', 'browser_source': 'url',
        'url': live().watch_url, 'receivers': [A]}))
    try:
        await asyncio.wait_for(started.wait(), 1)
        generation = x.control.generation
        await x.control.stop(B)
        assert x.control.generation == generation and x.control.pending_targets == {A}
        auth.playback_stopped.assert_not_called()
        release.set()
        await playing
        assert x.control.targets == {A}
        assert x.control.stream.commands == [{'action': 'add', 'id': A, 'slot': 0}]
    finally:
        release.set()
        await asyncio.gather(playing, return_exceptions=True)
        await x.control.close()
