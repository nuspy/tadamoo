"""The clips of the presenters with the ORIGINAL recording as their soundtrack, and how well it is in step with the picture.

    python tools/audio_remux.py --out FOLDER

A clip made by LTX 2.5 audio-to-video carries as its sound the recording the model was given; the newscast gives the model a tamed copy (quieter, flatter, low-passed) so that the mouth moves
calmly, and the soundtrack must be the original. For each clip this script: measures the loudness (EBU R128) of the soundtrack the clip has and of the original; measures the offset of the original
against the tamed copy (envelope cross-correlation) and the lag of the mouth opening against each of them; puts the original under the picture with the picture stream copied (no re-encode of the picture),
the audio at the same sample rate as the original, cut at the end of the picture; reads the result back (durations, start times, the offset of the decoded audio against the original). It writes
review.html (before and after, the numbers, a field for notes, the Publish button) and result.json in FOLDER."""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "tools" / "speed_lab"))

RENDER = Path("E:/AI/Output/newscast/hu_render_2026-11-12")
MATRIX = Path("E:/AI/Output/motion_matrix/20261114-run1")
MALE = Path("E:/AI/Output/second_journalist/20261114")
CLIPS = [
    {"id": "ltx25t_12", "title": "Presentatrice, LTX 2.5, 12 passi, salvataggio alto (ltx25t_12)", "video": MATRIX / "raw" / "ltx25t_12.mp4", "original": RENDER / "voice" / "00.wav", "heard": MATRIX / "heard" / "ltx25t_12.wav"},
    {"id": "ltx25t", "title": "Presentatrice, LTX 2.5, 8 passi, salvataggio di LTX Studio (ltx25t)", "video": MATRIX / "raw" / "ltx25t.mp4", "original": RENDER / "voice" / "00.wav", "heard": MATRIX / "heard" / "ltx25t.wav"},
    {"id": "endre", "title": "Váradi Endre (clip approvata)", "video": MALE / "video.mp4", "original": MALE / "audio.wav", "heard": MALE / "heard.wav"},
]


def ff(name: str) -> str:
    return shutil.which(name) or name


def lufs(path: Path) -> float:
    out = subprocess.run([ff("ffmpeg"), "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], capture_output=True, text=True).stderr
    for line in out.splitlines()[::-1]:
        if line.strip().startswith("I:"):
            return float(line.split()[1])
    return float("nan")


def probe(path: Path) -> dict:
    data = json.loads(subprocess.run([ff("ffprobe"), "-v", "error", "-show_entries", "stream=codec_type,codec_name,start_time,duration,nb_frames,sample_rate,r_frame_rate,width,height:format=duration", "-of", "json", str(path)],
                                     capture_output=True, text=True).stdout)
    out = {"format_s": float(data["format"]["duration"])}
    for s in data["streams"]:
        out[s["codec_type"]] = {k: s.get(k) for k in ("codec_name", "start_time", "duration", "nb_frames", "sample_rate", "r_frame_rate", "width", "height") if s.get(k) is not None}
    return out


def decode(path: Path, rate: int = 24000) -> np.ndarray:
    raw = subprocess.run([ff("ffmpeg"), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"], capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float64) / 32768.0


def envelope(x: np.ndarray, rate: int, window_s: float) -> np.ndarray:
    n = int(rate * window_s)
    k = len(x) // n
    return np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(axis=1) + 1e-12)


def lag_of(a: np.ndarray, b: np.ndarray, step_s: float, max_s: float) -> float:
    """How late `b` is against `a` (seconds; positive: b comes after a), from the cross-correlation of the two log-envelopes at their best, refined by a parabola through the peak."""
    la, lb = np.log(a + 1e-4), np.log(b + 1e-4)
    la, lb = la - la.mean(), lb - lb.mean()
    n = min(len(la), len(lb))
    la, lb = la[:n], lb[:n]
    reach = int(max_s / step_s)
    lags = list(range(-reach, reach + 1))
    scores = []
    for lag in lags:
        x, y = (la[:n - lag], lb[lag:]) if lag >= 0 else (la[-lag:], lb[:n + lag])
        scores.append(float((x * y).sum() / (np.sqrt((x * x).sum() * (y * y).sum()) + 1e-9)))
    i = int(np.argmax(scores))
    frac = 0.0
    if 0 < i < len(scores) - 1:
        d = scores[i - 1] - 2 * scores[i] + scores[i + 1]
        frac = 0.5 * (scores[i - 1] - scores[i + 1]) / d if d else 0.0
    return (lags[i] + frac) * step_s


def mouth_lag(video: Path, audio: np.ndarray, rate: int) -> float:
    """The lag of the mouth opening (inner lips, each frame) against the energy of the audio per frame, in seconds (positive: the mouth follows the sound)."""
    import motion_audio as ma
    ap = np.asarray(ma.aperture_series(video), dtype=float)
    env = envelope(audio, rate, 1 / 25)
    n = min(len(ap), len(env))
    return lag_of(env[:n], ap[:n], 1 / 25, 0.3) if n > 10 else float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    import psutil                                                          # a light job: below normal priority and four cores, so that it does not disturb what runs on the GPU meanwhile
    me = psutil.Process()
    me.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    me.cpu_affinity(list(range(max(0, psutil.cpu_count() - 4), psutil.cpu_count())))
    out = Path(args.out)
    (out / "before").mkdir(parents=True, exist_ok=True)
    (out / "after").mkdir(exist_ok=True)
    import review_page
    results, rows = {}, []
    for clip in CLIPS:
        cid = clip["id"]
        before = out / "before" / f"{cid}.mp4"
        after = out / "after" / f"{cid}.mp4"
        shutil.copyfile(clip["video"], before)
        info = probe(before)
        video_s = float(info["video"]["duration"])
        orig_wav = clip["original"]
        with wave.open(str(orig_wav)) as w:
            rate, orig_s = w.getframerate(), w.getnframes() / w.getframerate()
        subprocess.run([ff("ffmpeg"), "-y", "-v", "error", "-i", str(before), "-i", str(orig_wav), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", str(rate), "-t", f"{video_s:.6f}",
                        "-movflags", "+faststart", str(after)], check=True)
        done = probe(after)
        orig, heard = decode(orig_wav, rate), decode(Path(clip["heard"]), rate)
        n = min(len(orig), len(heard))
        step = 0.005
        offset_ms = round(lag_of(envelope(orig[:n], rate, step), envelope(heard[:n], rate, step), step, 0.2) * 1000, 1)
        back = decode(after, rate)
        m = min(len(orig), len(back))
        back_ms = round(lag_of(envelope(orig[:m], rate, step), envelope(back[:m], rate, step), step, 0.2) * 1000, 1)
        tail_from = int(video_s * rate)
        tail = orig[tail_from:]
        tail_db = round(20 * np.log10(np.sqrt((tail ** 2).mean()) + 1e-9), 1) if len(tail) else None
        mouth_heard = round(mouth_lag(before, heard, rate) * 1000, 1)
        mouth_orig = round(mouth_lag(before, orig, rate) * 1000, 1)
        loud_before, loud_after, loud_orig = lufs(before), lufs(after), lufs(orig_wav)
        res = {"video_s": round(video_s, 3), "video_frames": int(info["video"].get("nb_frames") or 0), "original_s": round(orig_s, 3), "after_format_s": round(done["format_s"], 3),
               "after_video_start": done["video"].get("start_time"), "after_audio_start": done["audio"].get("start_time"), "after_audio_s": done["audio"].get("duration"), "audio_rate": done["audio"].get("sample_rate"),
               "original_rate": rate, "audio_cut_at_end_s": round(max(0.0, orig_s - video_s), 3), "level_of_the_cut_tail_dbfs": tail_db, "tamed_copy_vs_original_offset_ms": offset_ms,
               "mouth_lag_vs_tamed_ms": mouth_heard, "mouth_lag_vs_original_ms": mouth_orig, "decoded_after_vs_original_offset_ms": back_ms, "lufs_before": loud_before, "lufs_after": loud_after, "lufs_original": loud_orig,
               "lufs_tamed_copy": lufs(Path(clip["heard"])), "picture_copied": info["video"].get("codec_name") == done["video"].get("codec_name")}
        results[cid] = res
        rows.append({"id": cid, "title": clip["title"], "what": "Prima: il file com'era (sotto c'è la copia smorzata o la voce già giusta). Dopo: la stessa immagine copiata senza ricodifica, la registrazione originale come colonna sonora, tagliata alla fine delle immagini.",
                     "look": "Il volume rispetto alle altre clip e la sincronia di bocca e voce (inizio, fine, deriva).",
                     "numbers": {"volume prima / dopo (LUFS)": f"{loud_before} / {loud_after}", "durata immagini / voce originale (s)": f"{res['video_s']} ({res['video_frames']} fotogrammi) / {res['original_s']}",
                                 "voce tagliata in fondo (s), livello": f"{res['audio_cut_at_end_s']} s, {tail_db} dBFS", "scarto copia smorzata contro originale (ms)": offset_ms,
                                 "ritardo della bocca contro copia smorzata / originale (ms)": f"{mouth_heard} / {mouth_orig}", "scarto dell'audio dopo contro l'originale, riletto (ms)": back_ms,
                                 "inizio immagini / audio dopo (s)": f"{res['after_video_start']} / {res['after_audio_start']}", "frequenza audio": res["audio_rate"]},
                     "videos": [{"label": "prima", "src": f"before/{cid}.mp4"}, {"label": "dopo (voce originale)", "src": f"after/{cid}.mp4"}]})
        print(cid, json.dumps(res), flush=True)
    (out / "result.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    review_page.build(out / "review.html", "Colonna sonora originale sulle clip", "Il modello sente una copia smorzata della voce; la colonna sonora deve essere l'originale. Qui le clip delle presentatrici rimesse con la voce originale, immagini copiate senza ricodifica. "
                      "Giudica volume e sincronia; scrivi una nota solo dove qualcosa non va.", rows, "audio-remux-segnalate.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
