"""The review page of the speed study: for each system the baseline clip and the clip of that system side by side, with the numbers.

    python build_speed_review.py DIR            (DIR has summary.json, metrics.json and the cells)

Copies run 1 of each cell into DIR/review/videos/ and writes DIR/review/review.html (see review_page.py). The numbers come from summary.json; the pictures are to be judged by a person."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_page import build  # noqa: E402

WHAT = {
    "base": "LTX Studio's job as it is: PyTorch SDPA (cuDNN backend on this PC), torch 2.14 nightly, 4 guided passes per step.",
    "sdpa_efficient": "Attention through PyTorch's memory-efficient SDPA kernel instead of cuDNN.",
    "x_fa4": "FlashAttention 4 (4.0.0b39, CuTe DSL) for the self-attention.",
    "t215_fa4": "FlashAttention 4 on torch 2.15 nightly.",
    "x_sage1": "SageAttention 1.0.6 (Triton, int8 QK) for the self-attention.",
    "s2_sage2": "SageAttention 2.2.0 (CUDA, int8 QK + fp8 PV) for the self-attention.",
    "t215_base": "torch 2.15 nightly, nothing else changed.",
    "x_compile": "torch.compile of the transformer blocks (inductor, default mode) by ltx_core's CompilationConfig.",
    "x_fb_10": "First-block cache, threshold 0.10 (stage 1 only).",
    "x_fb_13": "First-block cache, threshold 0.13 (stage 1 only).",
    "x_fb_17": "First-block cache, threshold 0.17 (stage 1 only).",
    "x_tea_10": "TeaCache-style cache per guided pass, threshold 0.10 (stage 1 only).",
    "x_teacache_ltxstudio_03": "LTX Studio's own TeaCache (`teacache_thresh` 0.03).",
    "x_guider_cfg": "Only two guided passes per step (cond + uncond; no STG, no modality pass): a change of the job, not a cache.",
    "x_guider_one": "One pass per step (cfg 1, no STG, no modality): a change of the job, not a cache.",
    "s2b_fb10": "THE WINNER: SageAttention 2.2.0 built here from source + first-block cache 0.10 on stage 1.",
    "s2b_sage2": "SageAttention 2.2.0 built here from source, alone.",
    "s2_fb10": "SageAttention 2 + first-block cache 0.10.",
    "s2_fb13": "SageAttention 2 + first-block cache 0.13.",
    "t211_base": "torch 2.11 (cu130), Python 3.11, nothing else changed.",
    "t211_fa2": "FlashAttention 2.8.4 (prebuilt, sm120) on torch 2.11.",
    "t211_xformers": "xformers 0.0.35 memory_efficient_attention on torch 2.11.",
}


def main() -> int:
    root = Path(sys.argv[1])
    summary = json.loads((root / "summary.json").read_text(encoding="utf-8"))
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8")) if (root / "metrics.json").is_file() else {}
    vids = root / "review" / "videos"
    vids.mkdir(parents=True, exist_ok=True)

    def clip(cell: str) -> str | None:
        src = root / cell / "run1" / "take.mp4"
        if not src.is_file():
            return None
        shutil.copyfile(src, vids / f"{cell}.mp4")
        return f"videos/{cell}.mp4"
    base = clip("base")
    rows = []
    for cell, row in summary.items():
        if cell == "base" or not row.get("ok") or cell not in WHAT:
            continue
        src = clip(cell)
        if not src:
            continue
        m = metrics.get(cell, {})
        nums = {"seconds per job (median, min-max)": f"{row.get('median_s')} ({row.get('min_s')}-{row.get('max_s')}), {row['ok']} runs", "cold / warm": f"{row.get('cold_s')} / {row.get('warm_s')}",
                "speed-up against the baseline median": row.get("speedup_vs_base"), "transformer forward s": row.get("forward_s"), "peak VRAM torch / GPU": f"{row.get('peak_alloc_gib')} GiB / {row.get('gpu_peak_mib')} MiB",
                "PSNR / SSIM against the baseline clip": f"{m.get('psnr_db')} dB / {m.get('ssim')}", "face sharpness (baseline 96.1)": m.get("face_sharpness")}
        rows.append({"id": cell, "title": cell, "what": WHAT[cell], "look": "Left the baseline, right this system: face detail, mouth, flicker, colour.", "numbers": nums,
                     "videos": [{"label": "baseline", "src": base}, {"label": cell, "src": src}]})
    intro = ("Same still, same recording, same seed and prompt, 896x512, 169 frames at 25 fps, 12 steps, saved at crf 4. Each row plays the baseline next to one system. "
             "A different kernel gives a slightly different picture even when it is exact (PSNR 29-31 dB between two exact backends): the numbers show how far from the baseline, you judge whether it matters. Write a note only where something is wrong.")
    out = build(root / "review" / "review.html", "Studio della velocità: LTX 2.5, ripresa ltx25t_12", intro, rows, "velocita-segnalate.md")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
