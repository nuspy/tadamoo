"use strict";
/* Monitor > VRAM Explorer: the VRAM over time (the total of each card, or each process stacked) and who holds the memory now.
   The total reads GET /api/vram/history (NVML, a day of peaks), the processes GET /api/vram/processes (recorded by the server
   from the first time the page is open), the list GET /api/vram/explorer (gputaskman's collector, only while this page shows).
   Names come from the operating system and are always set as text. */
(function () {
  const HISTORY_MS = 5000, EXPLORER_MS = 2000;
  const RANGES = [[300, "5 min"], [3600, "1 h"], [21600, "6 h"], [86400, "24 h"]];
  const MODES = [["total", "Total"], ["process", "By process"]];
  const GIB = 1024 ** 3, SVG = "http://www.w3.org/2000/svg";
  const CARD_COLORS = ["var(--accent)", "var(--fg-3)", "var(--tadamoo-500)"];
  const hue = i => `hsl(${(205 + i * 47) % 360} 55% 52%)`;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const svg = (tag, attrs) => { const e = document.createElementNS(SVG, tag); Object.entries(attrs).forEach(([k, v]) => e.setAttribute(k, v)); return e; };
  const isNum = v => typeof v === "number" && Number.isFinite(v);
  const gb = v => isNum(v) ? (v / GIB >= 100 ? (v / GIB).toFixed(0) : (v / GIB).toFixed(1)) : "n/a";
  const mb = v => isNum(v) ? (v >= GIB ? `${gb(v)} GiB` : `${Math.round(v / 1048576)} MiB`) : "n/a";
  const get = async path => {
    const r = await fetch(path, { cache: "no-store", headers: authHeaders() });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    return r.json();
  };
  const saved = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* the page works without it */ } };
  const shown = () => !document.hidden && document.body.dataset.view === "vram";
  const clock = (ms, long) => new Date(ms).toLocaleTimeString([], long ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });

  /* ---- the chart over time ---- */
  const chart = $("#chart-vh");
  let range = Number(saved("tadamoo.vram.range", "300")), mode = saved("tadamoo.vram.mode", "total"), gpu = 0;
  if (!RANGES.some(r => r[0] === range)) range = 300;
  if (!MODES.some(m => m[0] === mode)) mode = "total";
  let D = null, hover = null, chartBusy = false, chartAt = 0;      // D: the answer of the route the mode reads

  function runs(values) {      // stretches of consecutive readings, as [index, value] lists
    const out = []; let cur = null;
    values.forEach((v, i) => {
      if (isNum(v)) { (cur = cur || []).push([i, v]); } else if (cur) { out.push(cur); cur = null; }
    });
    if (cur) out.push(cur);
    return out;
  }
  const lastOf = a => [...a].reverse().find(isNum);

  // what is drawn, whatever the mode: layers of {name, color, values}, the bucket count, the scale, and the sum of a bucket
  function layers() {
    if (!D) return { n: 0, items: [], total: null, sum: () => null };
    if (mode === "total") {
      const cards = D.gpus || [];
      return { n: cards.length ? cards[0].used.length : 0, total: Math.max(...cards.map(c => isNum(c.total) ? c.total : 0), 0) || null,
               items: cards.map((c, k) => ({ name: c.name || `GPU ${c.index}`, color: CARD_COLORS[k % CARD_COLORS.length], values: c.used })), stacked: false,
               sum: i => cards.map(c => c.used[i]).find(isNum) };
    }
    const items = (D.series || []).map((s, k) => ({ name: s.name, color: hue(k), values: s.used }));
    if ((D.other || []).some(v => isNum(v) && v > 0)) items.push({ name: "Other processes", color: "var(--fg-3)", values: D.other });
    if ((D.unattributed || []).some(v => isNum(v) && v > 0)) items.push({ name: "Not attributed", color: "var(--warn)", values: D.unattributed });
    return { n: (D.used || []).length, total: isNum(D.total) ? D.total : null, items, stacked: true, sum: i => D.used[i] };
  }

  function drawChart() {
    const lines = $("#vh-lines"), leg = $("#vh-leg");
    lines.replaceChildren(); leg.replaceChildren();
    const L = layers(), n = L.n;
    $("#vh-empty").hidden = L.items.some(it => it.values.some(isNum));
    const peak = Math.max(...L.items.flatMap(it => it.values.filter(isNum)), ...(L.stacked ? (D.used || []).filter(isNum) : []), 1);
    const max = L.total ? Math.max(L.total, peak) : peak * 1.05;
    chart.querySelector(".y0").textContent = `${Math.round(max / GIB)} G`;
    chart.querySelector(".y1").textContent = `${Math.round(max / GIB / 2)} G`;
    const X = i => n > 1 ? i / (n - 1) * 100 : 100, Y = v => 100 - Math.min(v, max) / max * 100;
    if (L.stacked) {
      const base = Array(n).fill(0);                                   // the top of the layers drawn so far
      L.items.forEach(it => {
        const top = it.values.map((v, i) => isNum(D.used[i]) ? base[i] + (isNum(v) ? v : 0) : null);
        runs(top).forEach(r => {
          const up = r.map(([i, v]) => `${X(i).toFixed(2)} ${Y(v).toFixed(2)}`), down = [...r].reverse().map(([i]) => `${X(i).toFixed(2)} ${Y(base[i]).toFixed(2)}`);
          const area = svg("path", { d: `M ${up.join(" L ")} L ${down.join(" L ")} Z`, class: "vh-layer" });
          area.style.fill = it.color; lines.append(area);
        });
        top.forEach((v, i) => { if (isNum(v)) base[i] = v; });
      });
    } else {
      L.items.forEach(it => runs(it.values).forEach(r => {
        const pts = r.map(([i, v]) => `${X(i).toFixed(2)} ${Y(v).toFixed(2)}`);
        if (r.length === 1) pts.push(pts[0]);
        const area = svg("path", { d: `M ${pts.join(" L ")} L ${X(r[r.length - 1][0]).toFixed(2)} 100 L ${X(r[0][0]).toFixed(2)} 100 Z`, class: "vh-area" });
        area.style.fill = it.color;
        const line = svg("path", { d: `M ${pts.join(" L ")}`, class: "vh-line" });
        line.style.stroke = it.color;
        lines.append(area, line);
      }));
    }
    L.items.forEach(it => {
      const at = hover == null ? lastOf(it.values) : it.values[hover];
      const item = el("span"); item.style.setProperty("--c", it.color);
      item.append(`${it.name} `, el("b", "", isNum(at) ? (L.stacked ? mb(at) : `${gb(at)} / ${gb(L.total)} GiB`) : "-"));
      leg.append(item);
    });
    const now = hover == null ? lastOf(Array.from({ length: n }, (_, i) => L.sum(i))) : L.sum(hover);
    $("#vh-l").textContent = hover == null ? "In use now" : "In use";
    $("#vh-now").replaceChildren(isNum(now) ? gb(now) : "-", el("small", "", isNum(L.total) ? ` / ${gb(L.total)} GiB` : " GiB"));
    const span = D ? RANGES.find(r => r[0] === D.range_s) : null;
    $("#vh-x0").textContent = span ? `${span[1]} ago` : "";
    $("#vh-note").textContent = D && D.step_s ? `${L.stacked ? "mean" : "peak"} / ${D.step_s} s` : "";
    drawHover(L, X, Y);
  }

  function drawHover(L, X, Y) {
    const [cur, dot, tip] = chart.querySelectorAll(".cur, .cdot, .tip");
    const here = hover != null && L.n > 0 && isNum(L.sum(hover));
    [cur, dot, tip].forEach(e => e.hidden = !here);
    if (!here) return;
    const x = X(hover), y = Y(L.sum(hover));
    const ms = (D.end - (L.n - 1 - hover) * D.step_s) * 1000;
    cur.style.left = x + "%"; dot.style.left = x + "%"; dot.style.top = y + "%";
    tip.style.left = x + "%"; tip.style.top = Math.min(84, Math.max(16, y)) + "%";
    tip.classList.toggle("flip", x > 60);
    const rows = L.stacked ? L.items.filter(it => isNum(it.values[hover]) && it.values[hover] > 0) : [];
    tip.replaceChildren(el("b", "", L.stacked ? `${mb(L.sum(hover))} in use` : L.items.filter(it => isNum(it.values[hover])).map(it => `${gb(it.values[hover])} GiB`).join(" · ")),
      ...rows.map(it => { const r = el("span", "vh-tiprow"); r.style.setProperty("--c", it.color); r.textContent = `${it.name} ${mb(it.values[hover])}`; return r; }),
      el("span", "", clock(ms, D.range_s <= 3600)));
  }

  async function loadChart() {
    if (chartBusy) return;
    chartBusy = true;
    try {
      D = await get(mode === "total" ? `api/vram/history?range=${range}` : `api/vram/processes?gpu=${gpu}&range=${range}`);
      chartAt = Date.now(); drawChart();
    } catch (e) { D = null; drawChart(); }
    finally { chartBusy = false; }
  }
  function buttons(box, list, current, pick) {
    list.forEach(([key, label]) => {
      const b = el("button", "", label); b.type = "button";
      b.setAttribute("aria-pressed", String(key === current));
      b.onclick = () => { pick(key); box.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b))); hover = null; D = null; drawChart(); loadChart(); };
      box.append(b);
    });
  }
  buttons($("#vh-mode"), MODES, mode, k => { mode = k; keep("tadamoo.vram.mode", k); });
  buttons($("#vh-range"), RANGES, range, k => { range = k; keep("tadamoo.vram.range", String(k)); });
  chart.addEventListener("pointermove", e => {
    const n = layers().n;
    if (!n) return;
    const r = chart.getBoundingClientRect();
    hover = Math.min(n - 1, Math.max(0, Math.round((e.clientX - r.left) / r.width * (n - 1))));
    drawChart();
  });
  chart.addEventListener("pointerleave", () => { hover = null; drawChart(); });

  /* ---- who holds the memory now ---- */
  const root = $("#vx-root");
  let X = null, exploring = false, exploreAt = 0, built = false;
  const ui = {};

  function build() {
    built = true;
    const top = el("div", "vx-top");
    ui.pick = el("select"); ui.pick.setAttribute("aria-label", "GPU");
    ui.pick.onchange = () => { gpu = Number(ui.pick.value); X = null; D = null; drawChart(); refresh(true); loadChart(); };
    ui.sum = el("div", "vx-sum");
    top.append(ui.pick, ui.sum);
    ui.bar = el("div", "vx-bar"); ui.bar.setAttribute("aria-hidden", "true");
    ui.leg = el("div", "leg vx-leg");
    ui.notes = el("div", "vx-notes");
    const wrap = el("div", "vx-scroll");                              // the list scrolls inside its own box, its header stays
    wrap.tabIndex = 0; wrap.setAttribute("role", "region"); wrap.setAttribute("aria-label", "Processes");
    const table = el("table"), head = el("tr");
    [["Process", ""], ["Kind", ""], ["VRAM", "n"], ["Shared", "n"], ["GPU", "n"], ["Inside", ""]].forEach(([label, cls]) => head.append(el("th", cls, label)));
    const thead = el("thead"); thead.append(head);
    ui.rows = el("tbody");
    table.append(thead, ui.rows);
    ui.empty = el("div", "empty", "Reading the GPU...");
    wrap.append(table, ui.empty);
    ui.foot = el("p", "vx-foot", "The list reads the operating system's per-process counters; the total chart and the Monitor header read NVML, so their totals can differ by a few hundred MiB. ~ marks an estimate or an inference, n/a a value this system does not give.");
    root.append(top, ui.bar, ui.leg, ui.notes, wrap, ui.foot);
  }

  const shownBytes = p => isNum(p.vram_bytes) ? p.vram_bytes : (isNum(p.vram_inferred_bytes) ? p.vram_inferred_bytes : null);

  function drawExplorer() {
    const d = X;
    if (!d) return;
    if (d.error) { ui.sum.textContent = d.error; ui.rows.replaceChildren(); ui.empty.hidden = true; return; }
    if (ui.pick.options.length !== (d.gpus || []).length) {
      ui.pick.replaceChildren(...(d.gpus || []).map(g => { const o = el("option", "", g.name); o.value = g.index; return o; }));
    }
    ui.pick.value = String(gpu); ui.pick.hidden = (d.gpus || []).length < 2;
    const total = d.vram_total;
    ui.sum.textContent = `${d.gpu || "GPU"} · ${isNum(d.vram_used) ? mb(d.vram_used) : "n/a"} of ${mb(total)} in use`
      + (isNum(d.shared_used) ? ` · ${mb(d.shared_used)} shared (system RAM)` : "")
      + (d.driver ? ` · driver ${d.driver}` : "");
    const procs = (d.processes || []).map(p => ({ p, v: shownBytes(p) })).sort((a, b) => (b.v || 0) - (a.v || 0));
    const used = isNum(d.vram_used) ? d.vram_used : 0, base = isNum(total) && total > 0 ? total : Math.max(used, 1);
    ui.bar.replaceChildren(); ui.leg.replaceChildren();
    const part = (label, v, color, mark) => {
      if (!isNum(v) || v <= 0) return;
      const seg = el("i"); seg.style.width = (v / base * 100) + "%"; seg.style.background = color; seg.title = `${label}: ${mb(v)}`;
      ui.bar.append(seg);
      const item = el("span"); item.style.setProperty("--c", color);
      item.append(`${label} `, el("b", "", `${mark || ""}${mb(v)}`));
      ui.leg.append(item);
    };
    const big = procs.filter(x => isNum(x.v) && x.v >= base * 0.02).slice(0, 6);
    big.forEach((x, i) => part(x.p.name, x.v, hue(i), isNum(x.p.vram_bytes) ? "" : "~"));
    part("Other processes", procs.filter(x => !big.includes(x)).reduce((a, x) => a + (x.p.vram_bytes || 0), 0), "var(--fg-3)");
    part("Not attributed", Math.max(0, used - (d.attributed || 0)), "var(--warn)", "~");
    ui.notes.replaceChildren(...[d.counter_status, d.process_status, d.host_status, d.memory_note].filter(Boolean).map(t => el("p", "", t)));
    ui.rows.replaceChildren();
    procs.forEach(({ p, v }) => {
      const tr = el("tr");
      const name = el("td", "f", p.name); name.title = `PID ${p.pid}`;
      name.append(el("small", "", ` ${p.pid}`));
      const inside = el("td");
      inside.append(el("span", "", [p.inside, (p.runtimes || []).join(", ")].filter(Boolean).join(" · ")));
      (p.llm ? p.llm.models : []).forEach(m => inside.append(el("div", "vx-model", `${m.label || m.name} · ${m.quant || ""} ${m.kind_label || ""}`.trim())));
      if (p.llm && p.llm.segments && p.llm.segments.length) {
        const segs = el("div", "vx-segs");
        p.llm.segments.forEach(s => {
          const c = el("span", "vx-seg", `${s.label} ${s.exact ? "" : "~"}${mb(s.bytes)}`);
          c.dataset.kind = s.kind; c.title = s.exact ? "As reported by the runtime" : "Estimated from the model file";
          segs.append(c);
        });
        inside.append(segs);
      }
      const cell = (content, cls) => { const c = el("td", cls || ""); c.append(content); return c; };
      tr.append(name, cell(p.kind || ""), cell(v == null ? "n/a" : `${isNum(p.vram_bytes) ? "" : "~"}${mb(v)}`, "n f"), cell(mb(p.shared_bytes), "n"),
        cell(isNum(p.gpu_pct) ? `${p.gpu_pct.toFixed(1)}%` : "n/a", "n"), inside);
      ui.rows.append(tr);
    });
    ui.empty.hidden = procs.length > 0;
    ui.empty.textContent = "No process is using this GPU.";
    text("#vx-count", String(procs.length));
  }

  async function refresh(force) {
    if (exploring) return;
    if (!force && Date.now() - exploreAt < EXPLORER_MS) return;
    exploring = true;
    try { X = await get(`api/vram/explorer?gpu=${gpu}`); exploreAt = Date.now(); drawExplorer(); }
    catch (e) { X = { error: `The VRAM Explorer could not read the GPU (${e.message}).`, gpus: [] }; drawExplorer(); }
    finally { exploring = false; }
  }

  function open() {
    if (!built) build();
    loadChart(); refresh(true);
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "vram") open(); });
  setInterval(() => {
    if (!shown()) return;
    if (Date.now() - chartAt >= HISTORY_MS) loadChart();
    refresh(false);
  }, 1000);
  // the server records the processes from the first request: asking once when the page loads starts it
  get("api/vram/processes?range=300").catch(() => {});
  if (document.body.dataset.view === "vram") open();
})();
