"""The same-size enhancement compared: none / (a) FlashVSR+ at 2x brought back to the input size / (b) FlashVSR+ at 1x, on the approved clip and on the Lilith clip; and the ways of scaling (a) down.

    python same_size_report.py DIR            (DIR is the folder of enhance_same_size.py: <name>_a_x2_fit.mp4, <name>_b_native_1x.mp4 and the lossless working files <...>.big.mp4)

Measures for each clip and variant: the seconds of the job, the size, frames and duration, the face sharpness (tests/motion_matrix.py's measure), the sharpness of eyes / centre / hair normalised to 256 px, PSNR and SSIM
against the input; for the ways of scaling the working file down (ffmpeg's scaler flags) the same, and the PSNR against the `area` scaling (which cannot ring) as a measure of how far each goes from it.
Writes report.json, report.md and review.html (none / a / b side by side, synchronised, a notes box, Publish; the name of each test in the title)."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
INPUTS = {"20261114": ("Váradi Endre (clip approvata)", Path("E:/AI/Output/second_journalist/20261114/video.mp4")),
          "ltx25t_12": ("Presentatrice, ltx25t_12", Path("E:/AI/Output/audio_remux/20261114/after/ltx25t_12.mp4"))}
FLAGS = {"lanczos": "lanczos+accurate_rnd+full_chroma_int", "bicubic": "bicubic+accurate_rnd+full_chroma_int", "area": "area+accurate_rnd+full_chroma_int", "spline": "spline+accurate_rnd+full_chroma_int"}


def run_metrics(clips: dict, ref: Path) -> dict:
    out = subprocess.run([sys.executable, str(HERE / "metrics.py"), "--ref", str(ref), *map(str, clips.values())], capture_output=True, text=True).stdout
    res = {}
    for line in out.splitlines():
        for name, path in clips.items():
            if line.startswith(str(path) + " "):
                res[name] = json.loads(line[len(str(path)) + 1:])
    return res


def main() -> int:
    root = Path(sys.argv[1])
    seconds = json.loads((root / "result.json").read_text(encoding="utf-8"))
    report = {}
    for name, (title, source) in INPUTS.items():
        big = root / f"{name}_a_x2_fit.mp4.big.mp4"
        variants = {"none": source, "a_x2_fit": root / f"{name}_a_x2_fit.mp4", "b_native_1x": root / f"{name}_b_native_1x.mp4"}
        scaled = {}
        if big.is_file():
            for label, flags in FLAGS.items():
                out = root / f"{name}_scale_{label}.mp4"
                subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(big), "-vf", f"scale=896:512:flags={flags},format=yuv420p", "-c:v", "libx264", "-preset", "medium", "-crf", "4", "-pix_fmt", "yuv420p", "-an", str(out)], check=True)
                scaled[f"scale_{label}"] = out
        m = run_metrics({**variants, **scaled}, source)
        area = run_metrics({k: v for k, v in scaled.items()}, scaled["scale_area"]) if "scale_area" in scaled else {}
        rows = {}
        for k in list(variants) + list(scaled):
            r = m.get(k, {})
            rows[k] = {"seconds": (seconds.get(f"{name}/{k}") or {}).get("seconds"), "face_sharpness": r.get("face_sharpness"), "regions": r.get("regions"), "psnr_db": r.get("psnr_db"), "ssim": r.get("ssim"),
                       "psnr_vs_area_db": area.get(k, {}).get("psnr_db")}
        report[name] = rows
    (root / "report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    lines = ["| clip | variant | job seconds | face sharpness | eyes / centre / hair | PSNR / SSIM vs input | PSNR vs `area` scaling |", "|---|---|---|---|---|---|---|"]
    for name, rows in report.items():
        for k, r in rows.items():
            g = r["regions"] or {}
            lines.append(f"| {name} | {k} | {r['seconds'] or '-'} | {r['face_sharpness']} | {g.get('eyes')} / {g.get('centre')} / {g.get('hair')} | {('%s / %s' % (r['psnr_db'], r['ssim'])) if r['psnr_db'] else '-'} | {r['psnr_vs_area_db'] or '-'} |")
    (root / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    import review_page
    vids = root / "review" / "videos"
    vids.mkdir(parents=True, exist_ok=True)
    page = []
    for name, (title, source) in INPUTS.items():
        shutil.copyfile(source, vids / f"{name}_none.mp4")
        for k in ("a_x2_fit", "b_native_1x"):
            shutil.copyfile(root / f"{name}_{k}.mp4", vids / f"{name}_{k}.mp4")
        r = report[name]

        def nums(k):
            g = r[k]["regions"] or {}
            return {"secondi del lavoro": r[k]["seconds"] or "-", "nitidezza del volto": r[k]["face_sharpness"], "occhi / centro / capelli": f"{g.get('eyes')} / {g.get('centre')} / {g.get('hair')}",
                    "PSNR / SSIM contro l'originale": f"{r[k]['psnr_db']} / {r[k]['ssim']}" if r[k]["psnr_db"] else "-"}
        page.append({"id": f"{name}_all", "title": f"{title}: none / a_x2_fit / b_native_1x (tutte 896x512)", "what": "none: la clip com'è. a_x2_fit: FlashVSR+ Turbo lavora al doppio e il risultato è riportato a 896x512 con Lanczos, crf 4. b_native_1x: FlashVSR+ Turbo restaura direttamente a 896x512 (scala 1), crf 4. Stesse dimensioni, stessi fotogrammi, stessa durata.",
                     "look": "Blocchettatura tolta? Dettaglio ritrovato (occhi, ciglia, capelli)? Aloni attorno ai contorni (ringing)? Pelle plastica?", "numbers": {},
                     "videos": [{"label": "none", "src": f"videos/{name}_none.mp4"}, {"label": "a_x2_fit", "src": f"videos/{name}_a_x2_fit.mp4"}, {"label": "b_native_1x", "src": f"videos/{name}_b_native_1x.mp4"}]})
        for k in ("none", "a_x2_fit", "b_native_1x"):
            page.append({"id": f"{name}_{k}", "title": f"{title}: {k}", "what": f"Test {name}/{k}.", "look": "", "numbers": nums(k), "videos": [{"label": k, "src": f"videos/{name}_{k}.mp4"}]})
    review_page.build(root / "review" / "review.html", "Miglioramento a dimensioni invariate: none / (a) / (b)", "Migliorare non vuol dire ingrandire: la clip finale ha le dimensioni, i fotogrammi e la durata della clip generata. Le tre versioni, sincronizzate; il nome della prova è nel titolo. "
                      "Finché non scegli, il predefinito è (a). Scrivi una nota solo dove qualcosa non va.", page, "stessa-dimensione-segnalate.md")
    print(root / "review" / "review.html")
    return 0


if __name__ == "__main__":
    sys.exit(main())
