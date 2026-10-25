"""A mapped GPU must be usable by the browser, not merely by the root service."""
from pathlib import Path
import stat
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from service.browser_gpu import chrome_gpu_arguments


def node(name, mode, uid=0, failure=None):
    path = Mock(spec=Path)
    path.name = name
    path.__str__ = Mock(return_value="/dev/dri/" + name)
    path.stat.side_effect = failure
    path.stat.return_value = SimpleNamespace(st_mode=mode, st_uid=uid)
    return path


class BrowserGPUSelection(unittest.TestCase):
    def arguments(self, *nodes, rendering="vulkan"):
        directory = Mock(spec=Path)
        directory.iterdir.return_value = iter(nodes)
        return chrome_gpu_arguments(directory, rendering=rendering)

    def test_no_mapped_device_retains_ordinary_software_browser(self):
        self.assertEqual(self.arguments(), [])
        directory = Mock(spec=Path)
        directory.iterdir.side_effect = FileNotFoundError
        self.assertEqual(chrome_gpu_arguments(directory), [])

    def test_nondevices_and_unusable_nodes_do_not_enable_hardware(self):
        for candidate in (
            node("card0", stat.S_IFCHR | 0o660),
            node("renderD128", stat.S_IFREG | 0o660),
            node("renderD128", stat.S_IFCHR | 0o600),
            node("renderD128", stat.S_IFCHR | 0o640),
            node("renderD128", stat.S_IFCHR | 0o460, uid=1000),
            node("renderD128", stat.S_IFCHR | 0o660, failure=PermissionError),
            node("renderD128", stat.S_IFCHR | 0o660, failure=FileNotFoundError),
            node("renderD128-backup", stat.S_IFCHR | 0o660),
        ):
            with self.subTest(candidate=candidate.name):
                self.assertEqual(self.arguments(candidate), [])

    def test_first_usable_render_node_is_explicit_and_sandbox_is_preserved(self):
        arguments = self.arguments(
            node("renderD129", stat.S_IFCHR | 0o660),
            node("renderD128", stat.S_IFCHR | 0o600),
        )
        self.assertIn("--render-node-override=/dev/dri/renderD129", arguments)
        self.assertIn("--use-angle=vulkan", arguments)
        self.assertFalse(any("sandbox" in value or "debugging" in value or
                             "automation" in value for value in arguments))

    def test_browser_owned_device_uses_owner_permissions(self):
        arguments = self.arguments(node("renderD128", stat.S_IFCHR | 0o600, uid=1000))
        self.assertIn("--render-node-override=/dev/dri/renderD128", arguments)

    def test_vulkan_default_retains_installed_arguments_exactly(self):
        self.assertEqual(self.arguments(node("renderD128", stat.S_IFCHR | 0o660)), [
            "--use-gl=angle", "--use-angle=vulkan",
            "--enable-features=AcceleratedVideoDecoder,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE",
            "--ignore-gpu-blocklist", "--render-node-override=/dev/dri/renderD128"])

    def test_opengl_changes_only_fixed_graphics_path_on_same_usable_device(self):
        arguments = self.arguments(node("renderD128", stat.S_IFCHR | 0o660), rendering="opengl")
        self.assertEqual(arguments, [
            "--use-gl=angle", "--use-angle=gl",
            "--enable-features=AcceleratedVideoDecoder,AcceleratedVideoDecodeLinuxGL",
            "--disable-features=Vulkan,DefaultANGLEVulkan,VulkanFromANGLE",
            "--ignore-gpu-blocklist", "--render-node-override=/dev/dri/renderD128"])
        self.assertFalse(any("sandbox" in value or "debugging" in value or
                             "automation" in value for value in arguments))
        self.assertEqual(self.arguments(node("renderD128", stat.S_IFCHR | 0o600), rendering="opengl"), [])
        self.assertEqual(self.arguments(rendering="opengl"), [])
        with self.assertRaises(ValueError):
            self.arguments(rendering="--disable-gpu-sandbox")


if __name__ == "__main__":
    unittest.main()
