"use strict";
/* The charts of the Model converter page, as inline SVG (no library): a star (radar) of up to four models, bars as small multiples (one panel for
   each metric), and points (two metrics against each other, the size of a point a third). Colour follows the model, never its row; marks are thin,
   grids are hairlines, a 2px ring in the surface colour keeps overlapping marks apart, and every mark has a text twin for the hover and the table
   (data-tip). The functions return markup; the numbers they draw are computed in functions of their own so a test can check them. */
(function (root) {
  // the categorical colours in their fixed order, as validated against the dark surface (adjacent pairs); a model keeps its own for good
  const COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"];
  const SHAPES = ["circle", "square", "diamond", "triangle"];     // a second channel for identity beside the colour
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const num = v => typeof v === "number" && Number.isFinite(v);

  function format(v, unit) {
    if (!num(v)) return "—";
    const a = Math.abs(v), strip = t => (t.includes(".") ? t.replace(/\.?0+$/, "") : t);
    const text = a >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 }) : a >= 100 ? v.toFixed(0) : a >= 10 ? strip(v.toFixed(1)) : strip(v.toFixed(2));
    return unit ? `${text} ${unit}` : text;
  }

  /* ---- the numbers ---- */
  // 0.2 (worst of those compared) to 1 (best), the better direction outward; all equal: 1. Null where the model has no value.
  function scores(values, better) {
    const known = values.filter(num);
    if (!known.length) return values.map(() => null);
    const lo = Math.min(...known), hi = Math.max(...known);
    return values.map(v => !num(v) ? null : hi === lo ? 1 : 0.2 + 0.8 * (better === "lower" ? (hi - v) / (hi - lo) : (v - lo) / (hi - lo)));
  }
  // the axes every model has a figure for, and each model's score on them
  function radarData(models, metrics) {
    const axes = metrics.filter(m => models.every(x => num((x.figures || {})[m.id])));
    const skipped = metrics.filter(m => !axes.includes(m));
    const per = axes.map(m => scores(models.map(x => x.figures[m.id]), m.better));
    return { axes, skipped, scores: models.map((x, i) => per.map(p => p[i])) };
  }
  function niceTicks(lo, hi, count = 4) {
    if (!(hi > lo)) { lo = Math.min(lo, 0); hi = lo + 1; }
    const raw = (hi - lo) / count, mag = 10 ** Math.floor(Math.log10(raw)), step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => s >= raw);
    const start = Math.floor(lo / step) * step, out = [];
    for (let v = start; ; v += step) { out.push(+v.toFixed(10)); if (v >= hi - step * 1e-9) break; }     // up to the first tick at or past the top
    return out;
  }
  const polar = (cx, cy, r, k, n) => { const a = -Math.PI / 2 + 2 * Math.PI * k / n; return [cx + r * Math.cos(a), cy + r * Math.sin(a)]; };

  /* ---- marks ---- */
  function marker(shape, x, y, r, color, ring) {
    const common = `fill="${color}" stroke="${ring}" stroke-width="2"`;
    if (shape === "square") return `<rect x="${x - r}" y="${y - r}" width="${2 * r}" height="${2 * r}" rx="1.5" ${common}/>`;
    if (shape === "diamond") return `<path d="M${x} ${y - r * 1.25}L${x + r * 1.25} ${y}L${x} ${y + r * 1.25}L${x - r * 1.25} ${y}Z" ${common}/>`;
    if (shape === "triangle") return `<path d="M${x} ${y - r * 1.2}L${x + r * 1.15} ${y + r}L${x - r * 1.15} ${y + r}Z" ${common}/>`;
    return `<circle cx="${x}" cy="${y}" r="${r}" ${common}/>`;
  }
  const point = (g, tip) => `<g class="cv-pt" tabindex="0" role="img" data-tip="${esc(tip)}" aria-label="${esc(tip.replace(/\n/g, ", "))}">${g}</g>`;

  /* ---- the star ---- */
  function radar(o) {
    const { models, metrics, label, better, surface = "var(--viz-surface)" } = o;
    const d = radarData(models, metrics), n = d.axes.length;
    if (n < 3) return { svg: "", skipped: d.skipped, axes: d.axes, enough: false };
    const W = 380, H = 340, cx = W / 2, cy = 170, R = 105;
    let s = `<svg class="cv-svg" viewBox="0 0 ${W} ${H}" role="group" aria-label="${esc(o.title || "")}">`;
    for (const f of [0.25, 0.5, 0.75, 1]) {
      s += `<polygon points="${d.axes.map((_, k) => polar(cx, cy, R * f, k, n).map(v => v.toFixed(1)).join(",")).join(" ")}" fill="none" class="cv-grid"/>`;
    }
    d.axes.forEach((m, k) => {
      const [x, y] = polar(cx, cy, R, k, n), [lx, ly] = polar(cx, cy, R + 14, k, n), c = Math.cos(-Math.PI / 2 + 2 * Math.PI * k / n);
      s += `<line x1="${cx}" y1="${cy}" x2="${x.toFixed(1)}" y2="${y.toFixed(1)}" class="cv-grid"/>`;
      const anchor = c > 0.25 ? "start" : c < -0.25 ? "end" : "middle";
      s += `<text x="${lx.toFixed(1)}" y="${(ly + 4).toFixed(1)}" text-anchor="${anchor}" class="cv-axis">${esc(label(m))}</text>`;
      if (better) s += `<text x="${lx.toFixed(1)}" y="${(ly + 16).toFixed(1)}" text-anchor="${anchor}" class="cv-axis2">${esc(better(m))}</text>`;
    });
    models.forEach((m, i) => {
      const pts = d.axes.map((_, k) => polar(cx, cy, R * d.scores[i][k], k, n));
      s += `<polygon points="${pts.map(p => p.map(v => v.toFixed(1)).join(",")).join(" ")}" fill="${m.color}" fill-opacity="0.10" stroke="${m.color}" stroke-width="2" stroke-linejoin="round"/>`;
    });
    models.forEach((m, i) => d.axes.forEach((a, k) => {
      const [x, y] = polar(cx, cy, R * d.scores[i][k], k, n);
      const tip = `${m.name}\n${label(a)}: ${format(m.figures[a.id], a.unit)}`;
      s += point(marker(SHAPES[i % SHAPES.length], x.toFixed(1), y.toFixed(1), 4.5, m.color, surface) + `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="13" fill="transparent"/>`, tip);
    }));
    return { svg: s + "</svg>", skipped: d.skipped, axes: d.axes, enough: true };
  }

  /* ---- bars: one panel for each metric ---- */
  function bars(o) {
    const { models, metric, label, surface = "var(--viz-surface)" } = o;
    const W = 380, left = 8, right = 76, row = 26, thick = 14, top = 6;
    const values = models.map(m => (m.figures || {})[metric.id]);
    const max = Math.max(0, ...values.filter(num)) || 1;
    const H = top + models.length * row + 4, span = W - left - right;
    let s = `<svg class="cv-svg cv-bars" viewBox="0 0 ${W} ${H}" role="group" aria-label="${esc(label(metric))}">`;
    s += `<line x1="${left}" y1="${top - 2}" x2="${left}" y2="${H - 2}" class="cv-base"/>`;
    models.forEach((m, i) => {
      const v = values[i], y = top + i * row + (row - thick) / 2;
      if (!num(v)) {
        s += point(`<text x="${left + 10}" y="${y + thick - 2}" class="cv-note">—</text>`, `${m.name}\n${label(metric)}: —`);
        return;
      }
      const w = Math.max(2, span * v / max), x2 = left + w;
      const tip = `${m.name}\n${label(metric)}: ${format(v, metric.unit)}`;
      // a 4px rounded data end and a square foot on the baseline
      const r = Math.min(4, w / 2);
      s += point(`<path d="M${left} ${y}H${x2 - r}Q${x2} ${y} ${x2} ${y + r}V${y + thick - r}Q${x2} ${y + thick} ${x2 - r} ${y + thick}H${left}Z" fill="${m.color}"${m.kind === "published" ? ' fill-opacity="0.55"' : ""}/>` +
        `<rect x="${left}" y="${y - 4}" width="${W - left - 4}" height="${thick + 8}" fill="transparent"/>` +
        `<text x="${x2 + 6}" y="${y + thick - 2}" class="cv-val">${esc(format(v, ""))}</text>`, tip);
    });
    return s + "</svg>";
  }

  /* ---- points: two metrics against each other, the size of a point a third ---- */
  function scatter(o) {
    const { models, x, y, size, label, surface = "var(--viz-surface)" } = o;
    const W = 380, H = 300, ml = 46, mr = 16, mt = 14, mb = 42;
    const pts = models.map((m, i) => ({ m, i, vx: (m.figures || {})[x.id], vy: (m.figures || {})[y.id], vs: size ? (m.figures || {})[size.id] : null }))
      .filter(p => num(p.vx) && num(p.vy));
    const left = models.length - pts.length;
    if (!pts.length) return { svg: "", missing: left };
    const xs = pts.map(p => p.vx), ys = pts.map(p => p.vy);
    const padX = (Math.max(...xs) - Math.min(...xs)) * 0.12 || Math.abs(xs[0]) * 0.1 || 1, padY = (Math.max(...ys) - Math.min(...ys)) * 0.12 || Math.abs(ys[0]) * 0.1 || 1;
    const tx = niceTicks(Math.min(...xs) - padX, Math.max(...xs) + padX), ty = niceTicks(Math.min(...ys) - padY, Math.max(...ys) + padY);
    const x0 = tx[0], x1 = tx[tx.length - 1], y0 = ty[0], y1 = ty[ty.length - 1];
    const px = v => ml + (W - ml - mr) * (v - x0) / (x1 - x0), py = v => H - mb - (H - mt - mb) * (v - y0) / (y1 - y0);
    const sizes = pts.map(p => p.vs).filter(num), smax = Math.max(...sizes, 0), smin = Math.min(...sizes, smax);
    const radius = p => !num(p.vs) || smax === smin ? 7 : 6 + 8 * Math.sqrt((p.vs - smin) / (smax - smin));
    let s = `<svg class="cv-svg" viewBox="0 0 ${W} ${H}" role="group" aria-label="${esc(label(y))} / ${esc(label(x))}">`;
    ty.forEach(v => { s += `<line x1="${ml}" y1="${py(v).toFixed(1)}" x2="${W - mr}" y2="${py(v).toFixed(1)}" class="cv-grid"/><text x="${ml - 6}" y="${(py(v) + 4).toFixed(1)}" text-anchor="end" class="cv-axis">${esc(format(v, ""))}</text>`; });
    tx.forEach(v => { s += `<text x="${px(v).toFixed(1)}" y="${H - mb + 16}" text-anchor="middle" class="cv-axis">${esc(format(v, ""))}</text>`; });
    s += `<line x1="${ml}" y1="${H - mb}" x2="${W - mr}" y2="${H - mb}" class="cv-base"/>`;
    s += `<text x="${(ml + W - mr) / 2}" y="${H - 6}" text-anchor="middle" class="cv-axis2">${esc(label(x))}${x.unit ? " (" + esc(x.unit) + ")" : ""} ${x.better === "lower" ? "←" : "→"}</text>`;
    s += `<text x="12" y="${(mt + H - mb) / 2}" text-anchor="middle" class="cv-axis2" transform="rotate(-90 12 ${(mt + H - mb) / 2})">${esc(label(y))}${y.unit ? " (" + esc(y.unit) + ")" : ""} ${y.better === "lower" ? "↓" : "↑"}</text>`;
    pts.forEach(p => {
      const cx = px(p.vx), cy = py(p.vy), r = radius(p);
      const tip = `${p.m.name}\n${label(x)}: ${format(p.vx, x.unit)}\n${label(y)}: ${format(p.vy, y.unit)}` + (size && num(p.vs) ? `\n${label(size)}: ${format(p.vs, size.unit)}` : "");
      s += point(marker(SHAPES[p.i % SHAPES.length], cx.toFixed(1), cy.toFixed(1), r, p.m.color, surface) + `<circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="${(r + 10).toFixed(1)}" fill="transparent"/>`, tip);
    });
    return { svg: s + "</svg>", missing: left };
  }

  root.TadamooCharts = { COLORS, SHAPES, format, scores, radarData, niceTicks, polar, marker, radar, bars, scatter, esc };
})(typeof window !== "undefined" ? window : globalThis);
