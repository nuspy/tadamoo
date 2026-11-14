"""The final report of the TeaCache 0.03 x attention matrix: generation seconds, enhancement seconds, total seconds, the picture of the enhanced result (FlashVSR+ x2 Turbo) and of the unenhanced clip.

    python tea_report.py DIR ENHANCED_DIR

DIR holds the cells (t3_*, each run1..run4, from queue_tea.py), ENHANCED_DIR the enlarged clips and result.json of enhance_check.py (the clips of run 1 of each cell and of the baseline `q_base`).
Ranking: by the total seconds of the final result (generation median + enhancement of the run-1 clip); the columns also give the generation alone, the transformer seconds, and for the enhanced and the
unenhanced picture the PSNR / SSIM against the TeaCache-alone clip (t3_cudnn) and against the baseline, and the sharpness (the face measure and the three regions normalised to 256 px). Writes ranking.md,
ranking.json and review.html (two rows for each cell, the final one first, the unenhanced one second; the name of the cell is in the title)."""
from __future__ import annotations

import json
import shutil
import statistics
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from quiet_report import TEA, stats  # noqa: E402


def measures(clips: dict, ref: Path) -> dict:
    out = subprocess.run([sys.executable, str(HERE / "metrics.py"), "--ref", str(ref), *map(str, clips.values())], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        for name, path in clips.items():
            if line.startswith(str(path) + " "):
                res[name] = json.loads(line[len(str(path)) + 1:])
    return res


def main() -> int:
    root, big = Path(sys.argv[1]), Path(sys.argv[2])
    enh = json.loads((big / "result.json").read_text(encoding="utf-8"))
    cells = {n: stats(root / n) for n in TEA if (root / n).is_dir() and list((root / n).glob("run*/result.json"))}
    base_clip = root.parent / "quiet" / "q_base" / "run1" / "take.mp4"
    raw = {n: root / n / "run1" / "take.mp4" for n in cells}
    raw["base"] = base_clip
    final = {}
    for name in list(cells) + ["base"]:
        final[name] = big / f"{'q_base' if name == 'base' else name}_run1_take_enh.mp4"
    enh_by = {}
    for path, info in enh.items():
        p = Path(path)
        enh_by[f"{p.parent.parent.name}/{p.parent.name}"] = info
    seconds = {}
    for name in cells:
        info = next((v for k, v in enh.items() if f"\\{name}\\run1\\" in k or f"/{name}/run1/" in k), None)
        seconds[name] = info["job_seconds"] if info else None
    raw_vs_tea, raw_vs_base = measures(raw, raw["t3_cudnn"]), measures(raw, base_clip)
    fin_vs_tea, fin_vs_base = measures(final, final["t3_cudnn"]), measures(final, final["base"])
    ranking = {}
    for name, c in cells.items():
        e = seconds[name]
        ranking[name] = {"generation_s": c["median"], "generation_min": c["min"], "generation_max": c["max"], "transformer_s": c["forward_median"], "enhance_s": e, "total_s": round(c["median"] + e, 1) if e else None,
                         "busy_runs": c["busy_runs"], "runs": c["runs"], "raw": {"vs_tea": raw_vs_tea.get(name, {}), "vs_base": raw_vs_base.get(name, {})}, "final": {"vs_tea": fin_vs_tea.get(name, {}), "vs_base": fin_vs_base.get(name, {})}}
    order = sorted(ranking, key=lambda n: ranking[n]["total_s"] or 1e9)
    (root / "ranking.json").write_text(json.dumps({"order": order, "cells": ranking}, indent=1), encoding="utf-8")

    def fmt(m):
        return "-" if not m or "psnr_db" not in m else f"{m['psnr_db']} / {m['ssim']}"
    head = ["| rank | test | generation s min / median / max | transformer s | enhancement s | total s | PSNR/SSIM enhanced vs TeaCache alone | vs base | sharpness enhanced (face; eyes/centre/hair) | PSNR/SSIM unenhanced vs TeaCache alone | vs base | sharpness unenhanced (face) |", "|" + "---|" * 12]
    for i, n in enumerate(order, 1):
        r = ranking[n]
        f = fin_vs_tea.get(n, {})
        reg = f.get("regions", {})
        head.append(f"| {i} | {n} | {r['generation_min']} / {r['generation_s']} / {r['generation_max']} | {r['transformer_s']} | {r['enhance_s']} | {r['total_s']} | {fmt(r['final']['vs_tea'])} | {fmt(r['final']['vs_base'])} | "
                    f"{f.get('face_sharpness')}; {reg.get('eyes')}/{reg.get('centre')}/{reg.get('hair')} | {fmt(r['raw']['vs_tea'])} | {fmt(r['raw']['vs_base'])} | {raw_vs_tea.get(n, {}).get('face_sharpness')} |")
    (root / "ranking.md").write_text("\n".join(head) + "\n", encoding="utf-8")
    print("\n".join(head))
    import review_page
    vids = root / "review" / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    for n in list(cells) + ["base"]:
        shutil.copyfile(raw[n], vids / f"{n}.mp4")
        shutil.copyfile(final[n], vids / f"{n}_enh.mp4")
    rows = []
    for n in order:
        r, f = ranking[n], fin_vs_tea.get(n, {})
        title = f"{n}: {TEA[n][0]}"
        rows.append({"id": f"{n}_final", "title": f"{title} (risultato finale, x2 Turbo)", "what": TEA[n][1] + ". FlashVSR+ x2 Turbo, 1792x1024.", "look": "A sinistra TeaCache da solo (cuDNN), a destra questa configurazione, entrambe dopo il miglioratore.",
                     "numbers": {"test": n, "secondi di generazione (min / mediana / max)": f"{r['generation_min']} / {r['generation_s']} / {r['generation_max']}", "secondi del transformer": r["transformer_s"], "secondi del miglioratore": r["enhance_s"],
                                 "secondi in tutto": r["total_s"], "PSNR / SSIM contro TeaCache da solo (finale)": fmt(r["final"]["vs_tea"]), "PSNR / SSIM contro la base (finale)": fmt(r["final"]["vs_base"]),
                                 "nitidezza finale (volto; occhi/centro/capelli)": f"{f.get('face_sharpness')}; {f.get('regions', {}).get('eyes')}/{f.get('regions', {}).get('centre')}/{f.get('regions', {}).get('hair')}"},
                     "videos": [{"label": "t3_cudnn (finale)", "src": "videos/t3_cudnn_enh.mp4"}, {"label": f"{n} (finale)", "src": f"videos/{n}_enh.mp4"}]})
        rows.append({"id": f"{n}_raw", "title": f"{title} (senza miglioratore)", "what": TEA[n][1] + ". Il file come esce da LTX, 896x512.", "look": "Come sopra, prima del miglioratore.",
                     "numbers": {"test": n, "PSNR / SSIM contro TeaCache da solo": fmt(r["raw"]["vs_tea"]), "PSNR / SSIM contro la base": fmt(r["raw"]["vs_base"]), "nitidezza del volto": raw_vs_tea.get(n, {}).get("face_sharpness")},
                     "videos": [{"label": "t3_cudnn", "src": "videos/t3_cudnn.mp4"}, {"label": n, "src": f"videos/{n}.mp4"}]})
    review_page.build(root / "review" / "review.html", "TeaCache 0,03 con ogni attenzione: il risultato finale", "Stessa foto, stessa voce, seme e prompt, 12 passi, salvataggio crf 4, TeaCache di LTX Studio a 0,03 e guida normale; cambia solo l'attenzione. "
                      "Per ogni prova due righe: il risultato finale (dopo FlashVSR+ x2 Turbo) e la clip senza miglioratore. Il nome della prova è nel titolo: citalo nelle note. Giudichi tu.", rows, "tea-attenzioni-segnalate.md")
    print(root / "review" / "review.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
