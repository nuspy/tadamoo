"""The review page of the motion matrix: the variants one after the other in a column, each with its video (sound on, the real voice), a big counter of the second and the frame, buttons to step by a frame and to slow down, a spectrum of the
audio that guided the generation (with a switch to the real voice) drawn in step with the playback, and under it the curve of the mouth opening with the position of the playback. The spectra are worked out here, one for each frame of the
video, and drawn on a canvas from the video's own time, so nothing depends on WebAudio working on a page opened from a file.

`build(rows, ctx, spectra, marks, breaths, extras)` returns the HTML; the page is mobile first (one column, targets of 44 px at least) and keeps its notes in the page: "Pubblica" makes a .md of the variants that have a note."""
from __future__ import annotations

import base64
import html
import json
from typing import Any, Dict, List

import numpy as np

BANDS = 64
FMIN, FMAX = 60.0, 12000.0
DB_LO, DB_HI = -110.0, -20.0


def band_centers(bands: int = BANDS) -> List[float]:
    return [round(float(f), 1) for f in np.geomspace(FMIN, FMAX, bands)]


def spectrum_frames(x: np.ndarray, rate: int, frames: int, fps: int = 25, bands: int = BANDS) -> np.ndarray:
    """The spectrum of each video frame, in `bands` log-spaced bands, as bytes: the level of the band (dBFS of a sine in it, from -110 to -20) scaled to 0..255."""
    hop = rate / fps
    win = int(2 * hop)
    window = np.hanning(win)
    norm = (window.sum() / 2) ** 2
    freqs = np.fft.rfftfreq(win, 1.0 / rate)
    edges = np.geomspace(FMIN, FMAX, bands + 1)
    masks = []
    for b in range(bands):
        m = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if not m.any():                                                    # a band narrower than a bin: the nearest bin
            m = np.zeros_like(freqs, dtype=bool)
            m[int(np.argmin(np.abs(freqs - np.sqrt(edges[b] * edges[b + 1]))))] = True
        masks.append(m)
    out = np.zeros((frames, bands), dtype=np.uint8)
    for i in range(frames):
        start = int(i * hop - win / 2 + hop / 2)
        seg = np.zeros(win)
        lo, hi = max(0, start), min(len(x), start + win)
        if hi > lo:
            seg[lo - start:hi - start] = x[lo:hi]
        power = np.abs(np.fft.rfft(seg * window)) ** 2 / norm + 1e-12
        db = np.array([10 * np.log10(power[m].mean()) for m in masks])
        out[i] = np.clip((db - DB_LO) / (DB_HI - DB_LO) * 255, 0, 255).astype(np.uint8)
    return out


def pack(a: np.ndarray) -> str:
    return base64.b64encode(a.tobytes()).decode()


CSS = """:root{--bg:#f6f6f4;--fg:#1d1d1b;--card:#fff;--line:#c9c9c2;--acc:#0b5cad;--warn:#b7791f}
@media(prefers-color-scheme:dark){:root{--bg:#141414;--fg:#eee;--card:#202020;--line:#444;--acc:#6aaeff;--warn:#e0b050}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.5 system-ui,sans-serif;padding:12px max(12px,env(safe-area-inset-left)) 40px}
h1{font-size:1.25rem;margin:8px 0}h2{font-size:1.1rem;margin:28px 0 6px}h3{font-size:1rem;margin:0 0 8px}p{margin:6px 0}
.bar{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center;border-bottom:1px solid var(--line)}
button{font:inherit;min-height:44px;min-width:44px;padding:0 14px;background:var(--acc);color:#fff;border:0;border-radius:8px;cursor:pointer}
button.alt{background:transparent;color:var(--acc);border:2px solid var(--acc)}button.on{background:var(--warn);color:#000}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin:14px 0}
.counter{font-size:1.5rem;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.2;margin:4px 0}
video{width:auto;height:auto;max-width:none;max-height:none;border-radius:6px;background:#000;display:block}img{max-width:none}canvas{width:100%;display:block;border-radius:6px;background:#0c0c0c;touch-action:none}
.row{display:flex;gap:8px;flex-wrap:wrap;margin:8px 0}label.note{display:block;margin-top:8px}textarea{width:100%;font:inherit;min-height:64px;padding:8px}
ul{padding-left:18px;margin:6px 0}details{margin:6px 0}pre{white-space:pre-wrap;font-size:.8rem}.chart{width:100%;border-radius:6px}.small{font-size:.85rem;opacity:.85}"""

JS = r"""
const D = JSON.parse(document.getElementById('data').textContent);
const bytes = {};
for (const [k, v] of Object.entries(D.spectra)) { const s = atob(v.b64); const a = new Uint8Array(s.length); for (let i = 0; i < s.length; i++) a[i] = s.charCodeAt(i); bytes[k] = a; }
const NB = D.bands.length, SPEC = 25, lx = f => Math.log(f / D.bands[0]) / Math.log(D.bands[NB - 1] / D.bands[0]);
const cards = [];
function setup(card, v) {
  const vid = card.querySelector('video'), spec = card.querySelector('.spec'), mouth = card.querySelector('.mouth'), cnt = card.querySelector('.counter');
  const FPS = v.fps || 25, fmt = n => (n / FPS).toFixed(2).replace('.', ',');
  const st = { vid, v, real: false, frames: Math.round((v.frames || 0)) };
  function frameNow(t) { return Math.max(0, Math.min(st.frames - 1, Math.floor(t * FPS + 1e-3))); }
  function draw(n) {
    cnt.textContent = fmt(n) + ' s · secondo ' + Math.floor(n / FPS) + ', fotogramma ' + (n % FPS) + ' di ' + FPS + '  (' + (n + 1) + '/' + st.frames + ')';
    const key = st.real ? 'real' : v.given, a = bytes[key], n25 = a ? Math.min(a.length / NB - 1, Math.floor(n / FPS * SPEC)) : 0;
    let ctx = spec.getContext('2d'), W = spec.width, H = spec.height;
    ctx.clearRect(0, 0, W, H);
    for (const m of D.marks) { ctx.fillStyle = 'rgba(244,208,63,.22)'; ctx.fillRect(lx(m.lo) * W, 0, (lx(m.hi) - lx(m.lo)) * W, H); ctx.fillStyle = '#f4d03f'; ctx.font = (H * .09) + 'px sans-serif'; ctx.fillText(m.label, lx(m.lo) * W + 3, H * .09 + 2); }
    ctx.strokeStyle = '#333'; ctx.fillStyle = '#9aa'; ctx.font = (H * .085) + 'px sans-serif';
    for (const f of [100, 300, 1000, 3000, 10000]) { const x = lx(f) * W; ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke(); ctx.fillText(f >= 1000 ? (f / 1000) + ' kHz' : f + ' Hz', x + 2, H - 3); }
    if (a) { const w = W / NB; ctx.fillStyle = st.real ? '#6ec1ff' : '#ff8a65'; for (let b = 0; b < NB; b++) { const h = a[n25 * NB + b] / 255 * (H * .86); ctx.fillRect(b * w + 1, H * .9 - h, Math.max(1, w - 2), h); } }
    ctx = mouth.getContext('2d'); W = mouth.width; H = mouth.height; ctx.clearRect(0, 0, W, H);
    for (const br of D.breaths) { ctx.fillStyle = 'rgba(244,208,63,.3)'; ctx.fillRect(br[0] / SPEC * FPS / st.frames * W, 0, (br[1] - br[0]) / SPEC * FPS / st.frames * W, H); }
    const ap = v.aperture, mx = Math.max(0.25, ...ap);
    ctx.strokeStyle = '#4da3ff'; ctx.lineWidth = 2; ctx.beginPath(); ap.forEach((y, i) => { const X = i / (st.frames - 1) * W, Y = H - 4 - y / mx * (H - 10); i ? ctx.lineTo(X, Y) : ctx.moveTo(X, Y); }); ctx.stroke();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 2; const px = n / (st.frames - 1) * W; ctx.beginPath(); ctx.moveTo(px, 0); ctx.lineTo(px, H); ctx.stroke();
    ctx.fillStyle = '#9aa'; ctx.font = (H * .16) + 'px sans-serif'; ctx.fillText('apertura della bocca (altezze di viso), massimo ' + mx.toFixed(2), 4, H * .16);
  }
  st.draw = () => draw(frameNow(vid.currentTime));
  const tick = (now, meta) => { draw(frameNow(meta && meta.mediaTime !== undefined ? meta.mediaTime : vid.currentTime)); vid.requestVideoFrameCallback(tick); };
  if (vid.requestVideoFrameCallback) vid.requestVideoFrameCallback(tick);
  for (const ev of ['seeked', 'timeupdate', 'loadeddata', 'pause', 'play']) vid.addEventListener(ev, st.draw);
  const seekTo = e => { const r = mouth.getBoundingClientRect(), x = ((e.touches ? e.touches[0].clientX : e.clientX) - r.left) / r.width; vid.pause(); vid.currentTime = (Math.round(x * (st.frames - 1)) + .5) / FPS; };
  mouth.addEventListener('pointerdown', seekTo);
  card.querySelector('.back').onclick = () => { vid.pause(); vid.currentTime = Math.max(0, (frameNow(vid.currentTime) - 1 + .5) / FPS); };
  card.querySelector('.fwd').onclick = () => { vid.pause(); vid.currentTime = Math.min(st.frames / FPS - .001, (frameNow(vid.currentTime) + 1 + .5) / FPS); };
  card.querySelectorAll('.speed').forEach(b => b.onclick = () => { vid.playbackRate = parseFloat(b.dataset.r); card.querySelectorAll('.speed').forEach(x => x.classList.toggle('on', x === b)); });
  card.querySelector('.play').onclick = () => { vid.muted = false; vid.paused ? vid.play() : vid.pause(); };
  const tog = card.querySelector('.tog'); tog.onclick = () => { st.real = !st.real; tog.textContent = st.real ? 'Spettro: voce vera (tocca per quello dato al modello)' : 'Spettro: audio dato al modello (tocca per la voce vera)'; st.draw(); };
  vid.muted = false; vid.volume = 1;
  cards.push(st); return st;
}
document.querySelectorAll('.card').forEach(c => { const v = D.variants.find(x => x.id === c.dataset.id); setup(c, v); });
function fit() { document.querySelectorAll('canvas').forEach(c => { const w = c.clientWidth * (window.devicePixelRatio || 1); if (c.width !== Math.round(w)) { c.width = Math.round(w); c.height = Math.round(w * (c.classList.contains('spec') ? .42 : .2)); } }); cards.forEach(s => s.draw()); }
window.addEventListener('resize', fit); fit();
let seq = -1;
function playFrom(i) { seq = i; if (i >= cards.length) { seq = -1; document.getElementById('msg').textContent = 'Finito.'; return; } const s = cards[i]; cards.forEach(x => x.vid.pause()); s.vid.closest('.card').scrollIntoView({ behavior: 'smooth', block: 'start' }); s.vid.currentTime = 0; s.vid.muted = false; s.vid.play(); document.getElementById('msg').textContent = 'In riproduzione: ' + (i + 1) + ' di ' + cards.length; s.vid.onended = () => { if (seq === i) playFrom(i + 1); }; }
document.getElementById('all').onclick = () => playFrom(0);
document.getElementById('stop').onclick = () => { seq = -1; cards.forEach(x => x.vid.pause()); document.getElementById('msg').textContent = ''; };
document.getElementById('publish').onclick = () => {
  const out = []; document.querySelectorAll('.card').forEach(c => { const n = c.querySelector('textarea').value.trim(); if (n) { const v = D.variants.find(x => x.id === c.dataset.id); out.push('## ' + v.title + '\n\n- Cosa era: ' + v.changed + '\n- Nota: ' + n + '\n'); } });
  const md = '# Movimento della presentatrice: varianti segnalate\n\n' + (out.length ? out.join('\n') : 'Nessuna variante segnalata.\n');
  const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([md], { type: 'text/markdown' })); a.download = 'movimento-segnalate.md'; a.click();
  document.getElementById('msg').textContent = out.length + ' varianti segnalate: file movimento-segnalate.md creato';
};
"""


def build(rows: List[Dict[str, Any]], ctx: Dict[str, Any], spectra: Dict[str, Dict[str, Any]], marks: List[Dict[str, Any]], breaths: List[List[int]], section_html: Dict[str, str],
          intro: str) -> str:
    shown = [r for r in rows if r["id"] != "base"]
    variants, cards, secs = [], [], []
    for r in shown:
        sec = r.get("section", "")
        if r.get("error"):
            secs.append(sec)
            cards.append(f"""<section class="card" data-id="{r['id']}"><h3>{html.escape(r['title'])}</h3><p><b>Cosa è stato cambiato.</b> {html.escape(r['changed'])}</p>
<p><b>Il lavoro non è riuscito, due volte.</b> {html.escape(r['error'])}</p><label class="note">Note (vuoto = va bene)<textarea></textarea></label></section>""")
            continue
        m = r["measures"]
        nums = [f"movimento medio {m['mean']} px a fotogramma, 90° percentile {m['p90']} px",
                f"{m.get('size', '')} {m['frames']} fotogrammi a {m['fps']} al secondo, {m['repeated_frames']} uguali al precedente".strip()]
        if m.get("mouth_speed_p90") is not None:
            nums.append(f"velocità della bocca (90° perc.) {m['mouth_speed_p90']} altezze di viso al secondo, testa (rms) {m['head_rms_speed']}")
        if m.get("face_sharpness") is not None:
            nums.append(f"nitidezza del volto (varianza del Laplaciano, a 832 di larghezza) {m['face_sharpness']}; video {m['video_mbit_s']} Mbit/s")
        nums.append(f"tempo di GPU {r['gpu_seconds']:.0f} s" if r["gpu_seconds"] else "nessun lavoro di GPU (si applica a una ripresa già fatta)")
        sent = f"<details><summary>cosa è stato davvero mandato al modello</summary><pre>{html.escape(json.dumps(r['params'], indent=1, ensure_ascii=False))}</pre></details>" if r.get("params") else ""
        variants.append({"id": r["id"], "title": r["title"], "changed": r["changed"], "given": r.get("given_key", "real"), "frames": m["frames"], "fps": m["fps"], "aperture": [round(a, 3) for a in m["aperture"]]})
        secs.append(sec)
        cards.append(f"""<section class="card" data-id="{r['id']}"><h3>{html.escape(r['title'])}</h3>
<div class="counter" aria-live="off">0,00 s</div>
<canvas class="spec" width="720" height="300"></canvas>
<canvas class="mouth" width="720" height="144"></canvas>
<video src="videos/{r['id']}.mp4" controls controlslist="nofullscreen noremoteplayback" disablepictureinpicture playsinline preload="auto"></video>
<div class="row"><button class="play">Avvia / ferma</button><button class="alt back" aria-label="Un fotogramma indietro">◀ 1</button><button class="alt fwd" aria-label="Un fotogramma avanti">1 ▶</button>
<button class="alt speed" data-r="1">1×</button><button class="alt speed" data-r="0.5">0,5×</button><button class="alt speed" data-r="0.25">0,25×</button></div>
<div class="row"><button class="alt tog">Spettro: audio dato al modello (tocca per la voce vera)</button></div>
<p><b>Cosa è stato cambiato.</b> {html.escape(r['changed'])}</p><p><b>Cosa guardare.</b> {html.escape(r['look'])}</p>
<ul>{''.join(f'<li>{html.escape(n)}</li>' for n in nums)}</ul>{sent}
<label class="note">Note (vuoto = va bene)<textarea></textarea></label></section>""")
    grouped = "".join(f"<h2>{html.escape(sec)}</h2>{section_html.get(sec, '')}" + "".join(c for c, x in zip(cards, secs) if x == sec) for sec in dict.fromkeys(secs))
    data = json.dumps({"bands": band_centers(), "marks": marks, "breaths": breaths, "spectra": spectra, "variants": variants}, ensure_ascii=False)
    return f"""<!doctype html><html lang="it"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Movimento della presentatrice</title>
<style>{CSS}</style>
<h1>Movimento della presentatrice: le varianti</h1>
<p>{intro}</p>
<div class="bar"><button id="all">Riproduci tutte di seguito</button><button class="alt" id="stop">Ferma</button><button id="publish">Pubblica</button><span id="msg" role="status"></span></div>
{grouped}
<script id="data" type="application/json">{data}</script>
<script>{JS}</script></html>"""
