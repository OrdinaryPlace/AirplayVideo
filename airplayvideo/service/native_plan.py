"""Pure, conservative planning for the opt-in native YouTube experiment.

This module neither resolves media nor contacts a receiver. Model specifications
are ceilings, not evidence that a connected display or the native delivery path
has been verified. Plans intentionally contain no URLs, headers, titles or raw
extractor metadata. A subsequent media probe must confirm the selected tracks.
"""
from __future__ import annotations

from fractions import Fraction
import math
import re


class PlanError(ValueError):
    """A fixed, safe explanation suitable for an experimental plan report."""


# Apple technical specifications, checked 2026-09-26:
# https://support.apple.com/en-us/111928 (HD)
# https://support.apple.com/en-us/111929 (4K 1st generation)
# https://support.apple.com/en-us/111922 (4K 2nd generation)
# https://support.apple.com/en-us/111839 (4K 3rd generation)
# First-generation HDR frame rate is not specified: use a conservative 30 fps
# planning ceiling rather than implying the documented SDR 60 fps applies.
_MODELS = {
    "AppleTV5,3": (1920, 1080, 30, 0, (), "111928"),
    "AppleTV6,2": (3840, 2160, 60, 30, ("hdr10", "dolby_vision_5"), "111929"),
    "AppleTV11,1": (3840, 2160, 60, 60, ("hdr10", "hlg", "dolby_vision_5"), "111922"),
    "AppleTV14,1": (3840, 2160, 60, 60, ("hdr10", "hdr10+", "hlg", "dolby_vision_5"), "111839"),
}
_FORMAT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")


def receiver_capabilities(model):
    """Return published model limits; unknown models are never guessed."""
    if not isinstance(model, str) or model not in _MODELS:
        raise PlanError("Receiver model is unknown; native playback capability is unverified")
    long_edge, short_edge, hevc_fps, hdr_fps, hdr_formats, page = _MODELS[model]
    return {"model": model, "long_edge": long_edge, "short_edge": short_edge,
            "max_fps": 60, "h264_max_fps": 60, "hevc_max_fps": hevc_fps,
            "hdr_max_fps": hdr_fps, "hdr_formats": list(hdr_formats),
            "basis": "published_model_specifications", "receiver_verified": False,
            "reference": "https://support.apple.com/en-us/" + page}


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Integer metadata can be arbitrarily large; converting it to a float just
    # to call isfinite can itself overflow before the ordinary bounds checks.
    return value if value >= 0 and (isinstance(value, int) or math.isfinite(value)) else None


def _integer(value, low, high):
    value = _number(value)
    return int(value) if value is not None and low <= value <= high and value == int(value) else None


def _fps(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    if isinstance(value, int) and not 0 < value <= 240:
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    value = str(value)
    if len(value) > 32 or not re.fullmatch(r"\d+(?:\.\d+|/\d+)?", value):
        return None
    try:
        result = Fraction(value)
    except (ValueError, ZeroDivisionError, OverflowError):
        return None
    return result if 0 < result <= 240 and result.denominator <= 1000000 else None


def _rational(value):
    return f"{value.numerator}/{value.denominator}"


def _codec(value, audio=False):
    if not isinstance(value, str) or len(value) > 100:
        return None
    value = value.lower()
    if audio:
        if value == "aac" or re.fullmatch(r"mp4a\.40\.\d+", value):
            return "aac"
        return value if value in {"opus", "vorbis", "mp3", "ac3", "eac3", "flac"} else None
    if value == "h264" or re.fullmatch(r"avc[13](?:\.[0-9a-f]{6})?", value):
        return "h264"
    if value in {"hevc", "h265"} or re.fullmatch(r"(?:hvc1|hev1)(?:\.[a-z0-9]+)+", value):
        return "hevc"
    if value == "vp9" or re.fullmatch(r"vp09(?:\.\d+)+", value):
        return "vp9"
    if value == "av1" or re.fullmatch(r"av01(?:\.[a-z0-9]+)+", value):
        return "av1"
    return None


def _dynamic_range(row):
    value = row.get("dynamic_range")
    if not isinstance(value, str):
        return "unknown"
    return {"SDR": "sdr", "HDR10": "hdr10", "HDR10+": "hdr10+", "HLG": "hlg",
            "DV": "dolby_vision", "DOLBY VISION": "dolby_vision"}.get(value.upper(), "unknown")


def _copy_video(row, codec, fps, capabilities):
    # Codec names alone omit profile, level and pixel depth. Such tracks remain
    # eligible for conversion, but must not be declared safe to stream-copy.
    tag = row.get("vcodec", "").lower()
    pixel = row.get("pix_fmt")
    if codec == "h264":
        match = re.fullmatch(r"avc[13]\.([0-9a-f]{2})[0-9a-f]{2}([0-9a-f]{2})", tag)
        if not match or pixel not in (None, "yuv420p", "yuvj420p"):
            return False
        profile, level = (int(part, 16) for part in match.groups())
        maximum = 42 if capabilities["short_edge"] == 1080 else 52
        # Baseline above level 3.0 is outside Apple's explicit published entry.
        return profile in (66, 77, 100) and 0 < level <= (30 if profile == 66 else maximum)
    if codec == "hevc":
        match = re.fullmatch(r"(?:hvc1|hev1)\.([12])\.[0-9a-f]+\.l(\d+)(?:\.[0-9a-f]+)*", tag)
        if not match or pixel not in (None, "yuv420p", "yuv420p10le"):
            return False
        maximum = 123 if capabilities["short_edge"] == 1080 else 153
        return fps <= capabilities["hevc_max_fps"] and 0 < int(match.group(2)) <= maximum
    return False


def _copy_audio(row, codec):
    # AAC-LC, known mono/stereo layout and ordinary sample rates are the narrow
    # initial MP4 compatibility contract. Other profiles/layouts are converted.
    return (codec == "aac" and row.get("acodec", "").lower() == "mp4a.40.2"
            and _integer(row.get("audio_channels"), 1, 2) is not None
            and _integer(row.get("asr"), 8000, 48000) is not None
            and (_number(row.get("abr")) is None or _number(row["abr"]) <= 320))


def plan_media(metadata, receiver_model, *, max_resolution=None, max_fps=None):
    """Plan VOD tracks from yt-dlp-style metadata without fetching anything.

    Resolution is an optional short-edge integer or string such as ``1080p``;
    its 16:9 bounding box is compared by long/short edge, including portrait and
    ultrawide media. Frame rates are retained as exact rational strings. Decimal
    metadata stays decimal precision (59.94 becomes 2997/50, not guessed NTSC).
    Variants beyond the ceiling are excluded, never silently resized or retimed.
    """
    capabilities = receiver_capabilities(receiver_model)
    long_limit, short_limit = capabilities["long_edge"], capabilities["short_edge"]
    fps_limit = Fraction(capabilities["max_fps"])
    if max_resolution is not None:
        if isinstance(max_resolution, str) and re.fullmatch(r"\d{3,4}p", max_resolution):
            max_resolution = int(max_resolution[:-1])
        resolution = _integer(max_resolution, 144, 4320)
        if resolution is None:
            raise PlanError("Maximum resolution must be a short-edge size such as 1080p")
        short_limit = min(short_limit, resolution)
        long_limit = min(long_limit, resolution * 16 // 9)
    if max_fps is not None:
        requested_fps = _fps(max_fps)
        if requested_fps is None:
            raise PlanError("Maximum frame rate is invalid")
        fps_limit = min(fps_limit, requested_fps)
    if not isinstance(metadata, dict):
        raise PlanError("Media metadata is invalid")
    if metadata.get("has_drm") not in (None, False):
        raise PlanError("DRM-protected media is not supported")
    if metadata.get("is_live") or metadata.get("live_status") not in (None, "not_live", "was_live"):
        raise PlanError("Native playback planning currently supports completed videos only")
    rows = metadata.get("formats")
    if not isinstance(rows, list) or not 0 < len(rows) <= 10000:
        raise PlanError("Media metadata contains no usable formats")

    videos, audios, seen = [], [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        format_id = row.get("format_id")
        if not isinstance(format_id, str) or not _FORMAT_ID.fullmatch(format_id):
            continue
        if format_id in seen:
            raise PlanError("Media format identifiers are ambiguous")
        seen.add(format_id)
        if row.get("has_drm") not in (None, False):
            continue
        video_codec, audio_codec = _codec(row.get("vcodec")), _codec(row.get("acodec"), audio=True)
        if video_codec:
            width, height = _integer(row.get("width"), 2, 16384), _integer(row.get("height"), 2, 16384)
            fps = _fps(row.get("fps"))
            if (width and height and fps and max(width, height) <= long_limit
                    and min(width, height) <= short_limit and fps <= fps_limit):
                copy = _copy_video(row, video_codec, fps, capabilities)
                bitrate = _number(row.get("vbr")) or _number(row.get("tbr")) or 0
                # Preserve resolution and frame rate first, then avoid an
                # unnecessary generation of loss. Bitrates across different
                # codecs do not measure comparable visual quality.
                rank = (width * height, fps, copy, bitrate, format_id)
                videos.append((rank, row, video_codec, width, height, fps, copy))
        if audio_codec:
            copy = _copy_audio(row, audio_codec)
            bitrate = _number(row.get("abr")) or 0
            audios.append(((copy, bitrate, format_id), row, audio_codec, copy))
    if not videos:
        raise PlanError("No supported video format fits the receiver and requested quality limits")
    if not audios:
        raise PlanError("No supported audio track is available")

    _, video_row, video_codec, width, height, fps, copy_video = max(videos, key=lambda item: item[0])
    _, audio_row, audio_codec, copy_audio = max(audios, key=lambda item: item[0])
    dynamic_range = _dynamic_range(video_row)
    if dynamic_range != "sdr":
        # A model's decoder capability does not prove HDR metadata survives the
        # experimental serving/muxing path or that its connected screen is HDR.
        raise PlanError("The best-quality video is HDR or has unknown color range; HDR preservation is not yet verified")
    target_video = video_codec if copy_video else ("hevc" if max(width, height) > 1920 or min(width, height) > 1080 else "h264")
    reasons = ["Selected the highest-resolution source within the receiver and requested limits; retained its frame rate"]
    reasons.append("Video can be copied after probe verification" if copy_video else
                   "Video requires conversion to a receiver-compatible codec/profile at the selected source dimensions and frame rate")
    reasons.append("Compatible AAC-LC audio can be copied independently" if copy_audio else
                   "Audio requires conversion to AAC-LC stereo at up to 48 kHz")
    return {
        "supported": True, "experimental": True, "receiver": capabilities,
        "limits": {"long_edge": long_limit, "short_edge": short_limit, "fps": _rational(fps_limit)},
        "video": {"format_id": video_row["format_id"], "codec": video_codec, "width": width,
                  "height": height, "fps": _rational(fps), "hdr": dynamic_range},
        "audio": {"format_id": audio_row["format_id"], "codec": audio_codec},
        "copy_video": copy_video, "copy_audio": copy_audio,
        "target": {"container": "mp4", "video_codec": target_video, "audio_codec": "aac",
                   "width": width, "height": height, "fps": _rational(fps), "hdr": "sdr"},
        "required_codecs": {"decoders": sorted(set(([video_codec] if not copy_video else []) +
                                                    ([audio_codec] if not copy_audio else []))),
                            "encoders": sorted(set(([target_video] if not copy_video else []) +
                                                    (["aac"] if not copy_audio else [])))},
        "requires_probe_verification": True, "reasons": reasons,
    }
