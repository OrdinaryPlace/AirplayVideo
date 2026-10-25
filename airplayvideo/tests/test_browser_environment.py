from service.browser import child_environment


def test_browser_timezone_survives_the_private_environment_boundary(monkeypatch):
    monkeypatch.setenv('TZ', 'America/New_York')
    monkeypatch.setenv('SUPERVISOR_TOKEN', 'test-only-supervisor-secret')
    monkeypatch.setenv('MQTT_PASSWORD', 'test-only-mqtt-secret')
    monkeypatch.setenv('PYTHONPATH', '/test-only/untrusted-imports')
    monkeypatch.setenv('LD_PRELOAD', '/test-only/untrusted-library')

    environment = child_environment()

    assert environment['TZ'] == 'America/New_York'
    assert not {'SUPERVISOR_TOKEN', 'MQTT_PASSWORD', 'PYTHONPATH', 'LD_PRELOAD'} & environment.keys()
