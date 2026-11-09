"""Times every usable acceleration provider on synthetic frames and on frames you supply, and writes a JSON report.

    python -m voice.enhance_bench                          synthetic frames, report in ./enhance-bench.json
    python -m voice.enhance_bench --video clip.mp4         also the frames of a video (the first --count of them), with a quality number
    python -m voice.enhance_bench --out report.json --height 480 --fps 25 --count 60 --gpu 0 --rtx-video-path C:\\path\\to\\sdk.dll

For each provider it records resolution in and out, milliseconds per frame (mean, 95th percentile, worst), whether that fits the frame budget of the
rate asked for, and the GPU memory in use before and after (when readable). With a video it also records a quality number against the original:
PSNR of the upscaled picture (the video's frames are shrunk by the scale, then restored) and of an interpolated frame (the middle one of three
frames, rebuilt from the outer two). PSNR does not say whether a face stayed faithful: look at the pictures. `--save-dir` writes them.

It starts no avatar worker and loads no model. A provider that is not usable here is listed with its reason, never skipped silently. This is the
tool for the NVIDIA experiment of docs/voice/acceleration.md: the same report from the real SDK provider, once it exists, decides whether to keep it."""
from __future__ import annotations

import argparse
import json
import math
import platform
import sys
import time
from pathlib import Path

import voice  # noqa: F401 (puts the voice folder on sys.path)
from callbridge.enhance import capability, providers, rtx_video  # noqa: F401
from callbridge.enhance.settings import PROVIDER_SCALES, EnhanceSettings

SYNTHETIC_ASPECT = 4 / 3


def synthetic_frames(width: int, height: int, count: int):
    """A moving test picture: a gradient with a face-like disc and fine stripes, so that resizing and blending have detail to lose."""
    import cv2
    import numpy as np
    ys, xs = np.mgrid[0:height, 0:width]
    base = np.stack([(xs * 255 // max(1, width - 1)), (ys * 255 // max(1, height - 1)), ((xs + ys) * 255 // max(1, width + height - 2))], axis=2).astype(np.uint8)
    base[:: max(2, height // 60), :] //= 2
    out = []
    for i in range(count):
        img = base.copy()
        cx = int(width * (0.35 + 0.3 * (i % 30) / 30))
        cv2.circle(img, (cx, height // 2), height // 4, (150, 190, 230), -1)
        cv2.circle(img, (cx - height // 12, height // 2 - height // 20), max(2, height // 40), (30, 30, 30), -1)
        cv2.circle(img, (cx + height // 12, height // 2 - height // 20), max(2, height // 40), (30, 30, 30), -1)
        out.append(img)
    return out


def video_frames(path: str, count: int):
    import cv2
    cap = cv2.VideoCapture(path)
    frames = []
    try:
        while len(frames) < count:
            ok, frame = cap.read()
            if not ok:
                break
            frames.append(frame)
    finally:
        cap.release()
    if not frames:
        raise SystemExit(f"no frames could be read from {path}")
    return frames


def fit(img, height: int):
    """`img` scaled to `height` rows with an even width."""
    import cv2
    h, w = img.shape[:2]
    if h == height:
        return img
    nw = int(round(w * height / h / 2)) * 2
    return cv2.resize(img, (nw, height), interpolation=cv2.INTER_AREA if height < h else cv2.INTER_LANCZOS4)


def psnr(a, b) -> float:
    import numpy as np
    mse = float(np.mean((a.astype(np.float64) - b.astype(np.float64)) ** 2))
    return 99.0 if mse == 0 else round(10 * math.log10(255.0 * 255.0 / mse), 2)


def stats(ms: list[float]) -> dict:
    ordered = sorted(ms)
    return {"mean": round(sum(ms) / len(ms), 3), "p95": round(ordered[min(len(ordered) - 1, int(0.95 * len(ordered)))], 3), "worst": round(ordered[-1], 3)}


def _time(fn, items, clock) -> list[float]:
    for item in items[:2]:                                    # the first calls load code and warm caches
        fn(item)
    out = []
    for item in items:
        t0 = clock()
        fn(item)
        out.append((clock() - t0) * 1000.0)
    return out


def _memory(reader, index: int):
    info = reader(index)
    return info.memory_used_mb if info.vendor == "nvidia" else None


def bench_upscale(name: str, scale: float, frames, target_h: int, fps: int, gpu_reader, gpu_index: int, settings, clock=time.perf_counter, save=None) -> dict:
    provider = providers.make("upscale", name, settings)
    small_h = max(2, int(round(target_h / scale / 2)) * 2)
    small = [fit(f, small_h) for f in frames]
    out_w, out_h = fit(frames[0], target_h).shape[1], target_h
    before = _memory(gpu_reader, gpu_index)
    ms = _time(lambda img: provider.upscale(img, out_w, out_h), small, clock)
    after = _memory(gpu_reader, gpu_index)
    restored = provider.upscale(small[0], out_w, out_h)
    reference = fit(frames[0], target_h)
    if save:
        import cv2
        cv2.imwrite(str(save / f"upscale-{name}-x{scale:g}.png"), restored)
        cv2.imwrite(str(save / "reference.png"), reference)
    budget = 1000.0 / fps
    return {"stage": "upscale", "provider": name, "scale": scale, "in": [small[0].shape[1], small[0].shape[0]], "out": [out_w, out_h], "ms_per_frame": stats(ms),
            "fits_budget": stats(ms)["p95"] <= budget, "budget_ms": round(budget, 3), "gpu_memory_mb": {"before": before, "after": after},
            "psnr_db": psnr(restored, reference) if restored.shape == reference.shape else None}


def bench_interpolate(name: str, factor: int, frames, target_h: int, fps: int, gpu_reader, gpu_index: int, settings, clock=time.perf_counter, save=None) -> dict:
    provider = providers.make("interpolate", name, settings)
    pics = [fit(f, target_h) for f in frames]
    pairs = list(zip(pics, pics[1:])) or [(pics[0], pics[0])]
    alphas = [(j + 1) / factor for j in range(factor - 1)]
    before = _memory(gpu_reader, gpu_index)
    ms = _time(lambda p: provider.interpolate(p[0], p[1], alphas), pairs, clock)
    after = _memory(gpu_reader, gpu_index)
    quality = None
    if factor == 2 and len(pics) >= 3:
        middle = provider.interpolate(pics[0], pics[2], [0.5])[0]
        quality = psnr(middle, pics[1])
        if save:
            import cv2
            cv2.imwrite(str(save / f"interpolate-{name}-middle.png"), middle)
    budget = 1000.0 * factor / fps                           # the time to make the in-between frames of one input frame
    return {"stage": "interpolate", "provider": name, "factor": factor, "in": [pics[0].shape[1], pics[0].shape[0]], "ms_per_input_frame": stats(ms),
            "fits_budget": stats(ms)["p95"] <= budget, "budget_ms": round(budget, 3), "gpu_memory_mb": {"before": before, "after": after}, "psnr_db_vs_real_middle_frame": quality}


def bench_pipeline(frames, target_h: int, fps: int, settings: EnhanceSettings, report: dict, clock=time.perf_counter) -> dict:
    """Both stages through the real enhancer: what one worker frame costs from arrival to the last output frame, conversions included."""
    from callbridge.avatar_client import Frame
    from callbridge.enhance.pipeline import Enhancer
    import cv2
    enh = Enhancer(lambda f: None, pacing=False)
    plan = enh.configure(settings, report, fps, target_h)
    if not plan.active:
        return {"stage": "pipeline", "settings": settings.as_dict(), "skipped": "no stage of these settings can run here", "status": enh.status()}
    small = [fit(f, plan.worker_height or target_h) for f in frames]
    wire = [Frame(p.shape[1], p.shape[0], cv2.cvtColor(p, cv2.COLOR_BGR2YUV_I420).tobytes()) for p in small]
    ms = _time(enh.feed, wire, clock)
    st = enh.status()
    budget = 1000.0 / (plan.worker_fps or fps)
    return {"stage": "pipeline", "settings": settings.as_dict(), "plan": plan.as_dict(), "ms_per_worker_frame": stats(ms), "budget_ms": round(budget, 3),
            "fits_budget": stats(ms)["p95"] <= budget, "status": st}


def run(frames, target_h: int, fps: int, count: int, report: dict, gpu_reader, gpu_index: int = 0, settings: EnhanceSettings | None = None, save=None) -> dict:
    settings = settings or EnhanceSettings()
    results, skipped = [], []
    for stage, table in (("upscale", report["upscale"]), ("interpolate", report["interpolate"])):
        for name, entry in table.items():
            if name == "none":
                continue
            if not entry["available"]:
                skipped.append({"stage": stage, "provider": name, "code": entry["code"], "reason": entry["reason"]})
                continue
            try:
                if stage == "upscale":
                    results += [bench_upscale(name, s, frames, target_h, fps, gpu_reader, gpu_index, settings, save=save) for s in PROVIDER_SCALES[name]]
                else:
                    results += [bench_interpolate(name, f, frames, target_h, fps, gpu_reader, gpu_index, settings, save=save) for f in (2, 3)]
            except Exception as e:                                           # noqa: BLE001 (one provider failing is a result, not the end)
                results.append({"stage": stage, "provider": name, "error": f"{type(e).__name__}: {e}"})
    both = EnhanceSettings(upscale="basic", scale=2.0, interpolate="basic", factor=2)
    pipeline = bench_pipeline(frames, target_h, fps, both, report)
    return {"results": results, "skipped": skipped, "pipeline_basic_2x_2x": pipeline}


def build_report(inputs: list[dict], runs: list[dict], report: dict, args) -> dict:
    return {"created": time.strftime("%Y-%m-%dT%H:%M:%S"), "python": sys.version.split()[0], "os": platform.platform(), "gpu": report["gpu"],
            "target": {"height": args.height, "fps": args.fps}, "inputs": inputs, "runs": runs, "capabilities": {k: report[k] for k in ("upscale", "interpolate")},
            "dlss_experiment": {"status": "not run here", "why": "DLSS is a game-engine technology (D3D12 or Vulkan with motion vectors, through NVIDIA Streamline/NGX) and "
                                "cannot be driven from Python; see docs/voice/acceleration.md for the separate native harness and the decision rule."}}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m voice.enhance_bench", description=__doc__.split("\n\n")[0])
    ap.add_argument("--video", help="a video file whose frames are also timed (and scored)")
    ap.add_argument("--out", default="enhance-bench.json")
    ap.add_argument("--height", type=int, default=480, help="the height the outputs get")
    ap.add_argument("--fps", type=int, default=25, help="the rate the outputs get")
    ap.add_argument("--count", type=int, default=60, help="frames timed per input")
    ap.add_argument("--gpu", type=int, default=0)
    ap.add_argument("--rtx-video-path", default="", help="the full path of the RTX Video SDK's DLL")
    ap.add_argument("--save-dir", help="write the upscaled and interpolated pictures here")
    args = ap.parse_args(argv)
    save = Path(args.save_dir) if args.save_dir else None
    if save:
        save.mkdir(parents=True, exist_ok=True)
    report = capability.report(capability.read_gpu(args.gpu), dll_path=args.rtx_video_path)
    settings = EnhanceSettings(rtx_video_path=args.rtx_video_path)
    inputs = [("synthetic", synthetic_frames(int(args.height * SYNTHETIC_ASPECT) // 2 * 2, args.height, max(args.count, 3)))]
    if args.video:
        inputs.append(("video", video_frames(args.video, max(args.count, 3))))
    runs, described = [], []
    for label, frames in inputs:
        described.append({"name": label, "frames": len(frames), "size": [frames[0].shape[1], frames[0].shape[0]]})
        runs.append({"input": label, **run(frames, args.height, args.fps, args.count, report, capability.read_gpu, args.gpu, settings, save)})
    Path(args.out).write_text(json.dumps(build_report(described, runs, report, args), indent=2) + "\n", encoding="utf-8")
    print(f"report written to {args.out}")
    for r in runs[0]["results"]:
        key = "ms_per_frame" if "ms_per_frame" in r else "ms_per_input_frame"
        if key in r:
            print(f"  {r['stage']:<11} {r['provider']:<10} {r.get('scale', r.get('factor'))!s:<4} mean {r[key]['mean']:>8.2f} ms   p95 {r[key]['p95']:>8.2f} ms   fits budget: {r['fits_budget']}")
    for s in runs[0]["skipped"]:
        print(f"  {s['stage']:<11} {s['provider']:<10} not usable: {s['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
