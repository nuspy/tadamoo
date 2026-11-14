"""LTX Studio's FlashVSR+ runner (`workers/flashvsrplus_run.py`) with the attention kernel chosen from outside.

    python flashvsr_run.py INPUT OUTPUT [runner arguments]       with SL_VSR_KERNEL = sdpa | triton | auto and SL_VSR_CRF (the saved quality; unset = the writer's own crf 23)

LTX Studio's runner forces `hardware_profile="generic"`, which means PyTorch's SDPA with an explicit block mask: on a 2x enlargement of 896x512 that mask is more than 2 billion entries and the code refuses.
FlashVSR+ itself ships a Triton block-sparse kernel (`operators/flashvsr_attn`, pure Python on top of Triton); with `triton` the profile is "auto" and the kernel `triton_block_sparse` is demanded, so a missing
kernel is an error instead of a silent return to SDPA. The kernel folder is put on the path from here, so no environment is changed."""
from __future__ import annotations

import functools
import importlib.util
import os
import sys

RUNNER = "E:/AI/ltxstudio/workers/flashvsrplus_run.py"
CODE = os.environ.get("LTX_FLASHVSRPLUS_CODE", "E:/AI/Tools/FlashVSR_Plus")


def main() -> int:
    kernel = os.environ.get("SL_VSR_KERNEL", "sdpa")
    sys.path.insert(0, CODE)
    if kernel in ("triton", "auto"):
        sys.path.insert(0, os.path.join(CODE, "operators", "flashvsr_attn"))
        from inference import presets
        original = presets.stream_config_for_mode
        wanted = "triton_block_sparse" if kernel == "triton" else "auto"

        @functools.wraps(original)
        def patched(mode, **kw):
            kw.update(hardware_profile="auto", attn_kernel=wanted)
            return original(mode, **kw)
        presets.stream_config_for_mode = patched
        import flashvsr_attn
        print(f"[SL] flashvsr_attn {flashvsr_attn.__file__} kernel {wanted}", flush=True)
    crf = os.environ.get("SL_VSR_CRF", "")
    if crf:                                                           # the writer of FlashVSR+ saves at libx264's default quality (crf 23, veryfast)
        from inference import output as out_mod
        import imageio
        from pathlib import Path

        def init(self, path, fps, *, preset="veryfast"):
            self.destination = Path(path)
            self.destination.parent.mkdir(parents=True, exist_ok=True)
            self.fps = float(fps)
            self.writer = imageio.get_writer(str(self.destination), format="FFMPEG", mode="I", fps=self.fps, codec="libx264", pixelformat="yuv420p", macro_block_size=None,
                                             output_params=["-preset", "medium", "-crf", crf])
            self.frame_count = 0
            self.first_frame = None
        out_mod.IncrementalVideoWriter.__init__ = init
        print(f"[SL] FlashVSR+ writer saves at crf {crf}, preset medium", flush=True)
    spec = importlib.util.spec_from_file_location("flashvsrplus_run", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if crf:                                                           # the final crop (when the size is not a multiple of 128) re-encodes at crf 14: same quality here
        original_finish = module.crop_and_finish

        def finish(raw, out, sw, sh, tw, th):
            if (sw, sh) == (tw, th):
                return original_finish(raw, out, sw, sh, tw, th)
            import subprocess
            import shutil
            ff = shutil.which("ffmpeg") or __import__("imageio_ffmpeg").get_ffmpeg_exe()
            r = subprocess.run([ff, "-v", "error", "-y", "-i", raw, "-vf", f"crop={sw}:{sh}:0:0", "-c:v", "libx264", "-preset", "medium", "-crf", crf, "-pix_fmt", "yuv420p", "-map", "0:v:0", out], capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(r.stderr[-400:])
            os.remove(raw)
        module.crop_and_finish = finish
    return module.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
