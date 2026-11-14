"""The ranking by pure speed of the quiet round (queue_quiet.py): the seconds of each cell, its difference of picture from the baseline and from the approved configuration, and the review page.

    python quiet_report.py DIR [--page]       (DIR holds the cells, each with run1..run4)

Total job seconds are the wall time of the whole process (min, median, max of the runs), the transformer seconds the time spent inside the transformer's forward (the passes that a cache skips are not counted).
A run is marked busy when the PC's CPU was above 25 % before it started. The picture difference is PSNR, SSIM and the face sharpness (tests/motion_matrix.py's measure) of run 1 against the baseline clip and
against the approved clip (SageAttention 2 + first-block cache 0.10). With --page: the clips side by side, synchronised, with a notes box and the Publish button."""
from __future__ import annotations

import argparse
import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
QUIET = {
    "q_base": ("base", "LTX Studio's job as it is (cuDNN attention, 4 guided passes)"),
    "q_sage2": ("sage2", "SageAttention 2 alone"),
    "q_tea03": ("TeaCache 0.03", "LTX Studio's TeaCache, threshold 0.03, alone"),
    "q_tea02": ("TeaCache 0.02", "LTX Studio's TeaCache, threshold 0.02, alone"),
    "q_tea05": ("TeaCache 0.05", "LTX Studio's TeaCache, threshold 0.05, alone"),
    "q_g1": ("guider_one", "one guided pass per step (no negative, no STG, no modality), alone"),
    "q_s2_tea03": ("sage2 + TeaCache 0.03", "SageAttention 2 and LTX Studio's TeaCache 0.03"),
    "q_s2_g1": ("sage2 + guider_one", "SageAttention 2 and one guided pass"),
    "q_tea03_g1": ("TeaCache 0.03 + guider_one", "LTX Studio's TeaCache 0.03 and one guided pass"),
    "q_s2_tea03_g1": ("sage2 + TeaCache 0.03 + guider_one", "all three"),
    "q_s2_fb10": ("sage2 + first-block 0.10 (approved)", "the approved configuration"),
}
TEA = {
    "t3_cudnn": ("TeaCache 0.03 + cuDNN SDPA (TeaCache alone)", "LTX Studio's TeaCache 0.03, the baseline attention (cuDNN SDPA), torch 2.14 nightly"),
    "t3_sage1": ("TeaCache 0.03 + SageAttention 1.0.6", "TeaCache 0.03 with SageAttention 1.0.6 (Triton), torch 2.14 nightly"),
    "t3_sage2": ("TeaCache 0.03 + SageAttention 2.2.0", "TeaCache 0.03 with SageAttention 2.2.0 (built here), torch 2.14 nightly"),
    "t3_fa4": ("TeaCache 0.03 + FlashAttention 4 4.0.0b39", "TeaCache 0.03 with FlashAttention 4 (CuTe DSL), torch 2.14 nightly"),
    "t3_efficient": ("TeaCache 0.03 + SDPA memory-efficient", "TeaCache 0.03 with PyTorch's memory-efficient SDPA, torch 2.14 nightly"),
    "t3_t211_cudnn": ("TeaCache 0.03 + cuDNN SDPA on torch 2.11", "TeaCache 0.03, cuDNN SDPA, torch 2.11 cu130 Python 3.11 (the environment of FlashAttention 2 and xformers)"),
    "t3_fa2": ("TeaCache 0.03 + FlashAttention 2 2.8.4", "TeaCache 0.03 with FlashAttention 2.8.4 (prebuilt sm120), torch 2.11"),
    "t3_xformers": ("TeaCache 0.03 + xformers 0.0.35", "TeaCache 0.03 with xformers 0.0.35 memory_efficient_attention, torch 2.11"),
}


NAMES: dict = {}


def stats(cell: Path) -> dict:
    runs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(cell.glob("run*/result.json"))]
    ok = [r for r in runs if r["ok"]]
    walls = [r["wall_s"] for r in ok]
    return {"runs": len(ok), "walls": walls, "min": min(walls), "median": round(statistics.median(walls), 1), "max": max(walls), "forward_median": round(statistics.median([r["forward_s"] for r in ok]), 1),
            "forward_min": min(r["forward_s"] for r in ok), "forward_max": max(r["forward_s"] for r in ok), "busy_runs": sum(1 for r in ok if (r.get("cpu_busy_before_pct") or 0) > 25), "cpu_before": [r.get("cpu_busy_before_pct") for r in ok],
            "peak_gib": max(r.get("peak_alloc_gib") or 0 for r in ok)}


def picture(clips: dict, ref: Path) -> dict:
    out = subprocess.run([sys.executable, str(HERE / "metrics.py"), "--no-regions", "--ref", str(ref), *map(str, clips.values())], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        for name, path in clips.items():
            if line.startswith(str(path)):
                res[name] = json.loads(line[len(str(path)):].strip())
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--page", action="store_true")
    ap.add_argument("--set", default="quiet", choices=("quiet", "tea"))
    ap.add_argument("--base", default="", help="the baseline clip (default: the q_base of the folder)")
    ap.add_argument("--ref", default="", help="the second reference clip: the approved one (quiet) or TeaCache alone (tea)")
    args = ap.parse_args()
    root = Path(args.dir)
    NAMES.clear()
    NAMES.update(TEA if args.set == "tea" else QUIET)
    cells = {name: stats(root / name) for name in NAMES if (root / name).is_dir() and list((root / name).glob("run*/result.json"))}
    clips = {name: root / name / "run1" / "take.mp4" for name in cells}
    ref_name = "t3_cudnn" if args.set == "tea" else "q_s2_fb10"
    base = Path(args.base) if args.base else clips["q_base"]
    approved = Path(args.ref) if args.ref else clips[ref_name]
    vs_base = picture({k: v for k, v in clips.items()}, base)
    vs_approved = picture({k: v for k, v in clips.items()}, approved)
    for name, c in cells.items():
        c["vs_base"], c["vs_approved"] = vs_base.get(name, {}), vs_approved.get(name, {})
        c["sharpness"] = c["vs_base"].get("face_sharpness")
    ranked = sorted(cells, key=lambda n: cells[n]["median"])
    base_median = cells["q_base"]["median"] if "q_base" in cells else json.loads((root.parent / "quiet" / "ranking.json").read_text(encoding="utf-8"))["cells"]["q_base"]["median"]
    (root / "ranking.json").write_text(json.dumps({"ranked": ranked, "cells": cells}, indent=1), encoding="utf-8")
    lines = ["| rank | configuration | runs | job s min / median / max | speed-up on the baseline median | transformer s (median) | PSNR / SSIM vs base | PSNR / SSIM vs approved | face sharpness (base 96.1) | busy runs |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, name in enumerate(ranked, 1):
        c = cells[name]
        b, a = c["vs_base"], c["vs_approved"]
        fmt = lambda m: "-" if not m or "psnr_db" not in m else (f"{m['psnr_db']} dB / {m['ssim']}" if m["psnr_db"] != float("inf") else "identical")
        lines.append(f"| {i} | {NAMES[name][0]} | {c['runs']} | {c['min']} / {c['median']} / {c['max']} | {base_median / c['median']:.2f} | {c['forward_median']} | {fmt(b)} | {fmt(a)} | {c['sharpness']} | {c['busy_runs']} |")
    (root / "ranking.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    if args.page:
        import review_page
        vids = root / "review" / "videos"
        vids.mkdir(parents=True, exist_ok=True)
        for name in cells:
            shutil.copyfile(clips[name], vids / f"{name}.mp4")
        rows = []
        for name in ranked:
            if name == ref_name:
                continue
            c, b, a = cells[name], cells[name]["vs_base"], cells[name]["vs_approved"]
            rows.append({"id": name, "title": f"{name}: {NAMES[name][0]}: {c['median']} s (riferimento {ref_name}: {cells[ref_name]['median']} s)", "what": NAMES[name][1],
                         "look": f"A sinistra il riferimento ({ref_name}), a destra questa ({name}): volto, bocca, sfarfallio, colore.",
                         "numbers": {"secondi del lavoro min / mediana / max": f"{c['min']} / {c['median']} / {c['max']}", "secondi del transformer (mediana)": c["forward_median"], "PSNR / SSIM contro la base": f"{b.get('psnr_db')} / {b.get('ssim')}",
                                     f"PSNR / SSIM contro {ref_name}": f"{a.get('psnr_db')} / {a.get('ssim')}", "nitidezza del volto (base 96,1)": c["sharpness"], "giri con il PC occupato": c["busy_runs"]},
                         "videos": [{"label": f"riferimento {ref_name}", "src": f"videos/{ref_name}.mp4"}, {"label": f"{name}: {NAMES[name][0]}", "src": f"videos/{name}.mp4"}]})
        review_page.build(root / "review" / "review.html", "Velocità pura: TeaCache 0,03 con ogni attenzione" if args.set == "tea" else "Velocità pura: le clip veloci contro l'approvata", "Stessa foto, stessa voce, stesso seme e prompt, 12 passi, salvataggio crf 4. Ogni riga: la clip approvata e la clip di quella configurazione, sincronizzate. "
                          "Giudichi tu quale tenere; scrivi una nota solo dove qualcosa non va.", rows, "velocita-pura-segnalate.md")
        print(root / "review" / "review.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
