"""The attention backends on the shapes of the LTX 2.5 clip job, one call at a time: time per call and difference from PyTorch's cuDNN attention.

    python attn_micro.py BACKEND [BACKEND ...]      (names as SL_ATTN of ltx_bench.py; run by the Python of the environment that has the backend)

Self-attention of the video: 32 heads of 128, bf16, batch 1; stage 1 of the job has 2464 tokens (448x256, 169 frames), stage 2 has 9856 (896x512). Prints one JSON line per backend and shape."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parent))
SHAPES = {"stage1_2464": 2464, "stage2_9856": 9856}
HEADS, DIM = 32, 128


def timed(fn, q, k, v, iters=40):
    for _ in range(5):
        fn(q, k, v, HEADS)
    torch.cuda.synchronize()
    a, b = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    a.record()
    for _ in range(iters):
        out = fn(q, k, v, HEADS)
    b.record()
    torch.cuda.synchronize()
    return a.elapsed_time(b) / iters, out


def main() -> int:
    import ltx_bench
    names = sys.argv[1:]
    torch.manual_seed(0)
    for label, n in SHAPES.items():
        q, k, v = (torch.randn(1, n, HEADS * DIM, device="cuda", dtype=torch.bfloat16) for _ in range(3))
        ref_ms, ref = timed(ltx_bench.make_attention("cudnn"), q, k, v)
        for name in names:
            row = {"backend": name, "shape": label}
            try:
                ms, out = timed(ltx_bench.make_attention(name), q, k, v)
                diff = (out.float() - ref.float()).abs()
                row.update(ms=round(ms, 3), speed_vs_cudnn=round(ref_ms / ms, 2), max_abs_err=round(float(diff.max()), 4), mean_abs_err=round(float(diff.mean()), 5), cudnn_ms=round(ref_ms, 3))
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(json.dumps(row), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
