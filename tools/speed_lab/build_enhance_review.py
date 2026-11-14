"""The review page of the enhancement comparison: the four versions of the winning clip side by side and played together, the numbers of each, a field for notes and a button that writes the .md of the rows with a note.

    python build_enhance_review.py DIR            (DIR has none.mp4, seedvr2_x2.mp4, vsr_x2.mp4, vsr_x15.mp4, metrics.json and a folder with result.json and stats for each)

Writes DIR/review.html; the videos are referenced from the same folder."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from review_page import build  # noqa: E402

VERSIONS = [("none", "a) Non migliorata", "896x512, come esce da LTX 2.5 (ltx25t_12), salvataggio crf 4."),
            ("seedvr2_x2", "b) SeedVR2 x2", "SeedVR2 7B (fp8, blocco 35 fp16) 2x: 1792x1024; blocchi di 57 fotogrammi, decodifica VAE a tasselli, immagini PNG poi x264 crf 4."),
            ("vsr_x2", "c) FlashVSR+ x2 (Turbo)", "FlashVSR+ Turbo 2x: 1792x1024; attenzione = kernel Triton a blocchi (non la maschera SDPA); salvato x264 crf 4. 166 fotogrammi su 169: gli ultimi 3 non sono prodotti."),
            ("vsr_x15", "d) FlashVSR+ x1,5 (Turbo)", "FlashVSR+ Turbo 1,5x: 1344x768; stesso kernel Triton; x264 crf 4. 166 fotogrammi su 169.")]


def main() -> int:
    root = Path(sys.argv[1])
    metrics = json.loads((root / "metrics.json").read_text(encoding="utf-8"))
    info = {}
    for vid, _, _ in VERSIONS:
        row = {}
        res = root / vid / "result.json"
        if res.is_file():
            r = json.loads(res.read_text(encoding="utf-8"))
            row.update(wall=r["wall_s"], gpu=r["nvsmi_peak_mib"])
        st = root / vid / "stats.json"
        if st.is_file():
            s = json.loads(st.read_text(encoding="utf-8"))
            row.update(inference=s["tempo_inferenza_s"], load=s["tempo_carico_s"], alloc=s["picco_vram_alloc_mb"])
        info[vid] = row

    def numbers(vid: str) -> dict:
        m = metrics[str(root / f"{vid}.mp4").replace("\\", "/")] if str(root / f"{vid}.mp4").replace("\\", "/") in metrics else next(v for k, v in metrics.items() if k.replace("\\", "/").endswith(f"/{vid}.mp4"))
        i = info[vid]
        out = {}
        if i:
            out["secondi totali del lavoro (processo intero)"] = i.get("wall")
            if "inference" in i:
                out["di cui inferenza / caricamento modello"] = f"{i['inference']} / {i['load']}"
            out["VRAM: picco della GPU (nvidia-smi, totale) / di PyTorch"] = f"{i.get('gpu')} MiB / {i.get('alloc', '-')} MiB"
        else:
            out["secondi / VRAM"] = "nessun lavoro: è il file di partenza"
        r = m["regions"]
        out["nitidezza (varianza del Laplaciano, regioni a 256 px): occhi / centro viso / capelli"] = f"{r['eyes']} / {r['centre']} / {r['hair']}"
        out["nitidezza del volto (metodo di motion_matrix, 832 px)"] = m["face_sharpness"]
        if "psnr_db" in m:
            out["PSNR / SSIM contro la non migliorata ingrandita (indicativo)"] = f"{m['psnr_db']} dB / {m['ssim']}"
        return out
    rows = [{"id": "four", "title": "Le quattro versioni insieme", "what": "Stessa ripresa, stessi 6,8 secondi di voce; le ingrandite sono mostrate alla stessa larghezza dalla pagina.",
             "look": "Occhi, ciglia, capelli, bocca: dettaglio vero o inventato? Sfarfallio da un fotogramma all'altro, artefatti ai bordi del volto.", "numbers": {},
             "videos": [{"label": t, "src": f"{v}.mp4"} for v, t, _ in VERSIONS]}]
    for vid, title, what in VERSIONS[1:]:
        rows.append({"id": vid, "title": title, "what": what, "look": "A sinistra la non migliorata, a destra la versione: dettaglio, finto dettaglio, sfarfallio.", "numbers": numbers(vid),
                     "videos": [{"label": VERSIONS[0][1], "src": "none.mp4"}, {"label": title, "src": f"{vid}.mp4"}]})
    rows.insert(1, {"id": "none", "title": VERSIONS[0][1], "what": VERSIONS[0][2], "look": "Il riferimento.", "numbers": numbers("none"), "videos": [{"label": VERSIONS[0][1], "src": "none.mp4"}]})
    intro = ("Le regioni della nitidezza sono ritagliate (occhi, centro del viso, capelli) e portate a 256 px di larghezza prima di misurare: nella non migliorata il ritaglio è più piccolo e viene ingrandito, "
             "perciò i numeri più alti delle altre non dicono da soli se il dettaglio è vero. Giudichi tu; scrivi una nota solo dove qualcosa non va. GPU RTX 5090, limite di potenza 600 W.")
    print(build(root / "review.html", "Miglioratori su ltx25t_12: quattro versioni", intro, rows, "miglioratori-segnalate.md"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
