"""Experimental progressive native HLS preparation; no receiver commands.

The session exposes generated paths to a separately scoped media origin. Its
rolling playlist supports ongoing playback and seeking within its buffer, not
whole-video random access. Existing recordings and application state are never
used. GPL encoders are allowed only by an explicit experimental opt-in.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
from fractions import Fraction
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
import time

from .native_prepare import (PrepareError, _binary, _cdn_url, _extract_metadata,
                             _stream, plan_direct_media, validate_initial_timing,
                             validate_tracks, youtube_url)
from .native_plan import PlanError


class StreamError(ValueError):
    """A controlled explanation containing no URLs, credentials or raw logs."""


_MAX_COMMAND_OUTPUT = 2 * 1024 * 1024
_SEGMENT = re.compile(r"segment-\d{8}\.m4s\Z")
_PROBE_FIELDS = ("stream=codec_type,codec_name,profile,level,pix_fmt,width,height,avg_frame_rate,"
                 "r_frame_rate,color_transfer,sample_rate,channels,duration,nb_frames:format=duration,size")


async def _gather(*coroutines):
    tasks = [asyncio.create_task(coroutine) for coroutine in coroutines]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def parse_capabilities(text, kind):
    """Parse FFmpeg's stable tabular protocol/codec/muxer/filter inventories."""
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if kind == "protocols" and len(parts) == 1 and re.fullmatch(r"[a-z0-9_]+", parts[0]):
            names.add(parts[0])
        elif kind in {"encoders", "decoders"} and len(parts) >= 2 and re.fullmatch(r"[VAS\.][A-Z\.]{5}", parts[0]):
            names.add(parts[1])
        elif kind == "muxers" and len(parts) >= 2 and parts[0] == "E":
            names.update(parts[1].split(","))
        elif kind == "filters" and len(parts) >= 2 and re.fullmatch(r"[TSC\.]{3}", parts[0]):
            names.add(parts[1])
    if kind == "decoders" and {"libdav1d", "libaom-av1"} & names:
        names.add("av1")
    return names


def child_environment():
    environment = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH") if key in os.environ}
    # The resolver needs this package, never arbitrary inherited import paths
    # or Supervisor/MQTT/API credentials from the service process environment.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    return environment


def choose_encoder(plan, available, *, requested=None, vaapi_device=None, allow_gpl=False):
    if plan["copy_video"]:
        return None
    codec = plan["target"]["video_codec"]
    allowed = {"h264": {"libopenh264", "h264_vaapi", "libx264"},
               "hevc": {"hevc_vaapi", "libx265"}}[codec]
    encoder = requested or ("libopenh264" if codec == "h264" else "hevc_vaapi")
    if encoder not in allowed:
        raise StreamError("The selected video encoder does not produce the required target codec")
    if encoder in {"libx264", "libx265"} and not allow_gpl:
        raise StreamError("GPL software encoders require an explicit experimental opt-in")
    if encoder not in available:
        raise StreamError("A compatible video encoder is unavailable; source quality was not reduced")
    if encoder.endswith("_vaapi") and not vaapi_device:
        raise StreamError("The selected VAAPI encoder requires an explicit render device and successful encode verification")
    return encoder


def video_arguments(plan, encoder):
    if plan["copy_video"]:
        return ["-c:v", "copy"] + (["-tag:v", "hvc1"] if plan["target"]["video_codec"] == "hevc" else [])
    target = plan["target"]
    rate = Fraction(target["fps"])
    gop = max(1, round(rate * 4))
    bitrate = min(40000000, max(4000000, round(target["width"] * target["height"] * float(rate) * 0.15)))
    args = ["-c:v", encoder]
    if encoder in {"hevc_vaapi", "h264_vaapi"}:
        args += ["-vf", "format=nv12,hwupload", "-profile:v", "main" if encoder == "hevc_vaapi" else "high",
                 "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
                 "-g", str(gop), "-bf", "0"]
    elif encoder == "libopenh264":
        args += ["-pix_fmt", "yuv420p", "-profile:v", "high", "-b:v", str(bitrate),
                 "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2), "-g", str(gop)]
    elif encoder == "libx265":
        args += ["-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "20", "-x265-params",
                 f"log-level=error:pools=2:frame-threads=2:keyint={gop}:min-keyint={gop}:scenecut=0:open-gop=0"]
    elif encoder == "libx264":
        args += ["-pix_fmt", "yuv420p", "-preset", "fast", "-crf", "18", "-profile:v", "high",
                 "-level:v", "4.2", "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-threads", "4"]
    else:
        raise StreamError("A compatible encoder must be selected before conversion")
    args += ["-force_key_frames", "expr:gte(t,n_forced*4)"]
    if target["video_codec"] == "hevc":
        args += ["-tag:v", "hvc1"]
    return args


def hardware_arguments(encoder, device):
    if encoder and encoder.endswith("_vaapi"):
        if not isinstance(device, str) or not re.fullmatch(r"/dev/dri/renderD\d{3}", device):
            raise StreamError("Choose an explicit VAAPI render node")
        return ["-init_hw_device", "vaapi=airplay:" + device, "-filter_hw_device", "airplay"]
    return []


def hls_arguments(ffmpeg, video, audio, directory, plan, *, encoder=None, vaapi_device=None, remote=True):
    """Build shell-free arguments; source values must never be put in logs."""
    args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-n"]
    args += hardware_arguments(encoder, vaapi_device)
    for source in (video, audio):
        if remote:
            _cdn_url(source)
            args += ["-protocol_whitelist", "https,tls,tcp,crypto", "-rw_timeout", "10000000"]
        else:
            args += ["-protocol_whitelist", "file,pipe"]
        # Reading at media speed prevents the rolling window outrunning the TV.
        args += ["-readrate", "1", "-i", str(source)]
    args += ["-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1", "-map_chapters", "-1"]
    args += video_arguments(plan, encoder)
    args += (["-c:a", "copy"] if plan["copy_audio"] else
             ["-c:a", "aac", "-profile:a", "aac_low", "-b:a", "192k", "-ac", "2", "-ar", "48000"])
    return args + ["-shortest", "-fps_mode", "passthrough", "-f", "hls", "-hls_segment_type", "fmp4",
                   "-hls_time", "4", "-hls_list_size", "12", "-hls_delete_threshold", "6",
                   "-hls_flags", "delete_segments+independent_segments+temp_file",
                   "-hls_fmp4_init_filename", "init.mp4", "-hls_segment_filename",
                   str(directory / "segment-%08d.m4s"), str(directory / "index.m3u8")]


def playlist_segments(directory):
    """Accept only finalized files named by the controlled HLS muxer."""
    playlist = directory / "index.m3u8"
    if not playlist.is_file() or playlist.is_symlink() or playlist.stat().st_size > 65536:
        return []
    text = playlist.read_text()
    if not text.startswith("#EXTM3U") or '#EXT-X-MAP:URI="init.mp4"' not in text:
        raise StreamError("The generated HLS playlist is invalid")
    names = [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]
    if not names:
        return []
    if len(names) > 12 or not all(_SEGMENT.fullmatch(name) for name in names):
        raise StreamError("The generated HLS playlist contains an unexpected media path")
    for name in ["init.mp4", *names]:
        path = directory / name
        if path.is_symlink() or not path.is_file() or not path.stat().st_size:
            return []
    return names


def source_duration(probe, kind):
    value = _stream(probe, kind).get("duration")
    if value in (None, "N/A"):
        value = probe.get("format", {}).get("duration")
    if value in (None, "N/A"):
        return None
    try:
        duration = float(value)
        if not math.isfinite(duration) or duration <= 0:
            raise ValueError
        return duration
    except (TypeError, ValueError, OverflowError):
        raise StreamError("The source probe contains an invalid duration") from None


def validate_source_durations(expected, video_probe, audio_probe):
    video, audio = source_duration(video_probe, "video"), source_duration(audio_probe, "audio")
    # Extractor durations may be rounded to a whole second; actual stream
    # durations must agree more closely (AAC packet padding is only ~23 ms).
    if any(value is not None and abs(value - expected) > 1 for value in (video, audio)):
        raise StreamError("A selected source track does not cover the resolved video duration")
    if video is not None and audio is not None and abs(video - audio) > 0.25:
        raise StreamError("The selected audio and video durations do not agree")


def playlist_durations(directory):
    text = (directory / "index.m3u8").read_text()
    sequence, pending, result = None, None, []
    try:
        for line in text.splitlines():
            if line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
                sequence = int(line.split(":", 1)[1])
                if not 0 <= sequence <= 10000:
                    raise ValueError
            elif line.startswith("#EXTINF:"):
                pending = Fraction(line.split(":", 1)[1].split(",", 1)[0])
                if not 0 < pending <= 180:
                    raise ValueError
            elif line and not line.startswith("#"):
                if sequence is None or pending is None or not _SEGMENT.fullmatch(line):
                    raise ValueError
                number = int(line[len("segment-"):-len(".m4s")])
                if number != sequence + len(result):
                    raise ValueError
                result.append((number, pending))
                pending = None
        return result
    except (ValueError, TypeError, ZeroDivisionError):
        raise StreamError("The generated HLS durations or media sequence are invalid") from None


class NativeHLSStream:
    def __init__(self, *, output_dir, ffmpeg, ffprobe, startup_timeout=120, session_timeout=14400,
                 disk_limit=512 * 1024 * 1024, video_encoder=None, vaapi_device=None, allow_gpl=False):
        if not 10 <= startup_timeout <= 180 or not 30 <= session_timeout <= 14400:
            raise StreamError("Choose bounded startup and session timeouts")
        if isinstance(disk_limit, bool) or not isinstance(disk_limit, int) or not 16 * 1024 * 1024 <= disk_limit <= 2 * 1024**3:
            raise StreamError("Choose a session disk cap between 16 MiB and 2 GiB")
        if not isinstance(allow_gpl, bool):
            raise StreamError("The experimental GPL encoder choice must be an explicit boolean")
        self.root = Path(output_dir).resolve()
        if not self.root.is_dir():
            raise StreamError("Choose an existing media output directory")
        self.ffmpeg, self.ffprobe = _binary(ffmpeg), _binary(ffprobe)
        self.startup_timeout, self.session_timeout, self.disk_limit = startup_timeout, session_timeout, disk_limit
        self.requested_encoder, self.vaapi_device, self.allow_gpl = video_encoder, vaapi_device, allow_gpl
        self.directory = None
        self.plan = None
        self.encoder = None
        self.process = None
        self._jobs = set()
        self._spawning = set()
        self._owned_groups = set()
        self._startup_task = self._monitor_task = self._verification_task = self._close_task = None
        self._ready = asyncio.Event()
        self._finished = asyncio.Event()
        self.closed = asyncio.Event()
        self._error = None
        self._state = "new"
        self._bytes = 0
        self._started = None
        self._source_rate = self._source_offset = None
        self.expected_duration = None
        self._prepared_duration = Fraction(0)
        self._last_segment = -1

    @property
    def playlist(self):
        return self.directory / "index.m3u8" if self.directory is not None else None

    @property
    def status(self):
        return {"state": self._state, "ready": self._state in {"streaming", "finished"} and self._error is None,
                "finished": self._finished.is_set(), "error": self._error,
                "disk_bytes": self._bytes, "experimental": True,
                "receiver_playback_verified": False,
                "quality": self.plan["target"] if self.plan else None,
                "video_encoder": self.encoder,
                "expected_duration_seconds": self.expected_duration,
                "prepared_duration_seconds": float(self._prepared_duration)}

    async def _stop_process(self, process):
        if process.pid not in self._owned_groups:
            # Never signal a process group that this session did not create.
            return
        try:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            deadline = time.monotonic() + 2
            # A resolver leader can exit while its Node child remains. Wait
            # on the owned group, not only on the already-exited leader.
            while time.monotonic() < deadline:
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.02)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await asyncio.wait_for(process.wait(), 2)
        finally:
            self._owned_groups.discard(process.pid)

    async def _spawn(self, args, **kwargs):
        if self._close_task is not None:
            raise StreamError("The native HLS session is closing")
        task = asyncio.create_task(asyncio.create_subprocess_exec(*args, env=child_environment(), start_new_session=True, **kwargs))
        self._spawning.add(task)

        def adopt(completed):
            if not completed.cancelled() and completed.exception() is None:
                self._jobs.add(completed.result())
                self._owned_groups.add(completed.result().pid)
            self._spawning.discard(completed)

        task.add_done_callback(adopt)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Creation can finish after cancellation. Adopt that exact child
            # before returning so it cannot escape the session's cleanup.
            try:
                process = await asyncio.shield(task)
                await self._stop_process(process)
                self._jobs.discard(process)
            except Exception:
                pass
            raise

    async def _command(self, args, *, timeout=20, payload=None):
        process = None
        try:
            process = await self._spawn(args, stdin=asyncio.subprocess.PIPE if payload else asyncio.subprocess.DEVNULL,
                                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)

            async def finish():
                if payload:
                    process.stdin.write(payload)
                    await process.stdin.drain()
                    process.stdin.close()
                chunks, total = [], 0
                while True:
                    block = await process.stdout.read(min(65536, _MAX_COMMAND_OUTPUT - total + 1))
                    if not block:
                        break
                    total += len(block)
                    if total > _MAX_COMMAND_OUTPUT:
                        raise StreamError("A media command returned excessive data")
                    chunks.append(block)
                if await process.wait() != 0:
                    raise StreamError("A media command failed; no source quality fallback was attempted")
                return b"".join(chunks)

            return await asyncio.wait_for(finish(), timeout)
        except asyncio.CancelledError:
            raise
        except StreamError:
            raise
        except Exception:
            raise StreamError("A media command failed or timed out") from None
        finally:
            if process is not None:
                await self._stop_process(process)
                self._jobs.discard(process)

    async def _json_command(self, args, **kwargs):
        output = await self._command(args, **kwargs)
        try:
            return json.loads(output)
        except (ValueError, TypeError):
            raise StreamError("A media probe returned invalid data") from None

    async def _capabilities(self):
        kinds = ("protocols", "muxers", "encoders", "decoders", "filters")
        outputs = await _gather(*(self._command([self.ffmpeg, "-hide_banner", "-" + kind]) for kind in kinds))
        result = {kind: parse_capabilities(output.decode("utf-8", "replace"), kind) for kind, output in zip(kinds, outputs)}
        if not {"https", "tls", "tcp", "file"} <= result["protocols"]:
            raise StreamError("FFmpeg lacks the required HTTPS input protocols")
        if not {"hls", "mp4"} <= result["muxers"]:
            raise StreamError("FFmpeg lacks HLS and fragmented MP4 output support")
        probe_protocols = parse_capabilities((await self._command([self.ffprobe, "-hide_banner", "-protocols"])).decode(), "protocols")
        if not {"https", "tls", "tcp", "file"} <= probe_protocols:
            raise StreamError("FFprobe lacks the required HTTPS input protocols")
        return result

    async def _probe(self, source, *, kind=None, local=False):
        args = [self.ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe" if local else "https,tls,tcp,crypto"]
        if not local:
            _cdn_url(source)
            args += ["-rw_timeout", "10000000"]
        if kind:
            args += ["-select_streams", "v:0" if kind == "video" else "a:0", "-read_intervals", "%+#1",
                     "-show_entries", "packet=pts_time:packet_side_data=side_data_type,skip_samples:stream=codec_type,sample_rate"]
        else:
            args += ["-show_entries", _PROBE_FIELDS]
        return await self._json_command(args + ["-of", "json", str(source)], timeout=25)

    async def _initial_pts(self, source, kind, *, local=False):
        result = await self._probe(source, kind=kind, local=local)
        try:
            packet, = result["packets"]
            initial = Fraction(packet["pts_time"])
            if kind == "audio":
                rate = int(_stream(result, "audio")["sample_rate"])
                if not 8000 <= rate <= 192000:
                    raise ValueError
                for extra in packet.get("side_data_list", []):
                    if extra.get("side_data_type") == "Skip Samples":
                        skip = int(extra["skip_samples"])
                        if not 0 <= skip <= rate:
                            raise ValueError
                        initial += Fraction(skip, rate)
            if not -10 <= initial < 14400:
                raise ValueError
            return initial
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            raise StreamError("The initial presentation timestamp could not be verified") from None

    async def _verify_encoder(self, capabilities):
        if not self.plan["copy_audio"] and "aac" not in capabilities["encoders"]:
            raise StreamError("The required AAC audio encoder is unavailable")
        missing = set(self.plan["required_codecs"]["decoders"]) - capabilities["decoders"]
        if missing:
            raise StreamError("A required source decoder is unavailable; source quality was not reduced")
        self.encoder = choose_encoder(self.plan, capabilities["encoders"], requested=self.requested_encoder,
                                      vaapi_device=self.vaapi_device, allow_gpl=self.allow_gpl)
        if self.encoder is None:
            return
        if "color" not in capabilities["filters"]:
            raise StreamError("The selected encoder cannot be verified because the color source filter is unavailable")
        target = self.plan["target"]
        args = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        args += hardware_arguments(self.encoder, self.vaapi_device)
        args += ["-f", "lavfi", "-i", f"color=c=black:s={target['width']}x{target['height']}:r={target['fps']}",
                 "-frames:v", "3", "-an"] + video_arguments(self.plan, self.encoder) + ["-f", "null", "-"]
        try:
            await self._command(args, timeout=20)
        except StreamError:
            raise StreamError("The selected encoder could not encode the requested profile, dimensions and frame rate") from None

    async def start(self, url, receiver_model, *, max_resolution=None):
        if self._state != "new":
            raise StreamError("The native HLS session can only start once")
        url = youtube_url(url)
        self._state = "starting"
        self._started = time.monotonic()
        self._startup_task = asyncio.create_task(self._start(url, receiver_model, max_resolution))
        try:
            await asyncio.wait_for(asyncio.shield(self._startup_task), self.startup_timeout)
            return self
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as error:
            if isinstance(error, (StreamError, PrepareError, PlanError)):
                self._error = str(error)
            else:
                self._error = "Native HLS startup failed or timed out"
            await self.close()
            raise StreamError(self._error) from None

    async def _start(self, url, receiver_model, max_resolution):
        metadata, capabilities = await _gather(
            self._json_command([sys.executable, "-m", "service.native_stream", "--resolve-worker"],
                               timeout=45, payload=url.encode()), self._capabilities())
        self.plan = plan_direct_media(metadata, receiver_model, max_resolution=max_resolution)
        try:
            duration = float(metadata.get("duration", 0))
            # This is an all-in lifetime, including worst-case startup and the
            # promised final drain. Do not silently increase the four-hour cap.
            if not math.isfinite(duration) or not 0 < duration <= self.session_timeout - self.startup_timeout - 120:
                raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise StreamError("The complete video must fit within the bounded native HLS session") from None
        self.expected_duration = duration
        rows = {row["format_id"]: row for row in metadata["formats"] if isinstance(row, dict) and isinstance(row.get("format_id"), str)}
        video = _cdn_url(rows[self.plan["video"]["format_id"]]["url"])
        audio = _cdn_url(rows[self.plan["audio"]["format_id"]]["url"])
        video_probe, audio_probe, video_pts, audio_pts = await _gather(
            self._probe(video), self._probe(audio), self._initial_pts(video, "video"), self._initial_pts(audio, "audio"))
        self._source_rate = validate_tracks(self.plan, video_probe, audio_probe)
        validate_source_durations(duration, video_probe, audio_probe)
        self._source_offset = validate_initial_timing(video_pts, audio_pts)
        await self._verify_encoder(capabilities)
        self.directory = Path(tempfile.mkdtemp(prefix="native-stream-", dir=self.root))
        self.process = await self._spawn(
            hls_arguments(self.ffmpeg, video, audio, self.directory, self.plan, encoder=self.encoder, vaapi_device=self.vaapi_device),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self._monitor_task = asyncio.create_task(self._monitor())
        await self._ready.wait()
        if self._error:
            raise StreamError(self._error)

    async def _verify_first_segment(self, name):
        path = self.directory / "probe.unverified.mp4"
        try:
            needed = (self.directory / "init.mp4").stat().st_size + (self.directory / name).stat().st_size
            if self._bytes + needed > self.disk_limit:
                raise StreamError("The native HLS session cannot verify its first segment within the disk limit")
            with path.open("xb") as output:
                for source in (self.directory / "init.mp4", self.directory / name):
                    with source.open("rb") as input_file:
                        shutil.copyfileobj(input_file, output, length=65536)
            result, video_pts, audio_pts = await _gather(
                self._probe(path, local=True), self._initial_pts(path, "video", local=True), self._initial_pts(path, "audio", local=True))
            duration = float(_stream(result, "video")["duration"])
            validate_tracks(self.plan, result, result, output=True, seconds=duration, source_rate=self._source_rate)
            validate_initial_timing(video_pts, audio_pts, source_offset=self._source_offset)
        finally:
            path.unlink(missing_ok=True)

    async def _monitor(self):
        completed_at = None
        try:
            while True:
                self._bytes = 0
                for path in self.directory.iterdir():
                    # Rolling HLS legitimately deletes completed segments
                    # while the monitor is enumerating the directory.
                    try:
                        if path.is_file() and not path.is_symlink():
                            self._bytes += path.stat().st_size
                    except FileNotFoundError:
                        pass
                # Check twice per second; FFmpeg may exceed the cap by bytes
                # produced within one interval. No claim of filesystem quota.
                if self._bytes > self.disk_limit:
                    raise StreamError("The native HLS session reached its disk limit")
                elapsed = time.monotonic() - self._started
                if elapsed > self.session_timeout - 120 and completed_at is None:
                    raise StreamError("Native HLS generation reached its time limit before the reserved final drain")
                names = playlist_segments(self.directory)
                if names:
                    for number, duration in playlist_durations(self.directory):
                        if number > self._last_segment:
                            if number != self._last_segment + 1:
                                raise StreamError("The generated HLS media sequence skipped a segment")
                            self._prepared_duration += duration
                            self._last_segment = number
                if names and not self._ready.is_set():
                    if self._verification_task is None:
                        self._verification_task = asyncio.create_task(self._verify_first_segment(names[0]))
                    if self._verification_task.done():
                        self._verification_task.result()
                        self._state = "streaming"
                        self._ready.set()
                if self.process.returncode is not None:
                    if self.process.returncode != 0:
                        raise StreamError("Native HLS conversion stopped before completion")
                    if not names or "#EXT-X-ENDLIST" not in self.playlist.read_text():
                        raise StreamError("Native HLS conversion ended without a complete playlist")
                    if self.expected_duration is not None and abs(float(self._prepared_duration) - self.expected_duration) > 1:
                        raise StreamError("Native HLS output did not cover the complete resolved video duration")
                    if self._ready.is_set():
                        self._state = "finished"
                        self._finished.set()
                    completed_at = completed_at or time.monotonic()
                    # Keep final segments available while the receiver drains
                    # its buffer, then release this generated session only.
                    if time.monotonic() - completed_at > 120:
                        asyncio.create_task(self.close())
                        return
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._error = str(error) if isinstance(error, (StreamError, PrepareError)) else "Native HLS monitoring failed"
            self._state = "failed"
            self._ready.set()
            self._finished.set()
            await self._stop_process(self.process)
            asyncio.create_task(self.close())

    async def wait_finished(self):
        await self._finished.wait()
        if self._error:
            raise StreamError(self._error)

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._cleanup())
        await asyncio.shield(self._close_task)

    async def _cleanup(self):
        try:
            tasks = [task for task in (self._startup_task, self._monitor_task, self._verification_task) if task is not None]
            for task in tasks:
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*tuple(self._spawning), return_exceptions=True)
            await asyncio.gather(*(self._stop_process(process) for process in tuple(self._jobs)), return_exceptions=True)
            self._jobs.clear()
            if self.process is not None:
                await self._stop_process(self.process)
            if self.directory is not None:
                shutil.rmtree(self.directory)
        finally:
            self._state = "failed" if self._error else "closed"
            self._ready.set()
            self._finished.set()
            self.closed.set()


def _worker_main():
    """Private child-process transport; never call this as a user CLI."""
    import contextlib
    try:
        url = youtube_url(sys.stdin.buffer.read(4097).decode())
        with open(os.devnull, "w") as quiet, contextlib.redirect_stdout(quiet), contextlib.redirect_stderr(quiet):
            metadata = _extract_metadata(url)
        output = json.dumps(metadata).encode()
        if len(output) > _MAX_COMMAND_OUTPUT:
            return 1
        sys.stdout.buffer.write(output)
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    sys.exit(_worker_main() if sys.argv[1:] == ["--resolve-worker"] else 2)
