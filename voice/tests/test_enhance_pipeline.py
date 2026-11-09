"""The enhancer on fake frames (numpy arrays): order of the stages, the size and rate the outputs get, passthrough, and a provider that fails
never stops the picture."""
import threading
import time

import cv2
import numpy as np
import pytest

from callbridge.avatar_client import Frame
from callbridge.enhance import capability as cap
from callbridge.enhance import providers
from callbridge.enhance.pipeline import Enhancer
from callbridge.enhance.settings import EnhanceSettings

GPU = cap.GpuInfo("nvidia", "NVIDIA GeForce RTX 4090", 8.9, "581.42", 24564, 100)
REPORT = cap.report(GPU, platform="win32", importable=lambda n: True)
NO_CAMERA_PC = cap.report(cap.GpuInfo(note="none"), platform="linux", importable=lambda n: True)


def frame_of(value, w=320, h=240):
    """A flat frame of one grey level, as the worker sends it (I420)."""
    img = np.full((h, w, 3), value, np.uint8)
    return Frame(w, h, cv2.cvtColor(img, cv2.COLOR_BGR2YUV_I420).tobytes())


def level(frame):
    return int(frame.bgr()[0, 0, 0])


class Sink:
    def __init__(self):
        self.frames = []
        self.lock = threading.Lock()

    def __call__(self, frame):
        with self.lock:
            self.frames.append(frame)


def make(settings, fps=25, height=480, report=REPORT, pacing=False, clock=None):
    sink = Sink()
    enh = Enhancer(sink, pacing=pacing, **({"clock": clock} if clock else {}))
    plan = enh.configure(settings, report, fps, height)
    return enh, sink, plan


def test_with_both_stages_off_the_frame_goes_through_untouched():
    enh, sink, plan = make(EnhanceSettings())
    f = frame_of(100)
    enh.feed(f)
    assert sink.frames == [f] and sink.frames[0] is f and not plan.active
    assert enh.status()["timing"]["passthrough"] == 1


def test_upscaling_gives_the_outputs_exactly_the_target_size():
    enh, sink, plan = make(EnhanceSettings(upscale="basic", scale=2.0))
    assert plan.worker_height == 240
    enh.feed(frame_of(90, 320, 240))
    out = sink.frames[0]
    assert (out.height, out.width) == (480, 640) and len(out.i420) == 640 * 480 * 3 // 2
    assert abs(level(out) - 90) <= 2


def test_the_size_is_the_target_even_when_the_worker_renders_another_ratio():
    enh, sink, plan = make(EnhanceSettings(upscale="basic", scale=2.0, worker_height_cap=200))
    enh.feed(frame_of(90, 270, 200))
    assert (sink.frames[0].height, sink.frames[0].width) == (480, 648)


def test_a_frame_already_at_the_target_height_is_not_resized():
    enh, sink, _ = make(EnhanceSettings(upscale="basic"))
    enh.feed(frame_of(90, 640, 480))
    assert (sink.frames[0].height, sink.frames[0].width) == (480, 640)
    assert enh.status()["upscale"]["ms"] is None


def test_the_first_frame_goes_out_alone_and_each_next_one_brings_n_frames_ending_with_itself():
    enh, sink, plan = make(EnhanceSettings(interpolate="basic", factor=2))
    assert plan.out_per_in == 2
    enh.feed(frame_of(0))
    assert [level(f) for f in sink.frames] == [0]
    enh.feed(frame_of(100))
    assert [level(f) for f in sink.frames] == pytest.approx([0, 50, 100], abs=1)                 # the blend, then the real frame
    enh.feed(frame_of(200))
    assert [level(f) for f in sink.frames] == pytest.approx([0, 50, 100, 150, 200], abs=1)


def test_a_factor_of_three_sends_two_blends_and_the_real_frame():
    enh, sink, plan = make(EnhanceSettings(interpolate="basic", factor=3))
    enh.feed(frame_of(0))
    enh.feed(frame_of(90))
    assert [level(f) for f in sink.frames][1:] == pytest.approx([30, 60, 90], abs=1)


def test_both_stages_run_in_order_upscale_first_then_interpolation_at_the_target_size():
    enh, sink, plan = make(EnhanceSettings(upscale="basic", scale=2.0, interpolate="basic", factor=2))
    assert (plan.worker_height, plan.worker_fps) == (240, 13)
    enh.feed(frame_of(0, 320, 240))
    enh.feed(frame_of(100, 320, 240))
    assert len(sink.frames) == 3 and all((f.height, f.width) == (480, 640) for f in sink.frames)
    assert level(sink.frames[1]) in (49, 50, 51)


def test_a_change_of_size_between_frames_does_not_blend_across_it():
    enh, sink, _ = make(EnhanceSettings(interpolate="basic", factor=2))
    enh.feed(frame_of(0, 320, 240))
    enh.feed(frame_of(100, 160, 120))
    assert [(f.width, f.height) for f in sink.frames] == [(320, 240), (160, 120)]


def test_no_blend_across_a_long_pause():
    now = [0.0]
    enh, sink, _ = make(EnhanceSettings(interpolate="basic"), clock=lambda: now[0])
    enh.feed(frame_of(0))
    now[0] = 5.0
    enh.feed(frame_of(100))
    assert len(sink.frames) == 2


def test_a_provider_that_the_report_calls_unusable_is_not_run_and_the_worker_is_not_asked_for_less():
    enh, sink, plan = make(EnhanceSettings(upscale="rtx_video", scale=2.0, interpolate="basic"), report=NO_CAMERA_PC)
    assert plan.worker_height == 0 and plan.worker_fps > 0          # the interpolation (basic) can run; the upscaling cannot
    st = enh.status()
    assert st["upscale"]["running"] is False and "Windows" in st["upscale"]["reason"]
    enh.feed(frame_of(10))
    assert len(sink.frames) == 1 and sink.frames[0].height == 240


def test_with_nothing_usable_the_frames_pass_and_the_plan_is_inactive():
    enh, sink, plan = make(EnhanceSettings(upscale="rtx_video", scale=2.0), report=NO_CAMERA_PC)
    assert not plan.active
    f = frame_of(1)
    enh.feed(f)
    assert sink.frames == [f]


class Failing:
    name = "failing"

    def upscale(self, img, w, h):
        raise providers.ProviderError("the card said no")

    def interpolate(self, a, b, alphas):
        raise providers.ProviderError("no blend today")


@pytest.fixture
def failing(monkeypatch):
    monkeypatch.setitem(providers._UPSCALERS, "failing", lambda s: Failing())
    monkeypatch.setitem(providers._INTERPOLATORS, "failing", lambda s: Failing())
    report = {"upscale": {"failing": {"available": True}}, "interpolate": {"failing": {"available": True}}}
    return report


def test_a_failing_upscaler_never_stops_the_frames_and_is_switched_off_after_three_failures(failing):
    enh, sink, _ = make(EnhanceSettings(upscale="failing"), report=failing)
    for _ in range(5):
        enh.feed(frame_of(10))
    assert len(sink.frames) == 5 and all(f.height == 240 for f in sink.frames)           # passed through, not lost
    st = enh.status()["upscale"]
    assert st["running"] is False and "3 failures in a row" in st["reason"] and st["errors"] == 3 and "the card said no" in st["last_error"]


def test_a_failing_interpolator_sends_the_real_frame_alone(failing):
    enh, sink, _ = make(EnhanceSettings(interpolate="failing"), report=failing)
    for v in (0, 50, 100):
        enh.feed(frame_of(v))
    assert [level(f) for f in sink.frames] == [0, 50, 100]
    assert enh.status()["interpolate"]["errors"] == 2


def test_an_upscaler_that_returns_the_wrong_size_counts_as_a_failure(failing, monkeypatch):
    class Wrong(Failing):
        def upscale(self, img, w, h):
            return img
    monkeypatch.setitem(providers._UPSCALERS, "failing", lambda s: Wrong())
    enh, sink, _ = make(EnhanceSettings(upscale="failing"), report=failing)
    enh.feed(frame_of(10))
    assert sink.frames[0].height == 240 and enh.status()["upscale"]["errors"] == 1


def test_a_provider_that_cannot_be_built_is_reported_and_the_frames_pass(monkeypatch):
    def boom(settings):
        raise providers.NeedsSdk("the SDK is not here")
    monkeypatch.setitem(providers._UPSCALERS, "basic", boom)
    enh, sink, _ = make(EnhanceSettings(upscale="basic"))
    enh.feed(frame_of(10))
    assert len(sink.frames) == 1 and "the SDK is not here" in enh.status()["upscale"]["reason"]


def test_a_frame_that_cannot_be_decoded_goes_out_as_it_came_and_feed_does_not_raise():
    enh, sink, _ = make(EnhanceSettings(upscale="basic"))

    class Broken:
        width, height, i420 = 2, 2, b""

        def bgr(self):
            raise ValueError("bad frame")
    broken = Broken()
    enh.feed(broken)
    assert sink.frames == [broken] and "bad frame" in enh.status()["last_error"]


def test_the_rtx_video_provider_without_its_binding_says_what_to_install():
    from callbridge.enhance.rtx_video import RtxVideoUpscaler
    with pytest.raises(providers.NeedsSdk, match="RTX Video SDK is not installed"):
        RtxVideoUpscaler(EnhanceSettings()).upscale(None, 2, 2)
    with pytest.raises(providers.NeedsSdk, match="no binding"):
        RtxVideoUpscaler(EnhanceSettings(rtx_video_path="C:\\x\\a.dll")).upscale(None, 2, 2)


def test_a_worker_started_before_the_settings_changed_gets_what_it_can_and_the_status_says_to_restart():
    enh, sink, plan = make(EnhanceSettings(interpolate="basic", factor=2))
    enh.worker_started(0, 25)                                        # started with the old settings: 25 frames a second
    enh.feed(frame_of(0))
    enh.feed(frame_of(100))
    assert len(sink.frames) == 2                                     # 25 in, 25 out: nothing to add
    assert enh.status()["restart_needed"] is True
    enh.worker_started(0, plan.worker_fps)
    assert enh.status()["restart_needed"] is False
    enh.feed(frame_of(0))
    enh.feed(frame_of(100))
    assert len(sink.frames) == 5                                     # the first frame after a restart goes alone, the next brings its blend


def test_the_worker_stopping_clears_the_neighbour_frame():
    enh, sink, _ = make(EnhanceSettings(interpolate="basic"))
    enh.feed(frame_of(0))
    enh.worker_stopped()
    enh.feed(frame_of(100))
    assert [level(f) for f in sink.frames] == [0, 100]


def test_the_timings_are_measured_and_compared_with_the_frame_budget():
    enh, sink, _ = make(EnhanceSettings(upscale="basic", interpolate="basic"))
    for v in (0, 50, 100, 150):
        enh.feed(frame_of(v))
    t = enh.status()["timing"]
    assert t["frames_in"] == 4 and t["frames_out"] == len(sink.frames) == 7
    assert t["total_ms"] > 0 and t["convert_ms"] > 0 and t["budget_ms"] > 0 and isinstance(t["over_budget"], bool)
    assert enh.status()["upscale"]["ms"] > 0 and enh.status()["interpolate"]["ms"] > 0


def test_the_whole_rule_for_every_scale_and_factor_the_outputs_have_the_configured_size_and_rate():
    for scale in (1.5, 2.0, 3.0, 4.0):
        for factor in (2, 3):
            enh, sink, plan = make(EnhanceSettings(upscale="basic", scale=scale, interpolate="basic", factor=factor))
            w = 2 * round(plan.worker_height * 4 / 3 / 2)
            for v in range(0, 100, 10):
                enh.feed(frame_of(v, w, plan.worker_height))
            assert {(f.height) for f in sink.frames} == {480}, (scale, factor)
            assert len(sink.frames) == 1 + 9 * plan.out_per_in
            assert plan.worker_fps * plan.out_per_in >= 25


def test_paced_blends_arrive_in_order_between_the_two_real_frames():
    enh = Enhancer(sink := Sink(), pacing=True)
    enh.configure(EnhanceSettings(interpolate="basic", factor=2), REPORT, 25, 480)
    try:
        for v in (0, 100, 200):
            enh.feed(frame_of(v))
            time.sleep(0.06)
        deadline = time.time() + 2
        while len(sink.frames) < 5 and time.time() < deadline:
            time.sleep(0.01)
        assert [level(f) for f in sink.frames] == pytest.approx([0, 50, 100, 150, 200], abs=1)
    finally:
        enh.close()
