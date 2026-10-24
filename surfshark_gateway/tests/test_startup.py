"""Exercise the real image entrypoint with synthetic credentials and no network.

Build only this small gateway image first; never starts a VPN or a HA app.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
IMAGE = os.environ.get("SURFSHARK_GATEWAY_TEST_IMAGE", "surfshark-gateway:test")


def defaults():
    return {
        "vpn_type": "openvpn",
        "server_city": "Montreal",
        "openvpn_protocol": "udp",
        "openvpn_username": "synthetic-user@example.invalid",
        "openvpn_password": "synthetic-password $(not-a-command) `not-a-command`",
        "wireguard_private_key": "",
        "wireguard_address": "",
    }


class StartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("docker"):
            raise RuntimeError("Docker is required for the gateway startup boundary tests")
        result = subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError("Build surfshark-gateway:test from surfshark_gateway/ first")

    def run_gateway(self, options, prepare=None, inherited=None, expect_success=True,
                    network_probe=True):
        with tempfile.TemporaryDirectory(prefix="surfshark-gateway-test-") as temporary:
            data = Path(temporary).resolve()
            (data / "options.json").write_text(json.dumps(options), encoding="utf-8")
            if prepare:
                prepare(data)
            command = [
                "docker", "run", "--rm", "--platform", "linux/amd64",
                "--network", "none", "--read-only",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
                "--mount", f"type=bind,src={data},dst=/data",
                "--mount", f"type=bind,src={ROOT / 'tests' / 'probe.sh'},dst=/gluetun-entrypoint,readonly",
            ]
            if network_probe:
                command += ["--mount", f"type=bind,src={ROOT / 'tests' / 'probe-ip.sh'},dst=/usr/local/sbin/ip,readonly"]
            for key, value in (inherited or {}).items():
                command += ["--env", f"{key}={value}"]
            command.append(IMAGE)
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            for field in ("openvpn_username", "openvpn_password", "wireguard_private_key"):
                secret = options.get(field)
                if isinstance(secret, str) and secret:
                    self.assertFalse(secret in output, "A credential reached startup output")
            self.assertEqual(result.returncode == 0, expect_success, output)
            self.assertEqual((data / "probe-started").exists(), expect_success)
            if not expect_success:
                self.assertFalse(any((data / "credentials").glob(".options.*")))
            if expect_success:
                return json.loads(result.stdout)

    def test_openvpn_literal_credentials_private_files_and_no_network(self):
        result = self.run_gateway(defaults())
        self.assertEqual(result, {
            "protocol": "openvpn", "city": "Montreal",
            "private_files": True, "clean_environment": True,
        })

    def test_wireguard_private_key_and_ipv4_address(self):
        options = defaults() | {
            "vpn_type": "wireguard", "server_city": "Toronto",
            "openvpn_username": "", "openvpn_password": "",
            "wireguard_private_key": "A" * 43 + "=",
            "wireguard_address": "10.14.0.2/16",
        }
        self.assertEqual(self.run_gateway(options)["protocol"], "wireguard")

    def test_supervisor_tokens_plaintext_secrets_and_proxy_overrides_are_removed(self):
        self.run_gateway(defaults(), inherited={
            "SUPERVISOR_TOKEN": "synthetic-supervisor-token",
            "HASSIO_TOKEN": "synthetic-old-token",
            "OPENVPN_USER": "synthetic-inherited-user",
            "OPENVPN_PASSWORD": "synthetic-inherited-password",
            "WIREGUARD_PRIVATE_KEY": "synthetic-inherited-key",
            "HTTP_PROXY": "http://unexpected.invalid:9999",
            "HTTPS_PROXY": "http://unexpected.invalid:9999",
            "ALL_PROXY": "socks5://unexpected.invalid:9999",
            "FIREWALL_OUTBOUND_SUBNETS": "0.0.0.0/0",
            "HTTP_CONTROL_SERVER_ADDRESS": "0.0.0.0:8000",
            "PPROF_ENABLED": "on",
            "PPROF_BLOCK_PROFILE_RATE": "99",
            "PPROF_MUTEX_PROFILE_RATE": "99",
            "PPROF_HTTP_SERVER_ADDRESS": "0.0.0.0:6060",
        })

    def test_missing_credentials_never_launch_child(self):
        for field in ("openvpn_username", "openvpn_password"):
            with self.subTest(field=field):
                self.run_gateway(defaults() | {field: ""}, expect_success=False)

    def test_control_characters_and_unknown_options_never_launch_child(self):
        for changes in ({"openvpn_password": "synthetic\nsecond-line"},
                        {"server_city": "Montreal; echo unsafe"},
                        {"firewall": "off"}, {"vpn_type": "other"}):
            with self.subTest(fields=tuple(changes)):
                self.run_gateway(defaults() | changes, expect_success=False)

    def test_invalid_wireguard_address_never_launches_child(self):
        for address in ("999.1.1.1/16", "10.1.1.1/99", "10.1.1.1", "::1/128"):
            with self.subTest(address=address):
                self.run_gateway(defaults() | {
                    "vpn_type": "wireguard", "wireguard_private_key": "A" * 43 + "=",
                    "wireguard_address": address,
                }, expect_success=False)

    def test_missing_bridge_fails_before_child(self):
        self.run_gateway(defaults(), network_probe=False, expect_success=False)

    def test_multiple_json_objects_are_rejected(self):
        def prepare(data):
            with (data / "options.json").open("a", encoding="utf-8") as stream:
                json.dump(defaults(), stream)
        self.run_gateway(defaults(), prepare=prepare, expect_success=False)

    def test_private_directory_symlink_is_rejected(self):
        def prepare(data):
            (data / "outside").mkdir()
            (data / "credentials").symlink_to("outside", target_is_directory=True)
        self.run_gateway(defaults(), prepare=prepare, expect_success=False)

    def test_credential_target_symlink_is_rejected(self):
        def prepare(data):
            (data / "credentials").mkdir()
            (data / "outside").write_text("preserve", encoding="utf-8")
            (data / "credentials" / "openvpn-user").symlink_to("../outside")
        self.run_gateway(defaults(), prepare=prepare, expect_success=False)

    def test_protocol_switch_replaces_old_generated_files(self):
        def prepare(data):
            private = data / "credentials"
            private.mkdir()
            for name in ("openvpn-user", "openvpn-password", "wireguard-key", "wireguard-address"):
                (private / name).write_text("synthetic-stale-value", encoding="utf-8")
        self.run_gateway(defaults(), prepare=prepare)

    def test_real_gluetun_initialization_without_external_network(self):
        with tempfile.TemporaryDirectory(prefix="surfshark-gateway-real-test-") as temporary:
            data = Path(temporary).resolve()
            options = defaults()
            (data / "options.json").write_text(json.dumps(options), encoding="utf-8")
            result = subprocess.run([
                "docker", "run", "--rm", "--platform", "linux/amd64",
                "--network", "none", "--cap-add", "NET_ADMIN",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                "--mount", f"type=bind,src={data},dst=/data",
                "--mount", f"type=bind,src={ROOT / 'tests' / 'smoke-real-startup.sh'},"
                           "dst=/opt/surfshark-gateway/test-real-startup.sh,readonly",
                "--entrypoint", "/bin/sh", IMAGE,
                "/opt/surfshark-gateway/test-real-startup.sh",
            ], capture_output=True, text=True, timeout=30)
            output = result.stdout + result.stderr
            for field in ("openvpn_username", "openvpn_password"):
                self.assertFalse(options[field] in output, "A synthetic credential reached smoke output")
            self.assertEqual(result.returncode, 0, output)
            self.assertEqual(json.loads(result.stdout), {
                "real_initialization": True, "external_network": False,
                "panic_or_config_error": False,
            })


if __name__ == "__main__":
    unittest.main()
