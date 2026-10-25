import re
from pathlib import Path

from service.model import VERSION


def test_runtime_version_matches_packaged_release():
    app = Path(__file__).resolve().parents[1]
    manifest_version = re.search(r'^version: "([^"]+)"$', (app / 'config.yaml').read_text(), re.M)
    image_version = re.search(r'io\.hass\.version="([^"]+)"', (app / 'Dockerfile').read_text())
    changelog_version = re.search(r'^## (\S+)$', (app / 'CHANGELOG.md').read_text(), re.M)

    assert manifest_version and image_version and changelog_version
    assert VERSION == manifest_version[1] == image_version[1] == changelog_version[1]
