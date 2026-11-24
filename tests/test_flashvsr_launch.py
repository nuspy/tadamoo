"""The FlashVSR+ launcher (tadamoo/media/flashvsr_launch.py): how many frames it adds for the model, that the copy it makes of the input has the same frames plus the added ones, that its writer
stops at exactly the frames of the input and writes at the quality asked, and that a count that differs is an error. The enlargement itself needs the FlashVSR+ environment and a GPU and is
measured on real clips (docs/media/speed-study.md)."""
import shutil
import sys
import types
from pathlib import Path

import pytest

from video_fakes import make_clip
from tadamoo.media import flashvsr_launch as launch

FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")


def test_the_padding_is_at_least_four_frames_and_keeps_the_length_on_the_models_grid():
    for frames in (1, 2, 33, 97, 168, 169, 170, 171, 173, 297):
        extra = launch.pad_for(frames)
        assert extra >= 4 and (frames + extra - 1) % 4 == 0 and extra <= 7
    assert launch.pad_for(169) == 4 and launch.pad_for(297) == 4


def test_the_options_of_the_runner_are_read_from_its_arguments():
    args = ["in.mp4", "out.mp4", "--mode", "turbo", "--code-dir", "C:/code", "--scale", "2"]
    assert launch.option(args, "--code-dir") == "C:/code" and launch.option(args, "--missing", "x") == "x" and launch.option(["--code-dir"], "--code-dir", "d") == "d"


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_the_padded_copy_has_the_frames_of_the_source_and_the_added_ones_of_its_last_frame(tmp_path):
    cv2 = pytest.importorskip("cv2")
    src = make_clip(tmp_path / "src.mp4", 1.0, "128x72")
    n = launch.count_frames(str(src))
    padded = tmp_path / "padded.mp4"
    launch.padded_copy(str(src), str(padded), 4)
    assert launch.count_frames(str(padded)) == n + 4

    def frames(path):
        cap, out = cv2.VideoCapture(str(path)), []
        while True:
            ok, f = cap.read()
            if not ok:
                return out
            out.append(f)
    a, b = frames(src), frames(padded)
    assert all(abs(x.astype(int) - y.astype(int)).max() <= 2 for x, y in zip(a, b[:n])) and all(abs(b[n - 1].astype(int) - y.astype(int)).max() <= 2 for y in b[n:])       # lossless up to the codec's rounding


def test_the_writer_stops_at_the_frames_of_the_input_and_writes_at_the_quality_asked(monkeypatch):
    torch = pytest.importorskip("torch")
    written, params = [], {}

    class Writer:
        def append_data(self, frame):
            written.append(frame)

    def get_writer(path, **kw):
        params.update(kw)
        return Writer()
    output = types.ModuleType("inference.output")
    output.IncrementalVideoWriter = type("IncrementalVideoWriter", (), {})
    output.tensor_to_uint8_frame = lambda frame, value_range=None: int(frame.flatten()[0])
    package = types.ModuleType("inference")
    package.output = output
    monkeypatch.setitem(sys.modules, "inference", package)
    monkeypatch.setitem(sys.modules, "inference.output", output)
    monkeypatch.setitem(sys.modules, "imageio", types.SimpleNamespace(get_writer=get_writer))
    launch.save_quality("4", 6)
    w = output.IncrementalVideoWriter.__new__(output.IncrementalVideoWriter)
    output.IncrementalVideoWriter.__init__(w, Path("x.mp4"), 25)
    assert params["output_params"] == ["-preset", "medium", "-crf", "4"] and params["fps"] == 25.0
    segment = torch.arange(4, dtype=torch.float32).view(1, 1, 4, 1, 1).expand(1, 3, 4, 2, 2)           # [B, C, F, H, W], 4 frames valued 0..3
    output.IncrementalVideoWriter.append(w, segment)
    output.IncrementalVideoWriter.append(w, segment)                                                       # 8 offered, 6 allowed
    assert written == [0, 1, 2, 3, 0, 1] and w.frame_count == 6


@pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed")
def test_a_picture_made_at_twice_the_size_is_brought_back_to_the_size_of_the_input_with_the_same_frames(tmp_path):
    pytest.importorskip("cv2")                         # fit_to_input reads and writes the frames with it
    big = make_clip(tmp_path / "big.mp4", 1.0, "256x144")
    final = tmp_path / "final.mp4"
    launch.fit_to_input(str(big), str(final), 128, 72, "4", "lanczos+accurate_rnd+full_chroma_int")
    import subprocess
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(final)], capture_output=True, text=True).stdout.strip()
    assert out == "128,72" and launch.count_frames(str(final)) == launch.count_frames(str(big))
    assert launch.size_of(str(final)) == (128, 72)
