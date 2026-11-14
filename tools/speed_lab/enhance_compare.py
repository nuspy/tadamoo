"""The four versions of the winning clip: not enhanced, SeedVR2 2x, FlashVSR+ 2x, FlashVSR+ 1.5x; each timed, with its VRAM, sharpness measures and a review page.

    python enhance_compare.py --out DIR [--src CLIP] [--only a,b,c,d] [--seedvr-model NAME] [--seedvr-batch N] [--vsr-kernel triton|sdpa]

Each version goes through `timed_run.py` (below-normal priority, VRAM sampled) into DIR/<id>/. The enlarged files get the sound of the source back (the enlargers write picture only)."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
FFMPEG = "E:/AI/FFMPEG/bin/ffmpeg.exe"
SRC = "E:/AI/Output/motion_matrix/20261114-run1/raw/ltx25t_12.mp4"
VSR_PY = "E:/AI/Tools/inference_server_venv/Scripts/python.exe"
SEED_PY = "E:/AI/Tools/SeedVR2/venv/Scripts/python.exe"
SEED_DIR = "E:/AI/Tools/SeedVR2"
CKPT = "E:/AI/Models/FlashVSR/FlashVSR_plus"
CODE = "E:/AI/Tools/FlashVSR_Plus"


def ffmpeg() -> str:
    import shutil as sh
    found = sh.which("ffmpeg")
    return found or next(str(p) for p in Path("E:/AI/FFMPEG").rglob("ffmpeg.exe"))


def with_sound(picture: Path, source: Path, out: Path) -> Path:
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", str(picture), "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy", "-c:a", "copy", str(out)], check=True)
    return out


def timed(folder: Path, cmd: list, env: list = (), cwd: str | None = None, timeout: int = 3000) -> dict:
    run = [sys.executable, str(HERE / "timed_run.py"), "--dir", str(folder), "--timeout", str(timeout)]
    if env:
        run += ["--env", *env]
    if cwd:
        run += ["--cwd", cwd]
    subprocess.run(run + ["--", *cmd])
    return json.loads((folder / "result.json").read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=SRC)
    ap.add_argument("--only", default="none,seedvr2_x2,vsr_x2,vsr_x15")
    ap.add_argument("--seedvr-model", default="seedvr2_ema_7b_fp8_e4m3fn_mixed_block35_fp16.safetensors")
    ap.add_argument("--seedvr-batch", type=int, default=33)
    ap.add_argument("--vsr-kernel", default="triton")
    ap.add_argument("--vsr-mode", default="turbo")
    ap.add_argument("--max-frames", type=int, default=0)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    src = Path(args.src)
    todo = args.only.split(",")
    if "none" in todo:
        shutil.copyfile(src, out / "none.mp4")
    for vid, scale in (("vsr_x2", 2.0), ("vsr_x15", 1.5)):
        if vid not in todo:
            continue
        folder = out / vid
        raw = folder / "picture.mp4"
        cmd = [VSR_PY, str(HERE / "flashvsr_run.py"), str(src), str(raw), "--mode", args.vsr_mode, "--scale", f"{scale:g}", "--seed", "1", "--temporal-compression", "4", "--decode-every", "profile", "--code-dir", CODE, "--ckpt-root", CKPT,
               *(["--max-frames", str(args.max_frames)] if args.max_frames else []), "--stats-json", str(folder / "stats.json")]
        folder.mkdir(parents=True, exist_ok=True)
        res = timed(folder, cmd, env=[f"SL_VSR_KERNEL={args.vsr_kernel}", "SL_VSR_CRF=4", "PYTHONPATH="])
        if res["exit"] == 0 and raw.is_file():
            with_sound(raw, src, out / f"{vid}.mp4")
    if "seedvr2_x2" in todo:
        folder = out / "seedvr2_x2"
        folder.mkdir(parents=True, exist_ok=True)
        raw = folder / "frames"
        cmd = [SEED_PY, "inference_cli.py", str(src), "--output", str(raw), "--output_format", "png", "--resolution", "1024", "--model_dir", "E:/AI/Models/SeedVR2", "--dit_model", args.seedvr_model,
               "--batch_size", str(args.seedvr_batch), "--uniform_batch_size", "--color_correction", "lab", "--attention_mode", "sdpa", "--vae_encode_tiled", "--vae_decode_tiled", "--seed", "42", *(["--load_cap", str(args.max_frames)] if args.max_frames else [])]
        res = timed(folder, cmd, cwd=SEED_DIR)
        pngs = sorted(raw.rglob("*.png")) if raw.is_dir() else []
        if res["exit"] == 0 and pngs:                                     # the CLI's own mp4 writer is OpenCV's mp4v at a low bit rate: the pictures are encoded here at the same quality as the other versions
            picture = folder / "picture.mp4"
            subprocess.run([ffmpeg(), "-y", "-v", "error", "-framerate", "25", "-i", str(pngs[0].parent / (pngs[0].name.rsplit("_", 1)[0] + "_%06d.png")), "-c:v", "libx264", "-preset", "medium", "-crf", "4", "-pix_fmt", "yuv420p", str(picture)], check=True)
            with_sound(picture, src, out / "seedvr2_x2.mp4")
    return 0


if __name__ == "__main__":
    sys.exit(main())
