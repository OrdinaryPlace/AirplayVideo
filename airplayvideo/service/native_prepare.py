"""Bounded, account-free YouTube VOD preparation for the native experiment.

Run with ``python -m service.native_prepare --help``. This prepares one local
MP4; it never contacts an Apple TV. yt-dlp and a full external FFmpeg build are
optional experiment dependencies, not new dependencies of the running app.
Only a source prefix is fetched, so files needing a remote index fail safely.
"""
from __future__ import annotations

import argparse
import contextlib
from fractions import Fraction
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .native_plan import PlanError, plan_media


class PrepareError(ValueError):
    """A fixed explanation that contains no source URL or process output."""


_MAX_METADATA = 2 * 1024 * 1024
_MAX_OUTPUT = 256 * 1024 * 1024
_FORMAT_FIELDS = ("format_id", "width", "height", "fps", "vcodec", "acodec",
                  "dynamic_range", "pix_fmt", "vbr", "tbr", "abr", "asr",
                  "audio_channels", "has_drm", "url", "protocol", "filesize")


def youtube_url(value):
    """Normalize an explicit public video URL; playlists and other sites fail."""
    try:
        url = urlsplit(value)
        if (url.scheme != "https" or url.username or url.password or url.port not in (None, 443)):
            raise ValueError
        host = (url.hostname or "").lower()
        if host == "youtu.be":
            video_id = url.path.removeprefix("/")
        elif host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            if url.path == "/watch":
                ids = parse_qs(url.query).get("v", [])
                video_id = ids[0] if len(ids) == 1 else ""
            elif url.path.startswith(("/shorts/", "/embed/")):
                video_id = url.path.split("/")[-1]
            else:
                video_id = ""
        else:
            video_id = ""
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PrepareError("Supply an HTTPS YouTube video URL") from None
    return "https://www.youtube.com/watch?v=" + video_id


class _QuietLogger:
    def debug(self, *args, **kwargs):
        pass

    info = warning = error = debug


def _extract_metadata(url, ydl_factory=None):
    if ydl_factory is None:
        from yt_dlp import YoutubeDL
        ydl_factory = YoutubeDL
    # The Python API does not read CLI configuration. Explicitly disable every
    # account/cache input; never import a browser profile, cookies or netrc.
    options = {"quiet": True, "no_warnings": True, "logger": _QuietLogger(),
               "skip_download": True, "noplaylist": True, "cachedir": False,
               "cookiefile": None, "cookiesfrombrowser": None, "usenetrc": False,
               "username": None, "password": None, "socket_timeout": 10,
               "retries": 1, "extractor_retries": 1, "js_runtimes": {"node": {}}}
    with ydl_factory(options) as resolver:
        data = resolver.extract_info(url, download=False)
    if not isinstance(data, dict) or data.get("_type") in {"playlist", "multi_video"}:
        raise PrepareError("The URL did not resolve to one video")
    if data.get("availability") not in (None, "public", "unlisted"):
        raise PrepareError("The video is not available without an account")
    result = {key: data.get(key) for key in ("is_live", "live_status", "has_drm", "duration")}
    formats = data.get("formats")
    if not isinstance(formats, list) or len(formats) > 10000:
        raise PrepareError("The video has no usable media formats")
    result["formats"] = [{key: row[key] for key in _FORMAT_FIELDS if key in row}
                         for row in formats if isinstance(row, dict)]
    return result


def _resolver_worker(url, sender):
    try:
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            result = {"ok": True, "metadata": _extract_metadata(url)}
            payload = json.dumps(result).encode()
        if len(payload) > _MAX_METADATA:
            raise ValueError
    except Exception:
        payload = b'{"ok":false}'
    try:
        sender.send_bytes(payload)
    finally:
        sender.close()


def resolve_metadata(url):
    """Kill the isolated resolver after 45 seconds, with no diagnostic leakage."""
    url = youtube_url(url)
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_resolver_worker, args=(url, sender), daemon=True)
    try:
        process.start()
        sender.close()
        if not receiver.poll(45):
            raise PrepareError("Public video resolution timed out")
        result = json.loads(receiver.recv_bytes(_MAX_METADATA))
        if not result.get("ok"):
            raise PrepareError("Public video resolution failed; check the optional yt-dlp and Node dependencies")
        return result["metadata"]
    except (OSError, EOFError, ValueError) as error:
        if isinstance(error, PrepareError):
            raise
        raise PrepareError("Public video resolution failed") from None
    finally:
        receiver.close()
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(3)
            if process.is_alive():
                process.kill()
                process.join(3)


def _cdn_url(value):
    try:
        url = urlsplit(value)
        valid = (url.scheme == "https" and (url.hostname or "").endswith(".googlevideo.com")
                 and url.path == "/videoplayback" and not url.username and not url.password
                 and url.port in (None, 443))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise PrepareError("The selected format is not a direct HTTPS YouTube media resource")
    return value


class _CDNRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return super().redirect_request(request, fp, code, message, headers, _cdn_url(newurl))


def prefix_byte_limit(row, seconds, duration, *, audio=False):
    """Request a clip-sized prefix with headroom, never the entire size cap."""
    estimates = []
    for key in (("abr", "tbr") if audio else ("vbr", "tbr")):
        try:
            value = float(row.get(key, 0))
            if math.isfinite(value) and value > 0:
                estimates.append(value * 125)
        except (ValueError, TypeError, OverflowError):
            pass
    try:
        value = float(row.get("filesize", 0)) / duration
        if math.isfinite(value) and value > 0:
            estimates.append(value)
    except (ValueError, TypeError, OverflowError, ZeroDivisionError):
        pass
    fallback = 32000 if audio else 512000 * max(1, row["width"] * row["height"] / (1920 * 1080))
    byte_rate = max(estimates, default=fallback)
    maximum = (8 if audio else 128) * 1024 * 1024
    minimum = (1 if audio else 4) * 1024 * 1024
    # Four seconds of startup plus 2x mean bitrate tolerates ordinary bitrate
    # variation and container headers. An insufficient prefix fails validation.
    estimate = min(maximum, byte_rate * (seconds + 4) * 2 + (256 * 1024 if audio else 1024 * 1024))
    return max(minimum, math.ceil(estimate))


def download_prefix(row, destination, *, audio=False, byte_limit=None):
    """Download at most 128 MiB video / 8 MiB audio, for at most 60 seconds."""
    if row.get("protocol") != "https":
        raise PrepareError("The selected media format needs an unsupported download protocol")
    url = _cdn_url(row.get("url"))
    limit = (8 if audio else 128) * 1024 * 1024
    if byte_limit is not None:
        if isinstance(byte_limit, bool) or not isinstance(byte_limit, int) or not 1 <= byte_limit <= limit:
            raise PrepareError("The media prefix byte limit is invalid")
        limit = byte_limit
    request = Request(url, headers={"Range": f"bytes=0-{limit - 1}",
                                   "User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"})
    # Do not import proxy credentials from the process environment.
    opener = build_opener(ProxyHandler({}), HTTPSHandler(), _CDNRedirect())
    deadline = time.monotonic() + 60
    total = 0
    try:
        with opener.open(request, timeout=10) as response, open(destination, "xb") as output:
            if response.status not in (200, 206):
                raise PrepareError("The media download was rejected")
            if response.status == 206 and not re.fullmatch(r"bytes 0-\d+/\d+", response.headers.get("Content-Range", "")):
                raise PrepareError("The media download returned an unexpected byte range")
            while total < limit:
                if time.monotonic() > deadline:
                    raise PrepareError("The media prefix download timed out")
                block = response.read(min(256 * 1024, limit - total))
                if not block:
                    break
                output.write(block)
                total += len(block)
        if total == 0:
            raise PrepareError("The media download was empty")
        return total
    except PrepareError:
        raise
    except Exception:
        raise PrepareError("The media prefix download failed; resolving again may refresh an expired URL") from None


def _binary(value):
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise PrepareError("Supply an absolute path to an executable FFmpeg or FFprobe binary")
    return str(path.resolve())


def _run(arguments, *, timeout, capture=False):
    try:
        with tempfile.TemporaryFile() as output:
            subprocess.run(arguments, stdin=subprocess.DEVNULL,
                           stdout=output if capture else subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=timeout, check=True, shell=False)
            if capture:
                output.seek(0)
                data = output.read(_MAX_METADATA + 1)
                if len(data) > _MAX_METADATA:
                    raise PrepareError("The media probe returned excessive data")
                return json.loads(data)
    except PrepareError:
        raise
    except Exception:
        raise PrepareError("The local media command failed or timed out; check codec support and source prefix completeness") from None


def probe_media(ffprobe, path):
    return _run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe", "-show_entries",
                 "stream=codec_type,codec_name,profile,level,pix_fmt,width,height,avg_frame_rate,"
                 "r_frame_rate,color_transfer,sample_rate,channels,duration,nb_frames:format=duration,size",
                 "-of", "json", str(path)], timeout=20, capture=True)


def probe_initial_pts(ffprobe, path, kind):
    """Read the first effective presentation time, including audio priming."""
    if kind not in {"video", "audio"}:
        raise PrepareError("Choose one audio or video track for timestamp verification")
    result = _run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                   "-select_streams", "v:0" if kind == "video" else "a:0",
                   "-read_intervals", "%+#1", "-show_entries",
                   "packet=pts_time:packet_side_data=side_data_type,skip_samples:stream=codec_type,sample_rate",
                   "-of", "json", str(path)], timeout=20, capture=True)
    try:
        packets = result["packets"]
        if len(packets) != 1:
            raise ValueError
        packet = packets[0]
        initial = Fraction(packet["pts_time"])
        if kind == "audio":
            rate = int(_stream(result, "audio")["sample_rate"])
            if not 8000 <= rate <= 192000:
                raise ValueError
            for side_data in packet.get("side_data_list", []):
                if side_data.get("side_data_type") == "Skip Samples":
                    skip = int(side_data["skip_samples"])
                    if not 0 <= skip <= rate:
                        raise ValueError
                    initial += Fraction(skip, rate)
        if not -10 <= initial < 86400:
            raise ValueError
        return initial
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        raise PrepareError("The initial presentation timestamp could not be verified") from None


def validate_initial_timing(video_pts, audio_pts, *, source_offset=None):
    # FFmpeg normally subtracts each input's start time. For this bounded
    # experiment, reject an offset source rather than silently lose its sync.
    # FFprobe prints six decimal places; allow only rounding-level differences.
    offset = audio_pts - video_pts
    tolerance = Fraction(1, 10000)
    if source_offset is None and abs(offset) > tolerance:
        raise PrepareError("The source audio and video start at different presentation times; offset preservation is not implemented")
    if source_offset is not None and abs(offset - source_offset) > tolerance:
        raise PrepareError("The prepared sample changed the relative audio and video presentation timestamps")
    return offset


def _stream(probe, kind):
    rows = [row for row in probe.get("streams", []) if row.get("codec_type") == kind]
    if len(rows) != 1:
        raise PrepareError("The media probe must contain exactly one selected track of each kind")
    return rows[0]


def _rate(row):
    try:
        rate = Fraction(row.get("avg_frame_rate") or row.get("r_frame_rate", "0/1"))
        if not 0 < rate <= 60:
            raise ValueError
        return rate
    except (ValueError, TypeError, ZeroDivisionError):
        raise PrepareError("The media probe has an invalid frame rate") from None


def validate_tracks(plan, video_probe, audio_probe, *, output=False, seconds=None, source_rate=None):
    """Confirm actual streams; metadata alone never authorizes a copied track."""
    try:
        video, audio = _stream(video_probe, "video"), _stream(audio_probe, "audio")
        expected = plan["target"] if output else plan["video"]
        expected_codec = expected["video_codec"] if output else expected["codec"]
        rate = _rate(video)
        if (video.get("codec_name") != expected_codec or video.get("width") != expected["width"]
                or video.get("height") != expected["height"]
                or abs(rate - Fraction(expected["fps"])) > Fraction(1, 100)):
            raise PrepareError("The media probe differs from the selected codec, dimensions or frame rate")
        if source_rate is not None and abs(rate - source_rate) > Fraction(1, 100000):
            raise PrepareError("Output frame rate did not preserve the probed source frame rate")
        if video.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
            raise PrepareError("HDR output is not verified by this experiment")
        if output or plan["copy_video"]:
            level = int(video.get("level", 0))
            if expected_codec == "h264":
                maximum = 42 if plan["receiver"]["short_edge"] == 1080 else 52
                if (video.get("profile") not in {"Constrained Baseline", "Baseline", "Main", "High"}
                        or video.get("pix_fmt") not in {"yuv420p", "yuvj420p"} or not 0 < level <= maximum
                        or ("Baseline" in video["profile"] and level > 30)):
                    raise PrepareError("The H.264 profile, pixel format or level is not verified for the receiver")
            elif expected_codec == "hevc":
                maximum = 123 if plan["receiver"]["short_edge"] == 1080 else 153
                if (video.get("profile") not in {"Main", "Main 10"}
                        or video.get("pix_fmt") not in {"yuv420p", "yuv420p10le"} or not 0 < level <= maximum
                        or rate > plan["receiver"]["hevc_max_fps"]):
                    raise PrepareError("The HEVC profile, pixel format, level or frame rate is not verified for the receiver")
        expected_audio = "aac" if output else plan["audio"]["codec"]
        if audio.get("codec_name") != expected_audio:
            raise PrepareError("The audio probe differs from the selected codec")
        if output or plan["copy_audio"]:
            if (audio.get("profile") != "LC" or not 1 <= int(audio.get("channels", 0)) <= 2
                    or not 8000 <= int(audio.get("sample_rate", 0)) <= 48000):
                raise PrepareError("The AAC profile, channel count or sample rate is not verified for the receiver")
        if output:
            for row in (video, audio):
                duration = float(row.get("duration", 0))
                if not seconds - 0.25 <= duration <= seconds + 0.25:
                    raise PrepareError("The prepared media did not retain the requested audio and video duration")
        return rate
    except PrepareError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError):
        raise PrepareError("The media probe is incomplete or invalid") from None


def ffmpeg_arguments(ffmpeg, video_path, audio_path, output_path, plan, seconds):
    arguments = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n",
                 "-protocol_whitelist", "file,pipe", "-i", str(video_path),
                 "-protocol_whitelist", "file,pipe", "-i", str(audio_path), "-map", "0:v:0", "-map", "1:a:0",
                 "-t", str(seconds), "-map_metadata", "-1", "-map_chapters", "-1"]
    if plan["copy_video"]:
        arguments += ["-c:v", "copy"]
    elif plan["target"]["video_codec"] == "hevc":
        arguments += ["-c:v", "libx265", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
                      "-x265-params", "log-level=error:pools=2:frame-threads=2"]
    else:
        arguments += ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p",
                      "-profile:v", "high", "-level:v", "4.2", "-threads", "4"]
    if plan["target"]["video_codec"] == "hevc":
        arguments += ["-tag:v", "hvc1"]
    arguments += (["-c:a", "copy"] if plan["copy_audio"] else
                  ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k", "-ac", "2", "-ar", "48000"])
    return arguments + ["-fps_mode", "passthrough", "-movflags", "+faststart", "-fs", str(_MAX_OUTPUT),
                        "-f", "mp4", str(output_path)]


def plan_direct_media(metadata, receiver_model, *, max_resolution=None):
    best = plan_media(metadata, receiver_model, max_resolution=max_resolution)
    direct = {**metadata, "formats": [row for row in metadata["formats"]
                                      if isinstance(row, dict) and row.get("protocol") == "https"]}
    plan = plan_media(direct, receiver_model, max_resolution=max_resolution)
    if any(plan["target"][key] != best["target"][key] for key in ("width", "height", "fps")):
        raise PrepareError("The highest-quality source requires a download protocol not supported by this experiment")
    plan["reasons"].append("Selected direct HTTPS tracks without reducing the best available source dimensions or frame rate")
    return plan


def prepare_media(url, receiver_model, seconds, output_dir, ffmpeg, ffprobe, *, max_resolution=None):
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 30:
        raise PrepareError("Choose a whole-number sample duration from 1 to 30 seconds")
    url = youtube_url(url)
    ffmpeg, ffprobe = _binary(ffmpeg), _binary(ffprobe)
    directory = Path(output_dir).expanduser().resolve()
    if not directory.is_dir():
        raise PrepareError("Choose an existing output directory")
    metadata = resolve_metadata(url)
    plan = plan_direct_media(metadata, receiver_model, max_resolution=max_resolution)
    try:
        if not seconds <= float(metadata.get("duration", 0)) < 86400:
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise PrepareError("The requested sample needs a known, sufficiently long VOD duration") from None
    rows = {row["format_id"]: row for row in metadata["formats"] if isinstance(row.get("format_id"), str)}
    # A fresh private subdirectory prevents overwriting existing recordings.
    work = Path(tempfile.mkdtemp(prefix="native-sample-", dir=directory))
    video_path, audio_path = work / "video.source", work / "audio.source"
    staged, final = work / "sample.unverified.mp4", work / "sample.mp4"
    video_row, audio_row = rows[plan["video"]["format_id"]], rows[plan["audio"]["format_id"]]
    duration = float(metadata["duration"])
    download_prefix(video_row, video_path, byte_limit=prefix_byte_limit(video_row, seconds, duration))
    download_prefix(audio_row, audio_path, audio=True,
                    byte_limit=prefix_byte_limit(audio_row, seconds, duration, audio=True))
    video_probe, audio_probe = probe_media(ffprobe, video_path), probe_media(ffprobe, audio_path)
    rate = validate_tracks(plan, video_probe, audio_probe)
    source_offset = validate_initial_timing(probe_initial_pts(ffprobe, video_path, "video"),
                                            probe_initial_pts(ffprobe, audio_path, "audio"))
    _run(ffmpeg_arguments(ffmpeg, video_path, audio_path, staged, plan, seconds), timeout=300)
    if not staged.is_file() or not 0 < staged.stat().st_size < _MAX_OUTPUT:
        raise PrepareError("The prepared sample was empty or reached its size limit")
    result = probe_media(ffprobe, staged)
    validate_tracks(plan, result, result, output=True, seconds=seconds, source_rate=rate)
    output_offset = validate_initial_timing(probe_initial_pts(ffprobe, staged, "video"),
                                            probe_initial_pts(ffprobe, staged, "audio"),
                                            source_offset=source_offset)
    staged.rename(final)
    report = {"ready": True, "experimental": True, "seconds": seconds, "plan": plan,
              "sample_directory": work.name, "filename": final.name,
              "verified": {"width": plan["target"]["width"], "height": plan["target"]["height"],
                           "fps": str(rate), "video_codec": plan["target"]["video_codec"],
                           "audio_codec": "aac", "bytes": final.stat().st_size,
                           "source_audio_minus_video_start_seconds": float(source_offset),
                           "output_audio_minus_video_start_seconds": float(output_offset)},
              "receiver_playback_verified": False}
    (work / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return final, report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare a bounded public YouTube MP4; never start TV playback")
    parser.add_argument("--url", required=True)
    parser.add_argument("--receiver-model", required=True)
    parser.add_argument("--seconds", type=int, default=20)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument("--max-resolution", type=int, help="Optional source short-edge ceiling, such as 1080")
    arguments = parser.parse_args(argv)
    try:
        _, report = prepare_media(arguments.url, arguments.receiver_model, arguments.seconds,
                                  arguments.output_dir, arguments.ffmpeg, arguments.ffprobe,
                                  max_resolution=arguments.max_resolution)
        # Only controlled metadata is printed. Local paths and source URLs stay
        # out of diagnostic output; the new output subdirectory contains files.
        print(json.dumps(report))
        return 0
    except (PrepareError, PlanError) as error:
        print(json.dumps({"ready": False, "error": str(error)}))
        return 1
    except Exception:
        print(json.dumps({"ready": False, "error": "Sample preparation failed"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
