"""Gathers the results of the cells of a speed study into one table (markdown) and one JSON.

    python summarize.py DIR [--metrics metrics.json]

For each cell: how many runs, wall seconds (run 1 = cold, median of the others = warm, min-max of all), the transformer forward seconds, the transformer build seconds, the peak VRAM of the process
(torch counter) and of the GPU (`nvidia-smi`, includes what other programs hold), and, if a metrics file has them, PSNR/SSIM against the baseline clip and the face sharpness."""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--metrics")
    args = ap.parse_args()
    root = Path(args.dir)
    metrics = json.loads(Path(args.metrics).read_text(encoding="utf-8")) if args.metrics else {}
    rows = {}
    for cell in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_") and (p / "run1").is_dir()):
        runs = [json.loads((r / "result.json").read_text(encoding="utf-8")) for r in sorted(cell.glob("run*")) if (r / "result.json").is_file()]
        ok = [r for r in runs if r["ok"]]
        row = {"runs": len(runs), "ok": len(ok), "env": runs[0].get("env"), "teacache": runs[0].get("teacache")}
        if ok:
            walls = [r["wall_s"] for r in ok]
            row.update(cold_s=ok[0]["wall_s"], warm_s=med([r["wall_s"] for r in ok[1:]]) if len(ok) > 1 else None, min_s=min(walls), max_s=max(walls), median_s=med(walls),
                       forward_s=med([r.get("forward_s") for r in ok]), build_s=med([r.get("build_s") for r in ok]), peak_alloc_gib=max((r.get("peak_alloc_gib") or 0) for r in ok),
                       gpu_peak_mib=max(r.get("nvsmi_peak_mib") or 0 for r in ok), versions=ok[0].get("versions", ""))
        else:
            row["error"] = (runs[0].get("error_tail") or "")[-400:] if runs else ""
        rows[cell.name] = row
    base, base_min, base_fw = rows.get("base", {}).get("median_s"), rows.get("base", {}).get("min_s"), rows.get("base", {}).get("forward_s")
    for name, row in rows.items():
        if base and row.get("median_s"):
            row["speedup_vs_base"] = round(base / row["median_s"], 2)
            row["speedup_min_vs_base_min"] = round(base_min / row["min_s"], 2)
        if base_fw and row.get("forward_s"):
            row["forward_speedup"] = round(base_fw / row["forward_s"], 2)
        row.update({k: v for k, v in metrics.get(name, {}).items()})
    (root / "summary.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
    cols = ["runs", "cold_s", "warm_s", "min_s", "max_s", "median_s", "speedup_vs_base", "speedup_min_vs_base_min", "forward_s", "forward_speedup", "build_s", "peak_alloc_gib", "gpu_peak_mib", "psnr_db", "ssim", "face_sharpness"]
    print("| cell | " + " | ".join(cols) + " |")
    print("|" + "---|" * (len(cols) + 1))
    for name, row in rows.items():
        print(f"| {name} | " + " | ".join(str(row.get(c, "")) if row.get(c) is not None else "" for c in cols) + (" | FAILED" if not row["ok"] else " |"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
