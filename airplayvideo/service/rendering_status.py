"""Launch evidence and bounded, private Linux process/device observations."""
from dataclasses import dataclass
from pathlib import Path
import re

MAX_PROCESSES = 128
MAX_FDS = 256
RENDER_NODE = re.compile(r"/dev/dri/(renderD[0-9]{1,6})\Z")
PID_NAMESPACE = re.compile(r"pid:\[[0-9]+\]\Z")


def launch_status(profile, rendering, arguments):
    """Project only fixed facts from the exact argument list used for launch."""
    nodes = [match.group(1) for value in arguments if isinstance(value, str)
             and value.startswith("--render-node-override=")
             and (match := RENDER_NODE.fullmatch(value.split("=", 1)[1]))]
    angle = "--use-angle=gl" if rendering == "opengl" else "--use-angle=vulkan"
    applied = len(nodes) == 1 and "--use-gl=angle" in arguments and angle in arguments
    return {"active_profile": profile if profile in {"default", "sportsnet"} else None,
            "launch_rendering": rendering if rendering in {"opengl", "vulkan"} else None,
            "gpu_flags_applied": applied, "render_node": nodes[0] if applied else None}


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    parent: int
    started: int
    namespace: str


def _small(path, maximum):
    with path.open("rb") as source:
        value = source.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError("Process metadata limit")
    return value


def _stat(root, pid):
    value = _small(root / str(pid) / "stat", 4096)
    end = value.rfind(b") ")
    if end < 0 or value.split(b" (", 1)[0] != str(pid).encode():
        raise ValueError("Invalid process metadata")
    fields = value[end + 2:].split()
    parent, started = int(fields[1]), int(fields[19])
    if parent < 0 or started < 0:
        raise ValueError("Invalid process identity")
    return parent, started


def _namespace(path):
    value = str(path.readlink())
    if not PID_NAMESPACE.fullmatch(value):
        raise ValueError("Invalid process namespace")
    return value


def _scoped(root, pid, namespace):
    status = _small(root / str(pid) / "status", 8192)
    row = next((line.split()[1:] for line in status.splitlines() if line.startswith(b"Uid:")), [])
    return (len(row) == 4 and all(value == b"1000" for value in row)
            and _namespace(root / str(pid) / "ns" / "pid") == namespace)


def capture_identity(pid, root=Path("/proc")):
    if type(pid) is not int or not 0 < pid <= 2**31 - 1:
        return None
    try:
        namespace = _namespace(root / "self" / "ns" / "pid")
        parent, started = _stat(root, pid)
        if _scoped(root, pid, namespace) and _stat(root, pid) == (parent, started):
            return ProcessIdentity(pid, parent, started, namespace)
    except (OSError, ValueError, IndexError):
        pass
    return None


def owner_current(owner, root=Path("/proc")):
    if owner is None:
        return None
    try:
        return (_namespace(root / "self" / "ns" / "pid") == owner.namespace
                and _stat(root, owner.pid) == (owner.parent, owner.started)
                and _scoped(root, owner.pid, owner.namespace))
    except FileNotFoundError:
        return False
    except (OSError, ValueError, IndexError):
        return None


def gpu_process_status(owner, root=Path("/proc")):
    """Open GPU resources are evidence of use, never proof of video decoding."""
    unknown = {"gpu_process_count": None, "gpu_render_device_open": None}
    if owner is None or owner_current(owner, root) is not True:
        return unknown
    try:
        processes = {}
        # Do not materialize/sort /proc. Read only bounded parent/start metadata
        # until descendants are known; never inspect unrelated argv or FDs.
        for path in root.iterdir():
            if not path.name.isascii() or not path.name.isdecimal():
                continue
            if len(processes) >= MAX_PROCESSES:
                return unknown
            pid = int(path.name)
            processes[pid] = _stat(root, pid)
        if processes.get(owner.pid) != (owner.parent, owner.started):
            return unknown
        descendants = {owner.pid}
        while True:
            found = {pid for pid, (parent, _) in processes.items() if parent in descendants}
            expanded = descendants | found
            if expanded == descendants:
                break
            descendants = expanded
        def unchanged(pid):
            return (_stat(root, pid) == processes[pid] and _scoped(root, pid, owner.namespace))
        gpu = []
        for pid in descendants - {owner.pid}:
            if not unchanged(pid):
                return unknown
            command = _small(root / str(pid) / "cmdline", 32768)
            if not unchanged(pid):
                return unknown
            if b"--type=gpu-process" in command.split(b"\0"):
                gpu.append(pid)
        descriptors, device_open = 0, False
        for pid in gpu:
            for descriptor in (root / str(pid) / "fd").iterdir():
                if not descriptor.name.isascii() or not descriptor.name.isdecimal():
                    continue
                if descriptors >= MAX_FDS:
                    return unknown
                descriptors += 1
                if RENDER_NODE.fullmatch(str(descriptor.readlink())):
                    device_open = True
        # Recheck every link in each observed ancestry chain, not only GPU PIDs.
        if owner_current(owner, root) is not True or any(not unchanged(pid) for pid in descendants):
            return unknown
        return {"gpu_process_count": len(gpu), "gpu_render_device_open": device_open}
    except (OSError, ValueError, IndexError, KeyError):
        return unknown
