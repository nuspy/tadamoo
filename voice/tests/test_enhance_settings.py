"""The acceleration settings and the one rule that turns them into what the worker renders and what the outputs get."""
import pytest

from callbridge.enhance.settings import EnhanceSettings, parse, plan


def S(**over):
    return EnhanceSettings(**over)


def test_a_missing_or_damaged_object_means_everything_off():
    for stored in (None, {}, "x", [], {"upscale": "magic"}, {"upscale": 5}, {"interpolate": "rife"}):
        got = parse(stored)
        assert not got.active, stored
        assert got.upscale == "none" and got.interpolate == "none"


def test_each_invalid_value_takes_its_default_and_the_rest_is_kept():
    got = parse({"upscale": "basic", "scale": 7, "interpolate": "basic", "factor": 5, "worker_fps_cap": 3, "worker_height_cap": True, "rtx_video_path": "a.txt"})
    assert (got.upscale, got.scale, got.interpolate, got.factor) == ("basic", 2.0, "basic", 2)
    assert (got.worker_fps_cap, got.worker_height_cap, got.rtx_video_path) == (0, 0, "")


def test_a_scale_the_provider_does_not_take_turns_the_stage_off_rather_than_run_wrong():
    assert parse({"upscale": "rtx_video", "scale": 1.5}).upscale == "none"
    assert parse({"upscale": "rtx_video", "scale": 3}).upscale == "rtx_video"
    assert parse({"upscale": "basic", "scale": 1.5}).scale == 1.5


def test_the_sdk_path_must_be_a_dll_path():
    assert parse({"rtx_video_path": "C:\\sdk\\rtxvideo.DLL"}).rtx_video_path == "C:\\sdk\\rtxvideo.DLL"
    assert parse({"rtx_video_path": "C:\\sdk\\run.exe"}).rtx_video_path == ""


def test_a_network_device_or_relative_sdk_path_reads_as_nothing():
    for bad in ("\\\\server\\share\\rtxvideo.dll", "//server/share/rtxvideo.dll", "\\\\?\\C:\\sdk\\rtxvideo.dll", "sdk\\rtxvideo.dll", "rtxvideo.dll",
                "C:\\sdk\\a\x00.dll", "C:\\sdk\\a\n.dll"):
        assert parse({"rtx_video_path": bad}).rtx_video_path == "", bad


def test_with_nothing_on_the_worker_keeps_its_own_size_and_rate():
    p = plan(S(), 25, 480)
    assert not p.active and (p.worker_height, p.worker_fps, p.out_per_in) == (0, 0, 1)
    assert (p.out_height, p.out_fps) == (480, 25)


@pytest.mark.parametrize("scale,height", [(1.5, 320), (2.0, 240), (3.0, 160), (4.0, 120)])
def test_upscaling_asks_the_worker_for_the_target_divided_by_the_scale(scale, height):
    p = plan(S(upscale="basic", scale=scale), 25, 480)
    assert p.worker_height == height and p.out_height == 480 and p.worker_fps == 0


@pytest.mark.parametrize("factor,fps,per_in", [(2, 13, 2), (3, 9, 3)])
def test_interpolation_asks_the_worker_for_the_target_rate_divided_by_the_factor(factor, fps, per_in):
    p = plan(S(interpolate="basic", factor=factor), 25, 480)
    assert (p.worker_fps, p.out_per_in, p.out_fps) == (fps, per_in, 25)
    assert p.worker_fps * p.out_per_in >= 25                      # the outputs never get fewer frames than they were configured for


def test_a_cap_replaces_the_automatic_value_but_never_goes_above_the_target():
    assert plan(S(upscale="basic", worker_height_cap=200), 25, 480).worker_height == 200
    assert plan(S(upscale="basic", worker_height_cap=900), 25, 480).worker_height == 480
    p = plan(S(interpolate="basic", worker_fps_cap=10), 25, 480)
    assert (p.worker_fps, p.out_per_in) == (10, 3)               # 3 out for each in: 30 frames a second cover the 25 asked for
    assert plan(S(interpolate="basic", worker_fps_cap=40), 25, 480).worker_fps == 25


def test_the_worker_is_never_asked_for_less_than_a_quarter_of_the_rate():
    p = plan(S(interpolate="basic", worker_fps_cap=5), 60, 480)
    assert p.worker_fps == 15 and p.out_per_in == 4


def test_a_cap_of_a_stage_that_is_off_is_ignored():
    p = plan(S(worker_height_cap=200, worker_fps_cap=10), 25, 480)
    assert (p.worker_height, p.worker_fps, p.out_per_in) == (0, 0, 1)
    p = plan(S(upscale="basic", worker_fps_cap=10), 25, 480)
    assert p.worker_fps == 0 and p.out_per_in == 1


def test_heights_are_even():
    for h in (333, 481, 479):
        assert plan(S(upscale="basic", scale=1.5), 25, h).worker_height % 2 == 0
        assert plan(S(upscale="basic", scale=1.5), 25, h).out_height % 2 == 0


def test_the_lists_agree_with_the_ones_the_server_validates():
    from serve import voice_settings as vs
    from callbridge.enhance import settings as es
    assert (vs.ENHANCE_UPSCALE, vs.ENHANCE_INTERPOLATE, vs.ENHANCE_SCALES, vs.ENHANCE_FACTORS) == (es.UPSCALE, es.INTERPOLATE, es.SCALES, es.FACTORS)
    assert vs.ENHANCE_PROVIDER_SCALES == es.PROVIDER_SCALES
    assert (vs.ENHANCE_FPS_CAP, vs.ENHANCE_HEIGHT_CAP) == (es.FPS_CAP, es.HEIGHT_CAP)
    assert vs.enhance() == es.EnhanceSettings().as_dict()
