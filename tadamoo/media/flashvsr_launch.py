"""Runs LTX Studio's FlashVSR+ runner (`workers/flashvsrplus_run.py`) the way TaDaMoo needs it: Turbo on Windows, a picture with the size, the frames and the duration of the input.

    python flashvsr_launch.py RUNNER.py INPUT OUTPUT [the runner's own arguments]

Three things LTX Studio's runner does not do, each applied from outside (its source is not changed):

  1. The attention kernel. The runner forces PyTorch's SDPA with an explicit block mask, which at 2x is more than two billion entries and is refused. FlashVSR+ ships a block-sparse kernel written in
     Triton (`operators/flashvsr_attn` of its repository); with `triton-windows` it runs on Windows. It is put on the path and demanded (`triton_block_sparse`), so that a missing kernel is an error and
     never a silent return to the mask.
  2. The saved quality. FlashVSR+ writes its picture with libx264's default (crf 23, veryfast), which loses the detail it has just made; it is written at `FLASHVSR_CRF` (default 4, preset medium).
  3. The size. "Improving" a clip means removing the blockiness and restoring detail, not making it larger: with `--scale` 2 (the default) FlashVSR+ works at twice the size and the result is brought back to the
     size of the input with a Lanczos downscale (`FLASHVSR_DOWN`, ffmpeg's scaler flags; the working file is written lossless and the final one at `FLASHVSR_CRF`); with `--scale` 1 it restores at the size of the
     input and nothing is scaled.
  4. The length. The streaming decoder gives three frames fewer than it is given at the end of the clip (169 in, 166 out). The input is given `pad` more copies of its last frame (4 to 7, so that the
     length stays on the model's grid of 4n+1) and the writer stops at exactly the number of frames of the input: the enlarged clip has the frames, and so the duration, of the original, and the voice
     is in step with the picture. If fewer frames come out the launcher stops with an error and says so.

`FLASHVSR_LAUNCH=plain` runs the runner as it is (the tests of the job's plumbing use it). Python of the FlashVSR+ environment, standard library at import; torch, cv2 and imageio only when the runner needs them."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path


def pad_for(frames: int) -> int:
    """How many copies of the last frame to add: at least 4 (the decoder's loss and one more), and as many as keep the length at 4n+1."""
    return 4 + (-(frames - 1)) % 4


def option(args: list, name: str, default: str = "") -> str:
    return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default


def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def size_of(path: str) -> tuple:
    import cv2
    cap = cv2.VideoCapture(path)
    size = (int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)))
    cap.release()
    return size


def count_frames(path: str) -> int:
    import cv2
    cap = cv2.VideoCapture(path)
    n = 0
    while cap.grab():                                       # the container's own count is not always the number of frames that decode
        n += 1
    cap.release()
    return n


def padded_copy(source: str, target: str, extra: int) -> None:
    """The source with `extra` copies of its last frame added, the picture lossless (the frames that decode are the same)."""
    done = subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", source, "-an", "-vf", f"tpad=stop_mode=clone:stop={extra}", "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast", "-pix_fmt", "yuv420p", target],
                          capture_output=True, text=True)
    if done.returncode != 0 or not Path(target).is_file():
        raise RuntimeError("the input could not be padded: " + done.stderr[-300:])


def use_triton_kernel(code_dir: str) -> None:
    sys.path.insert(0, os.path.join(code_dir, "operators", "flashvsr_attn"))
    from inference import presets
    original = presets.stream_config_for_mode

    def patched(mode, **kw):
        kw.update(hardware_profile="auto", attn_kernel="triton_block_sparse")
        return original(mode, **kw)
    presets.stream_config_for_mode = patched
    import flashvsr_attn                                    # noqa: F401  (a missing kernel stops here, loudly)
    print("[launch] attention: triton_block_sparse (flashvsr_attn)", flush=True)


def fit_to_input(big: str, final: str, width: int, height: int, crf: str, flags: str) -> None:
    """The enlarged picture brought back to `width` x `height`, encoded once at `crf`."""
    done = subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", big, "-vf", f"scale={width}:{height}:flags={flags},format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p", "-an", final],
                          capture_output=True, text=True)
    if done.returncode != 0 or not Path(final).is_file():
        raise RuntimeError("the picture could not be brought back to the size of the input: " + done.stderr[-300:])


def save_quality(crf: str, limit: int) -> None:
    """FlashVSR+'s writer at `crf`, stopping at `limit` frames."""
    from pathlib import Path as P
    import imageio
    from inference import output as out_mod

    def init(self, path, fps, *, preset="veryfast"):
        self.destination = P(path)
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.fps = float(fps)
        self.writer = imageio.get_writer(str(self.destination), format="FFMPEG", mode="I", fps=self.fps, codec="libx264", pixelformat="yuv420p", macro_block_size=None,
                                         output_params=["-preset", "medium", "-crf", crf])
        self.frame_count = 0
        self.first_frame = None

    def append(self, video, *, value_range="neg_one_to_one"):
        if video.ndim != 5 or video.shape[0] != 1:
            raise ValueError("Expected one video [1,C,F,H,W]")
        for tensor_frame in video[0].permute(1, 0, 2, 3):
            if self.frame_count >= limit:
                return                                      # the frames added to the end of the input are not written
            frame = out_mod.tensor_to_uint8_frame(tensor_frame, value_range=value_range)
            if self.first_frame is None:
                self.first_frame = frame
            self.writer.append_data(frame)
            self.frame_count += 1
    out_mod.IncrementalVideoWriter.__init__ = init
    out_mod.IncrementalVideoWriter.append = append
    print(f"[launch] writer: crf {crf}, at most {limit} frames", flush=True)


def main() -> int:
    runner, args = sys.argv[1], sys.argv[2:]
    source, target = args[0], args[1]
    code_dir = option(args, "--code-dir", os.environ.get("LTX_FLASHVSRPLUS_CODE", ""))
    plain = os.environ.get("FLASHVSR_LAUNCH") == "plain"
    padded = ""
    if not plain:
        sys.path.insert(0, code_dir)
        frames = count_frames(source)
        width, height = size_of(source)
        working = float(option(args, "--scale", "2"))
        shrink = working > 1.0 + 1e-6                        # the working picture is larger than the input: it is brought back, so the working file is written lossless
        crf = os.environ.get("FLASHVSR_CRF", "4")
        extra = pad_for(frames)
        padded = target + ".padded.mp4"
        padded_copy(source, padded, extra)
        use_triton_kernel(code_dir)
        save_quality("0" if shrink else crf, frames)
        args = [padded, *args[1:]]
        print(f"[launch] {frames} frames in, {extra} added at the end for the model, {frames} to be written", flush=True)
    if plain:
        import runpy
        sys.argv = [runner, *args]
        runpy.run_path(runner, run_name="__main__")
        return 0
    spec = importlib.util.spec_from_file_location("flashvsrplus_run", runner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not plain:
        original_finish = module.crop_and_finish

        def finish(raw, out, sw, sh, tw, th):               # a size that is not a multiple of 128 is cut after the enlargement: the same quality, the same frames
            if (sw, sh) == (tw, th):
                return original_finish(raw, out, sw, sh, tw, th)
            done = subprocess.run([ffmpeg(), "-v", "error", "-y", "-i", raw, "-vf", f"crop={sw}:{sh}:0:0", "-c:v", "libx264", "-preset", "medium", "-crf", "0" if shrink else crf,
                                   "-pix_fmt", "yuv420p", "-map", "0:v:0", out], capture_output=True, text=True)
            if done.returncode != 0:
                raise RuntimeError("the final crop failed: " + done.stderr[-300:])
            os.remove(raw)
        module.crop_and_finish = finish
    try:
        code = module.main(args)
    finally:
        if padded and os.path.exists(padded):
            os.remove(padded)
    if code == 0 and shrink:
        big = target + ".big.mp4"
        os.replace(target, big)
        try:
            fit_to_input(big, target, width, height, crf, os.environ.get("FLASHVSR_DOWN", "lanczos+accurate_rnd+full_chroma_int"))
        finally:
            if os.path.exists(big) and not os.environ.get("FLASHVSR_KEEP_WORKING"):          # FLASHVSR_KEEP_WORKING: keep the lossless working file (to compare ways of scaling it down)
                os.remove(big)
        print(f"[launch] brought back to {width}x{height}", flush=True)
    if code == 0:
        got = count_frames(target)
        if got != frames:
            print(f"ERRORE: the enlarged video has {got} frames, the original {frames}", flush=True)
            return 5
        print(f"[launch] {got} frames out, as many as in", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
