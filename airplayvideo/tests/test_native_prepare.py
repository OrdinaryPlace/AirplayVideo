import copy
import json
from fractions import Fraction
from pathlib import Path
import subprocess

import pytest

from service import native_prepare as prepare
from service.native_plan import plan_media


def metadata(*, four_k=False):
    return {"duration": 120, "formats": [
        {"format_id": "315" if four_k else "299", "vcodec": "vp9" if four_k else "avc1.64002a",
         "acodec": "none", "width": 3840 if four_k else 1920, "height": 2160 if four_k else 1080,
         "fps": 60, "dynamic_range": "SDR", "url": "https://r1.googlevideo.com/videoplayback?secret=video",
         "protocol": "https"},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "audio_channels": 2,
         "asr": 44100, "abr": 128, "url": "https://r1.googlevideo.com/videoplayback?secret=audio",
         "protocol": "https"}]}


def probe(*, four_k=False, output=False):
    return {"streams": [
        {"codec_type": "video", "codec_name": ("hevc" if output else "vp9") if four_k else "h264",
         "profile": "Main" if four_k else "High", "level": 153 if four_k else 42,
         "pix_fmt": "yuv420p", "width": 3840 if four_k else 1920, "height": 2160 if four_k else 1080,
         "avg_frame_rate": "60/1", "duration": "20.016667", "color_transfer": "bt709"},
        {"codec_type": "audio", "codec_name": "aac", "profile": "LC", "channels": 2,
         "sample_rate": "44100", "duration": "20.016667"}]}


@pytest.mark.parametrize("url", ["https://youtu.be/aqz-KE-bpKQ?t=30", "https://www.youtube.com/watch?v=aqz-KE-bpKQ&list=ignored",
                                  "https://m.youtube.com/shorts/aqz-KE-bpKQ", "https://youtube.com/embed/aqz-KE-bpKQ"])
def test_only_explicit_video_url_is_canonicalized(url):
    assert prepare.youtube_url(url) == "https://www.youtube.com/watch?v=aqz-KE-bpKQ"


@pytest.mark.parametrize("url", ["http://youtube.com/watch?v=aqz-KE-bpKQ", "https://youtube.com/playlist?list=secret",
                                  "https://youtube.com.evil/watch?v=aqz-KE-bpKQ", "https://secret@youtube.com/watch?v=aqz-KE-bpKQ",
                                  "https://youtube.com:123/watch?v=aqz-KE-bpKQ", "file:///secret", "https://youtu.be/secret"])
def test_other_sites_playlists_and_embedded_credentials_are_rejected_without_echo(url):
    with pytest.raises(prepare.PrepareError) as error:
        prepare.youtube_url(url)
    assert "secret" not in str(error.value) and url not in str(error.value)


def test_resolver_does_not_read_accounts_cache_or_download_and_discards_unneeded_metadata():
    options_seen = {}

    class Resolver:
        def __init__(self, options):
            options_seen.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def extract_info(self, url, download):
            assert download is False
            return {**metadata(), "availability": "public", "title": "private-title",
                    "http_headers": {"Cookie": "private-cookie"}}

    result = prepare._extract_metadata("https://www.youtube.com/watch?v=aqz-KE-bpKQ", Resolver)
    assert options_seen["cookiesfrombrowser"] is None and options_seen["cookiefile"] is None
    assert options_seen["usenetrc"] is False and options_seen["cachedir"] is False
    assert options_seen["username"] is None and options_seen["password"] is None
    assert "private-title" not in json.dumps(result) and "private-cookie" not in json.dumps(result)


@pytest.mark.parametrize("url", ["http://r1.googlevideo.com/videoplayback", "https://googlevideo.com.evil/videoplayback",
                                  "https://127.0.0.1/videoplayback", "https://secret@r1.googlevideo.com/videoplayback",
                                  "https://r1.googlevideo.com/other", "https://r1.googlevideo.com:123/videoplayback"])
def test_direct_source_and_redirects_cannot_escape_https_media_cdn(url):
    with pytest.raises(prepare.PrepareError):
        prepare._cdn_url(url)
    with pytest.raises(prepare.PrepareError):
        prepare._CDNRedirect().redirect_request(None, None, 302, "", {}, url)


@pytest.mark.parametrize("four_k", [False, True])
def test_commands_preserve_dimensions_rate_and_independent_audio_copy(four_k):
    plan = plan_media(metadata(four_k=four_k), "AppleTV6,2")
    args = prepare.ffmpeg_arguments("/opt/ffmpeg", "video.source", "audio.source", "sample.mp4", plan, 20)
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[args.index("-c:v") + 1] == ("libx265" if four_k else "copy")
    assert "-r" not in args and "-s" not in args and "-vf" not in args
    assert args[args.index("-fps_mode") + 1] == "passthrough"
    assert args[args.index("-t") + 1] == "20" and "-fs" in args and "-n" in args
    assert not any("https:" in argument for argument in args)
    if four_k:
        assert args[args.index("-tag:v") + 1] == "hvc1"


def test_opus_converts_audio_while_compatible_video_copies():
    data = metadata()
    data["formats"][1]["acodec"] = "opus"
    plan = plan_media(data, "AppleTV6,2")
    args = prepare.ffmpeg_arguments("/opt/ffmpeg", "video", "audio", "out", plan, 20)
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[args.index("-profile:a") + 1] == "aac_low"


def test_direct_preparation_selects_same_quality_https_instead_of_hls():
    data = metadata()
    data["formats"].append({**data["formats"][0], "format_id": "hls", "protocol": "m3u8_native", "vbr": 99999})
    assert plan_media(data, "AppleTV6,2")["video"]["format_id"] == "hls"
    plan = prepare.plan_direct_media(data, "AppleTV6,2")
    assert plan["video"]["format_id"] == "299" and plan["copy_video"] and plan["copy_audio"]
    assert (plan["target"]["width"], plan["target"]["height"], plan["target"]["fps"]) == (1920, 1080, "60/1")


def test_direct_preparation_never_silently_lowers_quality_to_avoid_hls():
    data = metadata()
    data["formats"].append({**data["formats"][0], "format_id": "hls4k", "protocol": "m3u8_native",
                            "width": 3840, "height": 2160, "vcodec": "vp9"})
    with pytest.raises(prepare.PrepareError, match="highest-quality"):
        prepare.plan_direct_media(data, "AppleTV6,2")


@pytest.mark.parametrize("four_k", [False, True])
def test_actual_source_and_output_must_match_plan(four_k):
    plan = plan_media(metadata(four_k=four_k), "AppleTV6,2")
    source = probe(four_k=four_k)
    assert prepare.validate_tracks(plan, source, source) == Fraction(60)
    output = probe(four_k=four_k, output=True)
    assert prepare.validate_tracks(plan, output, output, output=True, seconds=20, source_rate=Fraction(60)) == 60


@pytest.mark.parametrize("track,changes", [
    (0, {"width": 1280}), (0, {"avg_frame_rate": "30/1"}), (0, {"avg_frame_rate": "0/0"}),
    (0, {"codec_name": "vp9"}), (0, {"pix_fmt": "yuv444p"}), (0, {"profile": "High 10"}),
    (0, {"level": 60}), (0, {"color_transfer": "smpte2084"}), (0, {"duration": "10"}),
    (1, {"codec_name": "opus"}), (1, {"profile": "HE-AAC"}), (1, {"channels": 6}),
    (1, {"sample_rate": "96000"}), (1, {"duration": "10"}), (1, {"duration": "nan"})])
def test_wrong_quality_color_codec_layout_or_truncated_audio_never_becomes_ready(track, changes):
    plan = plan_media(metadata(), "AppleTV6,2")
    output = probe()
    output["streams"][track].update(changes)
    with pytest.raises(prepare.PrepareError):
        prepare.validate_tracks(plan, output, output, output=True, seconds=20, source_rate=Fraction(60))


def test_rounded_extractor_fps_can_match_probe_but_output_must_preserve_actual_rate():
    data = metadata()
    data["formats"][0]["fps"] = 59.94
    plan = plan_media(data, "AppleTV6,2")
    source = probe()
    source["streams"][0]["avg_frame_rate"] = "60000/1001"
    rate = prepare.validate_tracks(plan, source, source)
    assert rate == Fraction(60000, 1001)
    changed = copy.deepcopy(source)
    changed["streams"][0]["avg_frame_rate"] = "2997/50"
    with pytest.raises(prepare.PrepareError, match="preserve"):
        prepare.validate_tracks(plan, changed, changed, output=True, seconds=20, source_rate=rate)


def test_process_failure_never_exposes_command_urls_or_stderr(monkeypatch):
    def fail(args, **kwargs):
        assert kwargs["shell"] is False and kwargs["stderr"] == subprocess.DEVNULL
        assert kwargs["timeout"] == 20
        raise subprocess.CalledProcessError(1, args, stderr=b"private-url-secret")

    monkeypatch.setattr(prepare.subprocess, "run", fail)
    with pytest.raises(prepare.PrepareError) as error:
        prepare._run(["ffmpeg", "private-url-secret"], timeout=20)
    assert "secret" not in str(error.value)


def test_prefix_download_stops_at_limit_even_if_server_ignores_range(tmp_path, monkeypatch):
    class Response:
        status = 200
        headers = {}
        count = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            self.count += size
            return b"x" * size

    response = Response()

    class Opener:
        def open(self, request, timeout):
            assert timeout == 10
            assert request.headers["Range"] == "bytes=0-8388607"
            assert not any("cookie" in key.lower() for key in request.headers)
            return response

    monkeypatch.setattr(prepare, "build_opener", lambda *args: Opener())
    path = tmp_path / "audio.source"
    count = prepare.download_prefix(metadata()["formats"][1], path, audio=True)
    assert path.stat().st_size == count == response.count == 8 * 1024 * 1024


def test_tiny_clip_uses_bitrate_estimate_and_headroom_instead_of_maximum_prefix():
    video = {**metadata()["formats"][0], "vbr": 4000}
    assert 4 * 1024 * 1024 <= prepare.prefix_byte_limit(video, 2, 120) < 8 * 1024 * 1024
    assert prepare.prefix_byte_limit(metadata()["formats"][1], 2, 120, audio=True) == 1024 * 1024
    assert prepare.prefix_byte_limit({**video, "vbr": 1000000}, 30, 120) == 128 * 1024 * 1024


def test_prefix_download_rejects_an_incorrect_range_start(tmp_path, monkeypatch):
    class Response:
        status = 206
        headers = {"Content-Range": "bytes 1024-2047/4096"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            pytest.fail("Incorrect range must be rejected before reading")

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(prepare, "build_opener", lambda *args: Opener())
    with pytest.raises(prepare.PrepareError, match="range"):
        prepare.download_prefix(metadata()["formats"][0], tmp_path / "source")


def test_prefix_download_deadline_is_enforced_between_bounded_reads(tmp_path, monkeypatch):
    class Response:
        status = 200
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, size):
            pytest.fail("Expired deadline must prevent the next read")

    class Opener:
        def open(self, *args, **kwargs):
            return Response()

    ticks = iter([100, 161])
    monkeypatch.setattr(prepare.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(prepare, "build_opener", lambda *args: Opener())
    with pytest.raises(prepare.PrepareError, match="timed out"):
        prepare.download_prefix(metadata()["formats"][0], tmp_path / "source")


def test_end_to_end_mock_keeps_existing_files_and_reports_ready_only_after_probe(tmp_path, monkeypatch):
    existing = tmp_path / "sample.mp4"
    existing.write_bytes(b"existing recording")
    monkeypatch.setattr(prepare, "_binary", lambda value: value)
    monkeypatch.setattr(prepare, "resolve_metadata", lambda url: metadata(four_k=True))
    monkeypatch.setattr(prepare, "download_prefix", lambda row, path, **kw: Path(path).write_bytes(b"source"))
    monkeypatch.setattr(prepare, "probe_media", lambda binary, path: probe(four_k=True, output="unverified" in path.name))
    monkeypatch.setattr(prepare, "probe_initial_pts", lambda *args: Fraction(0))
    calls = []

    def run(args, *, timeout):
        calls.append(args)
        assert timeout == 300
        Path(args[-1]).write_bytes(b"prepared")

    monkeypatch.setattr(prepare, "_run", run)
    path, report = prepare.prepare_media("https://youtu.be/aqz-KE-bpKQ", "AppleTV6,2", 20, tmp_path, "ffmpeg", "ffprobe")
    assert path.parent != tmp_path and path.name == "sample.mp4"
    assert path.read_bytes() == b"prepared" and existing.read_bytes() == b"existing recording"
    assert report["ready"] and report["verified"]["height"] == 2160 and report["verified"]["fps"] == "60"
    assert not report["receiver_playback_verified"]
    assert report == json.loads((path.parent / "report.json").read_text())
    assert "secret" not in json.dumps(report) and "googlevideo" not in json.dumps(report)


@pytest.mark.parametrize("seconds", [0, 31, True, 1.5])
def test_invalid_duration_fails_before_any_resolver_or_process_work(seconds, monkeypatch):
    monkeypatch.setattr(prepare, "resolve_metadata", lambda url: pytest.fail("resolver must not run"))
    with pytest.raises(prepare.PrepareError, match="duration"):
        prepare.prepare_media("https://youtu.be/aqz-KE-bpKQ", "AppleTV6,2", seconds, "/tmp", "ffmpeg", "ffprobe")


def test_probe_failure_keeps_unverified_file_and_never_publishes_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(prepare, "_binary", lambda value: value)
    monkeypatch.setattr(prepare, "resolve_metadata", lambda url: metadata())
    monkeypatch.setattr(prepare, "download_prefix", lambda row, path, **kw: Path(path).write_bytes(b"source"))
    monkeypatch.setattr(prepare, "_run", lambda args, **kw: Path(args[-1]).write_bytes(b"unverified"))
    monkeypatch.setattr(prepare, "probe_initial_pts", lambda *args: Fraction(0))

    def fake_probe(binary, path):
        result = probe()
        if "unverified" in path.name:
            result["streams"][1]["duration"] = "5"
        return result

    monkeypatch.setattr(prepare, "probe_media", fake_probe)
    with pytest.raises(prepare.PrepareError, match="duration"):
        prepare.prepare_media("https://youtu.be/aqz-KE-bpKQ", "AppleTV6,2", 20, tmp_path, "ffmpeg", "ffprobe")
    assert not list(tmp_path.glob("*/sample.mp4")) and not list(tmp_path.glob("*/report.json"))
    assert len(list(tmp_path.glob("*/sample.unverified.mp4"))) == 1


def test_initial_presentation_time_accounts_for_aac_encoder_priming(monkeypatch):
    result = {"packets": [{"pts_time": "-0.021333", "side_data_list": [
        {"side_data_type": "Skip Samples", "skip_samples": 1024}]}],
        "streams": [{"codec_type": "audio", "sample_rate": "48000"}]}
    monkeypatch.setattr(prepare, "_run", lambda *args, **kwargs: result)
    assert abs(prepare.probe_initial_pts("ffprobe", Path("sample"), "audio")) < Fraction(1, 1000000)


@pytest.mark.parametrize("packet", [{}, {"pts_time": "nan"}, {"pts_time": "999999"}])
def test_missing_or_invalid_initial_timestamp_fails_explicitly(packet, monkeypatch):
    monkeypatch.setattr(prepare, "_run", lambda *args, **kwargs: {"packets": [packet]})
    with pytest.raises(prepare.PrepareError, match="timestamp"):
        prepare.probe_initial_pts("ffprobe", Path("sample"), "video")


def test_source_offset_is_rejected_before_independent_input_rebasing():
    with pytest.raises(prepare.PrepareError, match="different presentation"):
        prepare.validate_initial_timing(Fraction(0), Fraction(1, 10))
    # A common nonzero start can be rebased without changing A/V alignment.
    assert prepare.validate_initial_timing(Fraction(5), Fraction(5)) == 0


def test_output_must_retain_relative_presentation_timestamp():
    with pytest.raises(prepare.PrepareError, match="changed the relative"):
        prepare.validate_initial_timing(Fraction(0), Fraction(1, 20), source_offset=Fraction(0))
    assert prepare.validate_initial_timing(Fraction(0), Fraction(0), source_offset=Fraction(0)) == 0
