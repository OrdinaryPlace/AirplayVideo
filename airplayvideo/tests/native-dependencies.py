"""Offline runtime-image gate for production YouTube dependencies.

Run inside the built image with its normal ``python3``. This does not contact
YouTube or any receiver. VAAPI inventory is explicitly not a hardware test.
"""
from __future__ import annotations

import argparse
import importlib
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from service.native_stream import parse_capabilities  # noqa: E402


def command(*args):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                            text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError("Dependency command failed: " + Path(args[0]).name)
    return result.stdout


def check():
    prefix = Path(sys.prefix).resolve()
    if prefix != Path("/opt/airplayvideo/venv"):
        raise RuntimeError("The service must use the production resolver virtual environment")
    expected = {"yt-dlp": "2026.8.19", "yt-dlp-ejs": "0.8.0"}
    for name, version in expected.items():
        if importlib.metadata.version(name) != version:
            raise RuntimeError("Unexpected pinned resolver package version: " + name)
        module = importlib.import_module(name.replace("-", "_"))
        if not Path(module.__file__).resolve().is_relative_to(prefix):
            raise RuntimeError("Resolver package is outside the service Python environment")
        distribution = importlib.metadata.distribution(name)
        destination = Path("/opt/licenses/python") / name
        copied = 0
        for entry in distribution.files or ():
            if "licenses" in entry.parts or entry.name.upper().startswith(("LICENSE", "COPYING", "NOTICE", "THIRD_PARTY_LICENSE")):
                target = destination / entry
                if not target.resolve().is_relative_to(destination):
                    raise RuntimeError("Unexpected resolver license path")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(distribution.locate_file(entry), target)
                copied += 1
        if not copied:
            raise RuntimeError("Resolver package license notices are missing: " + name)
    if importlib.util.find_spec("pyatv") is not None:
        raise RuntimeError("The optional pyatv experiment is not a production dependency")
    # Debian app dependencies remain available through --system-site-packages.
    for module in ("aiohttp", "paho.mqtt.client", "Crypto.Cipher.AES"):
        importlib.import_module(module)
    node_version = command("node", "--version").strip()
    if node_version != "v22.23.3":
        raise RuntimeError("Unexpected pinned Node runtime version")

    configuration = command("ffmpeg", "-hide_banner", "-buildconf")
    # FFmpeg writes its configuration banner to stderr, so query with -L as
    # well and use the retained build-time configuration for policy checks.
    configuration = Path("/opt/dependency-sources/ffmpeg-buildconf.txt").read_text()
    if not all(option in configuration for option in ("--disable-gpl", "--disable-nonfree", "--enable-gnutls", "--enable-libdav1d")):
        raise RuntimeError("The required LGPL-preserving FFmpeg configuration is missing")
    if re.search(r"--enable-(?:gpl|nonfree|libx264|libx265)\b", configuration):
        raise RuntimeError("A disallowed FFmpeg dependency was enabled")
    if "Lesser General Public License" not in command("ffmpeg", "-hide_banner", "-L"):
        raise RuntimeError("FFmpeg does not report its expected LGPL license")

    required = {"protocols": {"file", "pipe", "http", "https", "tcp", "tls", "crypto"},
                "muxers": {"hls", "mp4", "null"},
                "decoders": {"h264", "hevc", "vp9", "av1", "aac", "opus"},
                "encoders": {"libopenh264", "h264_vaapi", "hevc_vaapi", "aac"},
                "filters": {"color", "hwupload", "format", "scale", "aresample", "bwdif"}}
    inventory = {}
    for kind, expected_names in required.items():
        names = parse_capabilities(command("ffmpeg", "-hide_banner", "-" + kind), kind)
        missing = expected_names - names
        if missing:
            raise RuntimeError("Missing FFmpeg " + kind + ": " + ", ".join(sorted(missing)))
        inventory[kind] = sorted(expected_names)
    if {"libx264", "libx265"} & parse_capabilities(command("ffmpeg", "-hide_banner", "-encoders"), "encoders"):
        raise RuntimeError("GPL encoders are present in the production image")
    probe_protocols = parse_capabilities(command("ffprobe", "-hide_banner", "-protocols"), "protocols")
    if not required["protocols"] <= probe_protocols:
        raise RuntimeError("FFprobe lacks required media input protocols")

    with tempfile.TemporaryDirectory(prefix="native-dependencies-") as directory:
        root = Path(directory)
        # This exercises actual OpenH264, AAC and fragmented-MP4 HLS command
        # support. Hardware encode throughput needs the real HA render device.
        command("ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-f", "lavfi", "-i", "color=c=black:s=320x180:r=60",
                "-f", "lavfi", "-i", "sine=frequency=1000:sample_rate=48000",
                "-t", "1", "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "libopenh264", "-profile:v", "high", "-b:v", "4000000",
                "-c:a", "aac", "-profile:a", "aac_low", "-ar", "48000", "-ac", "2",
                "-fps_mode", "passthrough", "-f", "hls", "-hls_segment_type", "fmp4",
                "-hls_time", "1", "-hls_list_size", "0", "-hls_flags", "independent_segments+temp_file",
                "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename", str(root / "segment-%08d.m4s"),
                str(root / "index.m3u8"))
        for name in ("index.m3u8", "init.mp4", "segment-00000000.m4s"):
            if not (root / name).is_file() or not (root / name).stat().st_size:
                raise RuntimeError("Fragmented-MP4 HLS packaging did not produce complete files")
        probe = json.loads(command("ffprobe", "-v", "error", "-protocol_whitelist", "file,crypto",
                                   "-show_entries", "stream=codec_name,width,height,avg_frame_rate,channels,sample_rate",
                                   "-of", "json", str(root / "index.m3u8")))
        video = next(row for row in probe["streams"] if row["codec_name"] == "h264")
        audio = next(row for row in probe["streams"] if row["codec_name"] == "aac")
        if (video["width"], video["height"], video["avg_frame_rate"], audio["channels"], audio["sample_rate"]) != (320, 180, "60/1", 2, "48000"):
            raise RuntimeError("HLS output did not preserve the requested video and audio properties")
    return {"python_executable": sys.executable, "resolver_packages": expected, "node": node_version,
            "ffmpeg": inventory, "h264_aac_fmp4_hls_encode_verified": True,
            "hardware_encode_verified": False, "gpl_encoders_enabled": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-report", type=Path)
    args = parser.parse_args()
    try:
        result = check()
        if args.write_report:
            args.write_report.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    except Exception as error:
        message = str(error) if isinstance(error, RuntimeError) else "Native dependency verification failed"
        print(json.dumps({"verified": False, "error": message}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
