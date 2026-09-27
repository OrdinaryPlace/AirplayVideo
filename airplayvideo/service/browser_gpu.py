"""Choose the browser's mapped render device without changing host permissions."""
from pathlib import Path
import stat


def chrome_gpu_arguments(directory=Path("/dev/dri")):
    # Browser.launch grants the mapped devices' existing groups to uid 1000.
    # Check that user's effective mode bits, rather than root's os.access result.
    try:
        candidates = sorted(directory.iterdir(), key=lambda path: path.name)
    except OSError:
        return []
    for device in candidates:
        if not device.name.startswith("renderD") or not device.name[7:].isdigit():
            continue
        try:
            details = device.stat()
        except OSError:
            continue
        if not stat.S_ISCHR(details.st_mode):
            continue
        permissions = (details.st_mode >> (6 if details.st_uid == 1000 else 3)) & 7
        if permissions & 6 != 6:
            continue
        # Xvfb does not supply a hardware GL renderer. ANGLE's Vulkan path lets
        # Chrome use the mapped VA-API decoder while retaining its GPU sandbox.
        return ["--use-gl=angle", "--use-angle=vulkan",
                "--enable-features=AcceleratedVideoDecoder,Vulkan,DefaultANGLEVulkan,VulkanFromANGLE",
                "--ignore-gpu-blocklist", "--render-node-override=" + str(device)]
    return []
