import copy
import json

import pytest

from service.native_plan import PlanError, plan_media, receiver_capabilities


def video(format_id="137", width=1920, height=1080, fps="24000/1001", codec="avc1.640028", **extra):
    return {"format_id": format_id, "width": width, "height": height, "fps": fps,
            "vcodec": codec, "acodec": "none", "dynamic_range": "SDR", **extra}


def audio(format_id="140", codec="mp4a.40.2", **extra):
    return {"format_id": format_id, "vcodec": "none", "acodec": codec,
            "audio_channels": 2, "asr": 44100, "abr": 128, **extra}


def plan(*formats, model="AppleTV11,1", **kwargs):
    return plan_media({"formats": list(formats)}, model, **kwargs)


def test_split_tracks_retain_fractional_frame_rate_and_copy_aac_independently():
    result = plan(video(), audio(), audio("251", "opus", abr=160))
    assert result["video"]["fps"] == result["target"]["fps"] == "24000/1001"
    assert result["copy_video"] and result["copy_audio"]
    assert result["audio"]["format_id"] == "140"
    assert result["required_codecs"] == {"decoders": [], "encoders": []}


def test_4k_vp9_beats_1080_h264_and_plans_hevc_at_original_decimal_fps():
    result = plan(video(), video("315", 3840, 2160, 59.94, "vp09.00.51.08"), audio())
    assert result["video"]["format_id"] == "315"
    assert result["target"] == {"container": "mp4", "video_codec": "hevc", "audio_codec": "aac",
                                "width": 3840, "height": 2160, "fps": "2997/50", "hdr": "sdr"}
    assert not result["copy_video"] and result["copy_audio"]
    assert result["required_codecs"] == {"decoders": ["vp9"], "encoders": ["hevc"]}


def test_equivalent_geometry_prefers_copy_over_incomparable_cross_codec_bitrate():
    result = plan(video("avc60", fps=60, codec="avc1.64002a", vbr=4000),
                  video("vp960", fps=60, codec="vp09.00.41.08", vbr=6000), audio())
    assert result["video"]["format_id"] == "avc60"
    assert result["copy_video"] and result["target"]["fps"] == "60/1"


def test_hd_receiver_selects_full_hd_sixty_and_can_convert_hevc_without_forcing_thirty():
    formats = [video("4k", 3840, 2160, 60, "vp9"), video("hd", fps=60, codec="hvc1.1.6.L123.B0"), audio()]
    result = plan(*formats, model="AppleTV5,3")
    assert result["video"]["format_id"] == "hd"
    assert result["target"]["fps"] == "60/1"
    assert result["target"]["video_codec"] == "h264" and not result["copy_video"]
    assert plan(video(codec="hvc1.1.6.L123.B0", fps=30), audio(), model="AppleTV5,3")["copy_video"]


def test_user_caps_select_existing_variant_and_do_not_upscale_or_retime():
    formats = [video("4k", 3840, 2160, 60, "av1"), video("hd60", fps=60),
               video("hd24"), video("720", 1280, 720, 25), audio()]
    result = plan(*formats, max_resolution="1080p", max_fps=30)
    assert result["video"]["format_id"] == "hd24" and result["target"]["fps"] == "24000/1001"
    result = plan(video("720", 1280, 720, 25), audio())
    assert (result["target"]["width"], result["target"]["height"], result["target"]["fps"]) == (1280, 720, "25/1")


@pytest.mark.parametrize("width,height,accepted", [(1080, 1920, True), (1440, 1080, True),
                                                  (1920, 800, True), (1920, 1440, False), (2560, 1080, False)])
def test_non_widescreen_geometry_uses_both_long_and_short_edges(width, height, accepted):
    if accepted:
        result = plan(video(width=width, height=height), audio(), model="AppleTV5,3")
        assert (result["target"]["width"], result["target"]["height"]) == (width, height)
    else:
        with pytest.raises(PlanError, match="quality limits"):
            plan(video(width=width, height=height), audio(), model="AppleTV5,3")


def test_opus_needs_audio_conversion_even_when_video_copies():
    result = plan(video(), audio("251", "opus"))
    assert result["copy_video"] and not result["copy_audio"]
    assert result["required_codecs"] == {"decoders": ["opus"], "encoders": ["aac"]}


@pytest.mark.parametrize("changes", [{"audio_channels": 6}, {"asr": 96000}, {"asr": None},
                                    {"audio_channels": None}, {"acodec": "mp4a.40.5"}, {"abr": 512}])
def test_audio_copy_requires_compatible_lc_layout_and_rate(changes):
    assert not plan(video(), audio(**changes))["copy_audio"]


@pytest.mark.parametrize("codec,extra", [("h264", {}), ("avc1.640033", {}),
                                        ("avc1.640028", {"pix_fmt": "yuv420p10le"}),
                                        ("avc1.6e0028", {}), ("hvc1.1.6.H153.B0", {})])
def test_unknown_or_excessive_video_profiles_require_conversion(codec, extra):
    assert not plan(video(codec=codec, **extra), audio(), model="AppleTV5,3")["copy_video"]


def test_hdr_is_explicitly_blocked_instead_of_silently_downgrading_to_sdr():
    with pytest.raises(PlanError, match="HDR preservation"):
        plan(video(), video("hdr4k", 3840, 2160, 60, "vp9", dynamic_range="HDR10"), audio())
    assert receiver_capabilities("AppleTV6,2")["hdr_max_fps"] == 30
    assert receiver_capabilities("AppleTV11,1")["hdr_max_fps"] == 60
    assert "hdr10+" in receiver_capabilities("AppleTV14,1")["hdr_formats"]
    assert receiver_capabilities("AppleTV5,3")["hdr_formats"] == []


@pytest.mark.parametrize("extra", [{"has_drm": True}, {"is_live": True}, {"live_status": "is_upcoming"},
                                  {"live_status": "post_live"}])
def test_drm_and_unfinished_live_media_are_rejected(extra):
    with pytest.raises(PlanError):
        plan_media({"formats": [video(), audio()], **extra}, "AppleTV11,1")


def test_drm_variants_are_never_selected_and_completed_live_archive_is_vod():
    result = plan_media({"live_status": "was_live", "formats": [video(), audio(),
                        video("encrypted", 3840, 2160, 60, "vp9", has_drm=True)]}, "AppleTV11,1")
    assert result["video"]["format_id"] == "137"


@pytest.mark.parametrize("formats", [[], [audio()], [video()], [video(fps=None), audio()],
                                     [video(width=float("inf")), audio()], [video(fps="1/0"), audio()],
                                     [video(width=10**400), audio()], [video(fps=10**400), audio()],
                                     [video(fps=True), audio()], [video(width=-100), audio()],
                                     [video(format_id="https://private/?token=secret"), audio()]])
def test_missing_tracks_and_invalid_metadata_fail_safely(formats):
    with pytest.raises(PlanError) as error:
        plan(*formats)
    assert "secret" not in str(error.value)


def test_unknown_receiver_and_duplicate_ids_are_rejected():
    with pytest.raises(PlanError, match="unverified"):
        plan(video(), audio(), model="AppleTV999,9")
    with pytest.raises(PlanError, match="ambiguous"):
        plan(video(), video(), audio())


def test_plan_does_not_mutate_or_leak_source_metadata():
    metadata = {"title": "private-title", "webpage_url": "https://private/page",
                "formats": [video(url="https://private/video?signature=secret", http_headers={"Cookie": "secret"}),
                            audio(url="https://private/audio", fragments=[{"url": "https://private/fragment"}])]}
    before = copy.deepcopy(metadata)
    result = plan_media(metadata, "AppleTV11,1")
    assert metadata == before
    output = json.dumps(result)
    assert not any(secret in output for secret in ("private", "secret", "Cookie", "fragments", "http_headers"))
    assert result["requires_probe_verification"] and not result["receiver"]["receiver_verified"]


@pytest.mark.parametrize("kwargs", [{"max_fps": True}, {"max_fps": "nan"}, {"max_fps": "0/1"},
                                    {"max_resolution": True}, {"max_resolution": "4K"}])
def test_invalid_user_limits_are_rejected(kwargs):
    with pytest.raises(PlanError):
        plan(video(), audio(), **kwargs)
