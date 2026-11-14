"""LTX Studio's FlashVSR+ runner (`workers/flashvsrplus_run.py`) with the attention kernel chosen from outside.

    python flashvsr_run.py INPUT OUTPUT [runner arguments]       with SL_VSR_KERNEL = sdpa | triton | auto

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
    spec = importlib.util.spec_from_file_location("flashvsrplus_run", RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
