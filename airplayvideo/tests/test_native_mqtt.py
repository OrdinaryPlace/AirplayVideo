"""HA commands share direct-video choices without silently replacing another TV."""
import pytest

from service.mqtt import HomeAssistant
from test_lifecycle import A, B, Channels, request, setup
from test_native_controller import native


URL = 'https://youtu.be/aqz-KE-bpKQ'


@pytest.mark.parametrize('options,limit', [
    ({}, None),
    ({'delivery': 'native', 'native_resolution': '1080p'}, '1080p'),
    ({'delivery': 'native', 'native_resolution': '720p'}, '720p'),
])
@pytest.mark.asyncio
async def test_youtube_defaults_to_native_and_passes_explicit_quality(native, options, limit):
    store, controller, Session, _ = native
    ha = HomeAssistant(store, controller, Channels(), None)
    await ha.command({'action': 'youtube', 'receiver': A, 'url': URL, **options})
    assert len(Session.created) == 1
    assert Session.created[0].config['max_resolution'] == limit
    assert controller.source['kind'] == 'youtube' and controller.targets == {A}
    assert controller.error == ''
    controller.browser.navigate.assert_not_awaited()
    await controller.close()


@pytest.mark.asyncio
async def test_explicit_browser_delivery_bypasses_native_preparation(native):
    store, controller, Session, _ = native
    ha = HomeAssistant(store, controller, Channels(), None)
    await ha.command({'action': 'youtube', 'receiver': A, 'url': URL,
                      'delivery': 'browser', 'native_resolution': '1080p'})
    assert not Session.created and controller.source['kind'] == 'browser'
    assert controller.source['browser_source'] == 'youtube' and controller.targets == {A}
    controller.browser.navigate.assert_awaited_once()
    await controller.close()


@pytest.mark.parametrize('same_receiver', [True, False])
@pytest.mark.asyncio
async def test_native_request_preserves_single_receiver_target_rule(native, same_receiver):
    store, controller, Session, _ = native
    await controller.play(request(A))
    old = controller.stream
    ha = HomeAssistant(store, controller, Channels(), None)
    await ha.command({'action': 'youtube', 'receiver': A if same_receiver else B, 'url': URL})
    if same_receiver:
        assert len(Session.created) == 1 and old.closed
        assert controller.source['kind'] == 'youtube' and controller.targets == {A}
    else:
        assert not Session.created and controller.stream is old and not old.closed
        assert controller.source['kind'] == 'hdhomerun' and controller.targets == {A}
        assert 'one TV at a time' in controller.error
    await controller.close()


@pytest.mark.parametrize('options', [
    {'delivery': 'unknown'}, {'native_resolution': '360p'},
])
@pytest.mark.asyncio
async def test_invalid_youtube_options_are_rejected_before_replacing_playback(native, options):
    store, controller, Session, _ = native
    await controller.play(request(A))
    old = controller.stream
    ha = HomeAssistant(store, controller, Channels(), None)
    await ha.command({'action': 'youtube', 'receiver': A, 'url': URL, **options})
    assert controller.error and not Session.created
    assert controller.stream is old and not old.closed and controller.targets == {A}
    await controller.close()


@pytest.mark.asyncio
async def test_watch_later_retains_browser_and_shared_targets(native):
    store, controller, Session, _ = native
    await controller.play(request(A))
    ha = HomeAssistant(store, controller, Channels(), None)
    await ha.command({'action': 'watch_later', 'receiver': B,
                      'delivery': 'native', 'native_resolution': '720p'})
    assert not Session.created and controller.source['kind'] == 'browser'
    assert controller.source['browser_source'] == 'watch_later'
    assert controller.targets == {A, B} and controller.error == ''
    controller.browser.navigate.assert_awaited_once()
    await controller.close()
