import copy
import json
import os
import pytest
from service.model import Store, UserError, atomic_json, default_setup, private_json, validate_setup, patch_setup, browser_url, youtube_url, local_address, validate_browser_proxy
from service.hdhomerun import parse_lineup
from service.mqtt import discovery_documents


def test_setup_survives_restart_and_preserves_independent_usage(tmp_path):
    store = Store(tmp_path)
    page = store.save_page({"name": "Example", "url": "https://example.com/"})
    settings = default_setup()
    settings["video"].update(fps=60, encoder="libopenh264", bitrate_mbps=12)
    store.setup(settings)
    reopened = Store(tmp_path)
    assert reopened.data["setup"]["video"]["fps"] == 60
    assert reopened.page(page["id"]) == page
    assert reopened.data["installation_id"] == store.data["installation_id"]
    assert (tmp_path / "settings.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('value,expected', [('', ''), ('  ', ''),
    ('HTTP://016C10D5-SURFSHARK-GATEWAY:8888/', 'http://016c10d5-surfshark-gateway:8888'),
    ('http://192.168.250.10:8888', 'http://192.168.250.10:8888'),
    ('http://127.0.0.1:1', 'http://127.0.0.1:1'),
    ('http://[::1]:65535', 'http://[::1]:65535')])
def test_browser_proxy_normalization(value, expected):
    assert validate_browser_proxy(value) == expected


@pytest.mark.parametrize('value', [None, 8888, 'socks5://gateway:8888',
    'https://gateway:8888', 'http://gateway', 'http://:8888',
    'http://gateway:0', 'http://gateway:65536', 'http://gateway:invalid',
    'http://name:private-password@gateway:8888', 'http://gateway:8888/path',
    'http://gateway:8888?token=private-token', 'http://gateway:8888#private-fragment',
    'http://gateway:8888?', 'http://gateway:8888#', 'http://gateway:8888\n',
    'http://bad host:8888', 'http://8.8.8.8:8888', 'http://[::]:8888'])
def test_browser_proxy_invalid_edits_preserve_private_state(tmp_path, value):
    store = Store(tmp_path)
    before = store.path.read_bytes()
    settings = default_setup()
    settings['browser']['http_proxy'] = value
    with pytest.raises(UserError) as error:
        store.setup(settings)
    assert str(error.value) == 'Use an HTTP browser proxy with a host and port, without credentials or a path'
    assert store.path.read_bytes() == before
    assert store.data['setup']['browser']['http_proxy'] == ''


@pytest.mark.parametrize('complete', [False, True])
def test_browser_proxy_upgrade_preserves_existing_preferences_and_identity(tmp_path, complete):
    store = Store(tmp_path)
    store.setup(default_setup())
    store.save_page({'name': 'Keep me', 'url': 'https://example.com/'})
    store.data['setup']['complete'] = complete
    del store.data['setup']['browser']['http_proxy']
    store.save()
    before = copy.deepcopy(store.data)
    reopened = Store(tmp_path)
    assert reopened.data['setup']['browser']['http_proxy'] == ''
    assert Store(tmp_path).data == reopened.data
    del reopened.data['setup']['browser']['http_proxy']
    assert reopened.data == before


def test_browser_proxy_persists_and_old_setup_payload_remains_compatible(tmp_path):
    store = Store(tmp_path)
    settings = default_setup()
    del settings['browser']['http_proxy']
    store.setup(settings)
    assert store.data['setup']['browser']['http_proxy'] == ''
    settings['browser']['http_proxy'] = 'HTTP://016C10D5-SURFSHARK-GATEWAY:8888/'
    store.setup(settings)
    assert Store(tmp_path).data['setup']['browser']['http_proxy'] == 'http://016c10d5-surfshark-gateway:8888'


@pytest.mark.parametrize('complete', [False, True])
def test_saved_invalid_proxy_is_preserved_without_exposing_credentials(tmp_path, complete):
    store = Store(tmp_path)
    store.data['setup']['complete'] = complete
    store.data['setup']['browser']['http_proxy'] = 'http://fixture-user:private-password@gateway:8888'
    store.save()
    before = store.path.read_bytes()
    with pytest.raises(UserError) as error:
        Store(tmp_path)
    assert 'private-password' not in str(error.value)
    assert 'fixture-user' not in str(error.value)
    assert store.path.read_bytes() == before


def browser_proxy_application(tmp_path):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from service.main import Application
    store = Store(tmp_path)
    store.setup(default_setup())
    store.save_page({'name': 'Keep me', 'url': 'https://example.com/'})
    application = Application.__new__(Application)
    application.store = store
    application.configuration_lock = asyncio.Lock()
    application.controller = SimpleNamespace(lock=asyncio.Lock(), stream=None, pending=None,
        tearing_down=False, require_no_diagnostic=Mock(), video_encoder=Mock())
    application.recordings = SimpleNamespace(current=None)
    application.browser = SimpleNamespace(close=AsyncMock())
    application.channels = SimpleNamespace(rows=[], refresh=AsyncMock())
    application.ha = SimpleNamespace(close=AsyncMock(), start=AsyncMock(), refresh=Mock())
    application.state = store.public
    return application


@pytest.mark.asyncio
async def test_browser_proxy_edit_closes_browser_only_when_connection_changes(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    application = browser_proxy_application(tmp_path)
    before = copy.deepcopy(application.store.data)
    async def edit(value, expected):
        request = SimpleNamespace(match_info={'action': 'update'}, json=AsyncMock(return_value={
            'changes': {'browser': {'http_proxy': value}},
            'expected': {'browser': {'http_proxy': expected}}}))
        return await application.setup(request)
    proxy = 'http://016c10d5-surfshark-gateway:8888'
    await edit(proxy, '')
    application.browser.close.assert_awaited_once()
    assert Store(tmp_path).data['setup']['browser']['http_proxy'] == proxy
    assert application.store.data['pages'] == before['pages']
    assert application.store.data['installation_id'] == before['installation_id']
    application.browser.close.reset_mock()
    await edit(proxy, '')  # An identical retry leaves the prepared browser alone.
    application.browser.close.assert_not_awaited()
    persisted = application.store.path.read_bytes()
    with pytest.raises(UserError, match='another window'):
        await edit('', '')
    with pytest.raises(UserError, match='HTTP browser proxy'):
        await edit('http://user:private-password@gateway:8888', proxy)
    assert application.store.path.read_bytes() == persisted
    application.browser.close.assert_not_awaited()
    await edit('', proxy)
    application.browser.close.assert_awaited_once()
    application.channels.refresh.assert_not_awaited()
    application.ha.close.assert_not_awaited()
    application.ha.start.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['stream', 'pending', 'tearing_down', 'recording', 'diagnostic'])
async def test_browser_proxy_edit_respects_all_idle_guards(tmp_path, busy):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    application = browser_proxy_application(tmp_path)
    if busy == 'recording':
        application.recordings.current = object()
    elif busy == 'diagnostic':
        application.controller.require_no_diagnostic.side_effect = UserError('Wait for the diagnostic')
    else:
        setattr(application.controller, busy, object())
    before = application.store.path.read_bytes()
    request = SimpleNamespace(match_info={'action': 'update'}, json=AsyncMock(return_value={
        'changes': {'browser': {'http_proxy': 'http://016c10d5-surfshark-gateway:8888'}},
        'expected': {'browser': {'http_proxy': ''}}}))
    with pytest.raises(UserError):
        await application.setup(request)
    assert application.store.path.read_bytes() == before
    application.browser.close.assert_not_awaited()


@pytest.mark.parametrize("section,key,value", [("video","fps",25),("video","fps",True),("video","codec","hevc"),("video","bitrate_mbps",30),("video","encoder","command injection"),("audio","latency_ms",0),("browser","youtube_quality","4k")])
def test_invalid_setup_is_atomic(tmp_path, section, key, value):
    store = Store(tmp_path)
    before = (tmp_path / "settings.json").read_bytes()
    data = default_setup()
    data[section][key] = value
    with pytest.raises(UserError):
        store.setup(data)
    assert (tmp_path / "settings.json").read_bytes() == before
    assert not store.data["setup"]["complete"]


def test_hdhr_requires_a_configured_device():
    settings = default_setup()
    settings["modes"]["hdhomerun"] = True
    with pytest.raises(UserError):
        validate_setup(settings)
    settings["hdhomerun"]["devices"] = [{"id":"ABCDEF12","name":"Fixture tuner","address":"192.168.250.10"}]
    assert validate_setup(settings)["complete"]


def test_bitrate_upgrade_preserves_existing_state(tmp_path):
    store = Store(tmp_path)
    settings = default_setup()
    settings['video'].update(bitrate_mbps=20, fps=60)
    store.setup(settings)
    store.save_page({'name': 'Keep me', 'url': 'https://example.com/'})
    for key in ('rate_control', 'max_bitrate_mbps'):
        del store.data['setup']['video'][key]
    store.save()
    before = copy.deepcopy(store.data)
    reopened = Store(tmp_path)
    assert reopened.data['setup']['video']['rate_control'] == 'auto'
    assert reopened.data['setup']['video']['max_bitrate_mbps'] == 20
    assert Store(tmp_path).data == reopened.data
    for key in ('rate_control', 'max_bitrate_mbps'):
        del reopened.data['setup']['video'][key]
    assert reopened.data == before


@pytest.mark.parametrize('changes', [dict(max_bitrate_mbps=15), dict(max_bitrate_mbps=41),
    dict(max_bitrate_mbps=True), dict(max_bitrate_mbps=30.5), dict(rate_control='crf'),
    dict(encoder='libopenh264')])
def test_invalid_variable_bitrate_is_atomic(tmp_path, changes):
    store = Store(tmp_path)
    before = store.path.read_bytes()
    settings = default_setup()
    settings['video'].update(encoder='h264_vaapi', rate_control='vbr', bitrate_mbps=16, max_bitrate_mbps=30)
    settings['video'].update(changes)
    with pytest.raises(UserError):
        store.setup(settings)
    assert store.path.read_bytes() == before


def test_isolated_edit_preserves_other_fields_and_detects_conflicts():
    current = validate_setup(default_setup())
    current['video']['fps'] = 60  # another page/client's saved edit
    updated = patch_setup(current, {'audio': {'latency_ms': 750}}, {'audio': {'latency_ms': 1500}})
    assert updated['video']['fps'] == 60
    assert updated['audio']['latency_ms'] == 750
    assert current['audio']['latency_ms'] == 1500
    with pytest.raises(UserError, match='another window'):
        patch_setup(updated, {'audio': {'latency_ms': 1000}}, {'audio': {'latency_ms': 1500}})
    # A repeated request is safe if its desired value is already saved.
    assert patch_setup(updated, {'audio': {'latency_ms': 750}}, {'audio': {'latency_ms': 1500}}) == updated


@pytest.mark.parametrize('changes,expected', [({'complete': True},{'complete':False}),
    ({'audio': {'unknown': 1}},{'audio': {'unknown': 0}}),
    ({'audio': {'latency_ms': 1}},{'audio': {'latency_ms': 1500}}),
    ({'video': {'fps': 60}},{}), ([],[])])
def test_settings_patch_rejects_invalid_edits(changes, expected):
    with pytest.raises(UserError):
        patch_setup(validate_setup(default_setup()), changes, expected)


def test_corrupt_private_state_is_preserved(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("not valid json")
    target.chmod(0o600)
    with pytest.raises(UserError):
        Store(tmp_path)
    assert target.read_text() == "not valid json"


def test_state_symlink_and_broad_permissions_are_rejected(tmp_path):
    path = tmp_path / "private.json"
    atomic_json(path, {"original": True})
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises((UserError, OSError)):
        atomic_json(link, {"replaced": True})
    assert private_json(path) == {"original": True}
    path.chmod(0o644)
    with pytest.raises(UserError):
        private_json(path)


@pytest.mark.parametrize("url", ["file:///data/settings.json", "javascript:alert(1)", "data:text/html,x", "http://name:password@example.com", "https://example.com/\nheader"])
def test_browser_rejects_non_web_navigation(url):
    with pytest.raises(UserError):
        browser_url(url)


def test_youtube_single_video_normalization():
    assert youtube_url("https://youtu.be/aqz-KE-bpKQ?t=180&tracking=discard") == "https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=180"
    assert youtube_url("https://www.youtube.com/shorts/aqz-KE-bpKQ") == "https://www.youtube.com/watch?v=aqz-KE-bpKQ"
    with pytest.raises(UserError):
        youtube_url("https://example.com/watch?v=aqz-KE-bpKQ")


@pytest.mark.parametrize("address", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "localhost", "192.168.1.3/24", "192.168.1.3;command"])
def test_device_addresses_stay_on_lan(address):
    with pytest.raises(UserError):
        local_address(address)


def test_lineup_rejects_drm_and_foreign_stream_targets():
    device = {"id":"ABCDEF12","address":"192.168.250.10"}
    valid = {"GuideNumber":"4.1","GuideName":"Fixture","URL":"http://192.168.250.10:5004/auto/v4.1","VideoCodec":"mpeg2","AudioCodec":"ac3"}
    rows = parse_lineup(device, [valid, {**valid,"DRM":1}, {**valid,"VideoCodec":"hevc"}, {**valid,"AudioCodec":"ac4"}, {**valid,"URL":"http://169.254.169.254:5004/auto/v4.1"}])
    assert [row["supported"] for row in rows] == [True, False, False, False, False]
    assert "DeviceAuth" not in json.dumps(rows)


class Channels:
    def public(self):
        return [{"id":"ABCDEF12:4.1","label":"4.1 Fixture","supported":True}]


def test_mqtt_discovery_exposes_usage_and_keeps_stable_ids(tmp_path):
    store = Store(tmp_path)
    store.data["receivers"] = [{"id":"a"*32,"name":"Fixture TV","slot":0}]
    assert discovery_documents(store, Channels()) == {}
    store.setup(default_setup())
    page = store.save_page({"name":"Example","url":"https://example.com"})
    first = discovery_documents(store, Channels())
    assert any(doc["name"] == "Show Example" for doc in first.values())
    assert not any(word in doc["name"].lower() for doc in first.values() for word in ("codec", "frame rate", "tuner ip", "bitrate"))
    store.save_page({**page,"name":"Renamed"})
    second = discovery_documents(store, Channels())
    assert first.keys() == second.keys()
    assert {doc["unique_id"] for doc in first.values()} == {doc["unique_id"] for doc in second.values()}
    assert "https://example.com" not in json.dumps(second)
