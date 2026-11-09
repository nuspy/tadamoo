"""The measurement harness on synthetic frames, with a fake GPU: it times what is usable, lists what is not with the reason, and writes a JSON report.
It starts no worker and loads no model."""
import json

import numpy as np
import pytest

from callbridge.enhance import capability as cap
from voice import enhance_bench as bench

GPU = cap.GpuInfo("nvidia", "NVIDIA GeForce RTX 4090", 8.9, "581.42", 24564, 1500)


def fake_smi(monkeypatch, info=GPU):
    monkeypatch.setattr(cap, "read_gpu", lambda index=0: info)


def test_the_synthetic_frames_are_the_asked_size_and_they_move():
    frames = bench.synthetic_frames(64, 48, 5)
    assert len(frames) == 5 and frames[0].shape == (48, 64, 3) and frames[0].dtype == np.uint8
    assert not np.array_equal(frames[0], frames[3])


def test_psnr_is_99_for_equal_pictures_and_lower_for_a_different_one():
    a = bench.synthetic_frames(64, 48, 1)[0]
    assert bench.psnr(a, a) == 99.0
    assert bench.psnr(a, np.zeros_like(a)) < 30


def test_run_times_the_usable_providers_and_lists_the_others_with_their_reason():
    report = cap.report(GPU, platform="linux", importable=lambda n: True)
    frames = bench.synthetic_frames(128, 96, 8)
    got = bench.run(frames, 96, 25, 8, report, lambda i: GPU)
    ups = [r for r in got["results"] if r["stage"] == "upscale"]
    assert [r["scale"] for r in ups] == [1.5, 2.0, 3.0, 4.0] and all(r["provider"] == "basic" for r in ups)
    assert all(r["out"] == [128, 96] and r["ms_per_frame"]["mean"] > 0 for r in ups)
    assert ups[1]["psnr_db"] > 15
    ins = [r for r in got["results"] if r["stage"] == "interpolate"]
    assert [r["factor"] for r in ins] == [2, 3] and ins[0]["psnr_db_vs_real_middle_frame"] is not None
    assert [(s["stage"], s["provider"], s["code"]) for s in got["skipped"]] == [("upscale", "rtx_video", "not_windows")]
    assert got["pipeline_basic_2x_2x"]["ms_per_worker_frame"]["mean"] > 0
    assert got["results"][0]["gpu_memory_mb"] == {"before": 1500, "after": 1500}


def test_without_opencv_nothing_is_timed_and_everything_says_why():
    report = cap.report(GPU, platform="win32", importable=lambda n: n != "cv2")
    got = bench.run(bench.synthetic_frames(64, 48, 4), 48, 25, 4, report, lambda i: GPU)
    assert got["results"] == [] and {s["provider"] for s in got["skipped"]} == {"basic", "rtx_video"}
    assert got["pipeline_basic_2x_2x"]["skipped"]


def test_a_provider_that_raises_is_a_result_not_the_end(monkeypatch):
    from callbridge.enhance import providers

    class Boom:
        def upscale(self, *a):
            raise RuntimeError("boom")
    monkeypatch.setitem(providers._UPSCALERS, "basic", lambda s: Boom())
    report = cap.report(GPU, platform="linux", importable=lambda n: True)
    got = bench.run(bench.synthetic_frames(64, 48, 4), 48, 25, 4, report, lambda i: GPU)
    assert any(r.get("error") == "RuntimeError: boom" for r in got["results"])
    assert any(r["stage"] == "interpolate" and "ms_per_input_frame" in r for r in got["results"])


def test_main_writes_the_report_and_mentions_the_dlss_experiment(tmp_path, monkeypatch, capsys):
    fake_smi(monkeypatch)
    out = tmp_path / "r.json"
    assert bench.main(["--out", str(out), "--height", "96", "--count", "6", "--save-dir", str(tmp_path / "pics")]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["gpu"]["name"] == "NVIDIA GeForce RTX 4090" and data["target"] == {"height": 96, "fps": 25}
    assert data["runs"][0]["input"] == "synthetic" and data["runs"][0]["results"]
    assert data["dlss_experiment"]["status"] == "not run here" and "Streamline" in data["dlss_experiment"]["why"]
    assert (tmp_path / "pics" / "reference.png").is_file() and "report written" in capsys.readouterr().out


def test_main_also_times_the_frames_of_a_supplied_video(tmp_path, monkeypatch):
    import cv2
    fake_smi(monkeypatch)
    clip = tmp_path / "c.mp4"
    w = cv2.VideoWriter(str(clip), cv2.VideoWriter_fourcc(*"mp4v"), 10.0, (128, 96))
    for f in bench.synthetic_frames(128, 96, 8):
        w.write(f)
    w.release()
    out = tmp_path / "r.json"
    assert bench.main(["--out", str(out), "--video", str(clip), "--height", "96", "--count", "6"]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [r["input"] for r in data["runs"]] == ["synthetic", "video"] and data["inputs"][1]["frames"] == 6


def test_a_video_that_cannot_be_read_stops_with_a_message(tmp_path):
    with pytest.raises(SystemExit, match="no frames"):
        bench.video_frames(str(tmp_path / "nope.mp4"), 5)
