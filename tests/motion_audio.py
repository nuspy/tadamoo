"""What in the voice makes the presenter open her mouth: the mouth opening of each frame of a take against the energy of the voice in frequency bands, the breaths between sentences, copies of the voice made to
calm it, and the charts for the review page of `tests/motion_matrix.py`.

The mouth opening is the inner-lip distance in face heights, frame by frame (`videoedit.qa.face_track`, MediaPipe). The voice is cut into frames of the video's rate (25 a second) and its energy in 16 mel bands is taken
for each; the correlation of the opening with each band is found at the lags of a few frames (the mouth may follow the sound). A *pause* is a stretch of at least 4 frames well under the speech level; a *breath* is a pause
that is not silence (broadband noise above the floor). Nothing here decides what drives the mouth: it measures, and the page shows it."""
from __future__ import annotations

import base64
import io
import subprocess
import wave
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

FPS = 25
BANDS = 16
LAGS = range(-3, 4)


def read_wav(path: Path) -> Tuple[int, np.ndarray]:
    with wave.open(str(path)) as w:
        rate, n = w.getframerate(), w.getnframes()
        raw = np.frombuffer(w.readframes(n), dtype=np.int16).astype(np.float32) / 32768.0
        if w.getnchannels() > 1:
            raw = raw.reshape(-1, w.getnchannels()).mean(axis=1)
    return rate, raw


def write_wav(path: Path, rate: int, x: np.ndarray) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes((np.clip(x, -1, 1) * 32767).astype(np.int16).tobytes())
    return path


def mel_edges(rate: int, bands: int = BANDS, fmin: float = 60.0) -> np.ndarray:
    mel = lambda f: 2595 * np.log10(1 + f / 700.0)                        # noqa: E731
    inv = lambda m: 700 * (10 ** (m / 2595.0) - 1)                         # noqa: E731
    return inv(np.linspace(mel(fmin), mel(rate / 2 * 0.98), bands + 1))


def band_energy(x: np.ndarray, rate: int, frames: int, bands: int = BANDS) -> Dict[str, Any]:
    """For each video frame: the energy in dB of each mel band, the total RMS in dB and the spectral flatness (1: noise, 0: a tone)."""
    hop = rate / FPS
    win = int(2 * hop)
    edges = mel_edges(rate, bands)
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    window = np.hanning(win)
    e = np.full((frames, bands), -120.0)
    rms = np.full(frames, -120.0)
    flat = np.zeros(frames)
    for i in range(frames):
        start = int(i * hop - win / 2 + hop / 2)
        seg = np.zeros(win)
        lo, hi = max(0, start), min(len(x), start + win)
        if hi > lo:
            seg[lo - start:hi - start] = x[lo:hi]
        spec = np.abs(np.fft.rfft(seg * window)) ** 2 + 1e-12
        for b in range(bands):
            m = (freqs >= edges[b]) & (freqs < edges[b + 1])
            e[i, b] = 10 * np.log10(spec[m].mean()) if m.any() else -120.0
        rms[i] = 20 * np.log10(np.sqrt(np.mean(seg ** 2)) + 1e-9)
        flat[i] = float(np.exp(np.mean(np.log(spec))) / np.mean(spec))
    tilt = e[:, bands // 2 + 1:].mean(axis=1) - e[:, :bands // 2 + 1].mean(axis=1)             # high bands over low bands, dB: a breath is hiss, a vowel is not
    return {"bands_db": e, "rms_db": rms, "flatness": flat, "edges": edges, "tilt_db": tilt}


def aperture_series(video: Path) -> np.ndarray:
    """The mouth opening of every frame (face heights), gaps filled."""
    from tadamoo.videoedit import qa
    ap = np.array(qa.face_track(str(video)).aperture, dtype=float)
    ok = ~np.isnan(ap)
    if ok.any() and not ok.all():
        ap = np.interp(np.arange(len(ap)), np.flatnonzero(ok), ap[ok])
    return ap


def pauses(rms_db: np.ndarray, tilt_db: np.ndarray, under_db: float = 10.0) -> List[Dict[str, Any]]:
    """Stretches of 2 frames or more that are `under_db` under the level of the speech (its median), gaps of one frame closed: `{start, end, level_db, tilt_db, breath}` in frames. A breath is a stretch that is not
    silence (its level is more than 10 dB over the floor) and whose high bands stand 8 dB higher over the low ones than they do in the speech."""
    speech = rms_db > np.percentile(rms_db, 25)
    level = float(np.median(rms_db[speech]))
    floor = float(np.percentile(rms_db, 2))
    typical_tilt = float(np.median(tilt_db[speech]))
    low = rms_db < level - under_db
    for i in range(1, len(low) - 1):
        if not low[i] and low[i - 1] and low[i + 1]:
            low[i] = True
    out, i = [], 0
    while i < len(low):
        if low[i]:
            j = i
            while j < len(low) and low[j]:
                j += 1
            if j - i >= 2:
                lv, tl = float(np.mean(rms_db[i:j])), float(np.mean(tilt_db[i:j]))
                out.append({"start": i, "end": j, "level_db": round(lv, 1), "tilt_db": round(tl, 1), "breath": bool(lv > floor + 10 and tl > typical_tilt + 8)})
            i = j
        else:
            i += 1
    return out


def correlations(ap: np.ndarray, bands_db: np.ndarray, window: Optional[Tuple[int, int]] = None) -> List[Dict[str, Any]]:
    """For each band the correlation of the opening with the band's energy at the lag (in frames, the sound before the mouth when positive) where it is strongest, over the whole take or over `window`."""
    n = min(len(ap), len(bands_db))
    lo, hi = window if window else (0, n)
    out = []
    for b in range(bands_db.shape[1]):
        best = (0.0, 0)
        for lag in LAGS:
            idx = np.arange(max(lo, lag, 0), min(hi, n + min(lag, 0), n))
            if len(idx) < 8:
                continue
            c = np.corrcoef(ap[idx], bands_db[idx - lag, b])[0, 1]
            if not np.isnan(c) and abs(c) > abs(best[0]):
                best = (float(c), lag)
        out.append({"band": b, "corr": round(best[0], 3), "lag": best[1]})
    return out


def analyze(video: Path, audio: Path, around_s: float = 4.0) -> Dict[str, Any]:
    """The mouth against the voice for one take: the correlation per band over the whole take and over the breath nearest `around_s` seconds, the pauses found, and the series for the charts."""
    rate, x = read_wav(audio)
    ap = aperture_series(video)
    en = band_energy(x, rate, len(ap))
    stops = pauses(en["rms_db"], en["tilt_db"])
    breaths = [p for p in stops if p["breath"]] or stops
    near = min(breaths, key=lambda p: abs((p["start"] + p["end"]) / 2 / FPS - around_s)) if breaths else None
    window = (max(0, near["start"] - 3), min(len(ap), near["end"] + 3)) if near else None
    return {"aperture": ap, "energy": en, "pauses": stops, "breath": near, "window": window,
            "corr_all": correlations(ap, en["bands_db"]), "corr_breath": correlations(ap, en["bands_db"], window) if window else [],
            "corr_level": correlations(ap, en["rms_db"][:, None])[0], "corr_tilt": correlations(ap, en["tilt_db"][:, None])[0],
            "aperture_in_breath": round(float(ap[near["start"]:near["end"]].mean()), 4) if near else None, "aperture_mean": round(float(ap.mean()), 4)}


# ---- copies of the voice
def ff(args: List[str]) -> None:
    from tadamoo.videoedit import compose
    subprocess.run([compose.tool("ffmpeg"), "-y", "-v", "error", *args], check=True)


def gate_breaths(src: Path, out: Path, stops: List[Dict[str, Any]], drop_db: float = -30.0) -> Path:
    """The voice with every pause lowered by `drop_db` (the breath between sentences), the change smoothed over 30 ms so that nothing clicks."""
    rate, x = read_wav(src)
    gain = np.ones(len(x))
    for p in stops:
        gain[int(p["start"] / FPS * rate):int(p["end"] / FPS * rate)] = 10 ** (drop_db / 20)
    k = int(0.03 * rate)
    gain = np.convolve(gain, np.ones(k) / k, mode="same")
    return write_wav(out, rate, x * gain)


def filtered(src: Path, out: Path, chain: str) -> Path:
    ff(["-i", str(src), "-af", chain, "-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(out)])
    return out


def level_db(x: np.ndarray) -> float:
    return float(20 * np.log10(np.sqrt(np.mean(x ** 2)) + 1e-9))


# ---- charts
def png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    import matplotlib.pyplot as plt
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def img(b64: str, alt: str) -> str:
    return f'<img class="chart" alt="{alt}" src="data:image/png;base64,{b64}">'


def chart_variant(ap: np.ndarray, voice_db: np.ndarray, given_db: np.ndarray, breath: Optional[Dict[str, Any]], title: str) -> str:
    """The opening of the mouth of one take, with the level of the voice that was given to the model under it and the breath shaded."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    t = np.arange(len(ap)) / FPS
    fig, (a, b) = plt.subplots(2, 1, figsize=(8, 3.6), sharex=True, gridspec_kw={"height_ratios": [2, 1]})
    a.plot(t, ap, color="#0b5cad")
    a.set_ylabel("apertura (altezze di viso)")
    b.plot(t, voice_db[:len(t)], color="#999", label="voce vera")
    b.plot(t, given_db[:len(t)], color="#c0392b", label="audio dato al modello")
    b.set_ylabel("livello dB")
    b.set_xlabel("secondi")
    b.legend(loc="lower right", fontsize=7)
    if breath:
        for ax in (a, b):
            ax.axvspan(breath["start"] / FPS, breath["end"] / FPS, color="#f4d03f", alpha=0.35)
    a.set_title(title, fontsize=9)
    return img(png(fig), title)


def chart_analysis(an: Dict[str, Any], rate: int, x: np.ndarray, title: str) -> str:
    """The spectrogram of the voice with the breath marked, the correlation of the opening with each band, and the opening with the level of the voice."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    en, ap = an["energy"], an["aperture"]
    t = np.arange(len(ap)) / FPS
    edges = en["edges"]
    centers = np.sqrt(edges[:-1] * edges[1:])
    fig, ax = plt.subplots(3, 1, figsize=(8, 8.5))
    ax[0].imshow(en["bands_db"].T, origin="lower", aspect="auto", extent=[0, len(ap) / FPS, 0, en["bands_db"].shape[1]], cmap="magma")
    ax[0].set_yticks(range(0, en["bands_db"].shape[1], 3))
    ax[0].set_yticklabels([f"{int(centers[i])} Hz" for i in range(0, en["bands_db"].shape[1], 3)], fontsize=7)
    ax[0].set_title(f"{title}: energia della voce per banda (mel), respiro in giallo", fontsize=9)
    ax[1].plot(t, ap, color="#0b5cad", label="apertura bocca")
    ax[1].plot(t, (en["rms_db"][:len(t)] - en["rms_db"].min()) / (en["rms_db"].max() - en["rms_db"].min()) * ap.max(), color="#999", label="livello voce (scala relativa)")
    ax[1].legend(fontsize=7)
    for p in an["pauses"]:
        for a_ in ax[:2]:
            a_.axvspan(p["start"] / FPS, p["end"] / FPS, color="#f4d03f" if p["breath"] else "#d5d8dc", alpha=0.35)
    w = 0.4
    idx = np.arange(len(an["corr_all"]))
    ax[2].bar(idx - w / 2, [c["corr"] for c in an["corr_all"]], w, label="tutto il tratto", color="#0b5cad")
    if an["corr_breath"]:
        ax[2].bar(idx + w / 2, [c["corr"] for c in an["corr_breath"]], w, label="solo intorno al respiro", color="#e67e22")
    ax[2].set_xticks(idx)
    ax[2].set_xticklabels([f"{int(c)}" for c in centers], fontsize=7, rotation=60)
    ax[2].set_xlabel("banda (Hz, centro)")
    ax[2].set_ylabel("correlazione con l'apertura")
    ax[2].legend(fontsize=7)
    return img(png(fig), title)
