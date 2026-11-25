"use strict";
/* Monitor: the VRAM-over-time chart (left column) and the VRAM Explorer tab (Requests | Logs | VRAM Explorer).
   The chart reads GET /api/vram/history (the server keeps a day of peaks, one point per 10 s); the Explorer reads
   GET /api/vram/explorer, the gputaskman collector, only while its tab is open. Names come from the operating system
   and are always set as text. */
(function () {
  const HISTORY_MS = 5000, EXPLORER_MS = 2000;
  const RANGES = [[300, "5 min"], [3600, "1 h"], [21600, "6 h"], [86400, "24 h"]];
  const GIB = 1024 ** 3;
  const COLORS = ["var(--accent)", "var(--fg-3)", "var(--tadamoo-500)"];
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
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
  const onStats = () => !document.hidden && document.body.dataset.view === "stats";

  /* ---- the chart over time ---- */
  const chart = $("#chart-vh");
  let range = Number(saved("tadamoo.vram.range", "300")), H = null, hover = null, historyBusy = false, historyAt = 0;
  if (!RANGES.some(r => r[0] === range)) range = 300;

  function runs(values) {      // the stretches of consecutive readings, as [index, value] lists
    const out = []; let cur = null;
    values.forEach((v, i) => {
      if (isNum(v)) { (cur = cur || []).push([i, v]); } else if (cur) { out.push(cur); cur = null; }
    });
    if (cur) out.push(cur);
    return out;
  }
  const clock = (ms, long) => new Date(ms).toLocaleTimeString([], long ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" });

  function drawHistory() {
    const lines = $("#vh-lines"), leg = $("#vh-leg");
    lines.replaceChildren(); leg.replaceChildren();
    const cards = (H && H.gpus) || [];
    const n = cards.length ? cards[0].used.length : 0;
    const seen = cards.some(c => c.used.some(isNum));
    $("#vh-empty").hidden = seen;
    const top = Math.max(...cards.map(c => isNum(c.total) ? c.total : 0), ...cards.flatMap(c => c.used.filter(isNum)), 1);
    const max = isNum(cards[0] && cards[0].total) ? top : top * 1.05;
    chart.querySelector(".y0").textContent = `${Math.round(max / GIB)} G`;
    chart.querySelector(".y1").textContent = `${Math.round(max / GIB / 2)} G`;
    const X = i => n > 1 ? i / (n - 1) * 100 : 100, Y = v => 100 - Math.min(v, max) / max * 100;
    cards.forEach((c, k) => {
      const color = COLORS[k % COLORS.length];
      runs(c.used).forEach(r => {
        const pts = r.map(([i, v]) => `${X(i).toFixed(2)} ${Y(v).toFixed(2)}`);
        if (r.length === 1) pts.push(pts[0]);
        const area = document.createElementNS("http://www.w3.org/2000/svg", "path");
        area.setAttribute("d", `M ${pts.join(" L ")} L ${X(r[r.length - 1][0]).toFixed(2)} 100 L ${X(r[0][0]).toFixed(2)} 100 Z`);
        area.setAttribute("class", "vh-area"); area.style.fill = color;
        const line = document.createElementNS("http://www.w3.org/2000/svg", "path");
        line.setAttribute("d", `M ${pts.join(" L ")}`);
        line.setAttribute("class", "vh-line"); line.style.stroke = color;
        lines.append(area, line);
      });
      const last = [...c.used].reverse().find(isNum), at = hover == null ? last : c.used[hover];
      const item = el("span"); item.style.setProperty("--c", color);
      item.append(`${c.name || `GPU ${c.index}`} `, el("b", "", isNum(at) ? `${gb(at)} / ${gb(c.total)} GiB` : "-"));
      leg.append(item);
    });
    const first = cards[0], now = first && [...first.used].reverse().find(isNum);
    $("#vh-now").replaceChildren(isNum(now) ? `${gb(now)}` : "-", el("small", "", isNum(first && first.total) ? ` / ${gb(first.total)} GiB` : " GiB"));
    const span = H ? RANGES.find(r => r[0] === H.range_s) : null;
    $("#vh-x0").textContent = span ? `${span[1]} ago` : "";
    $("#vh-note").textContent = H && H.step_s ? `peak / ${H.step_s} s` : "";
    drawHover(n, max, X, Y);
  }

  function drawHover(n, max, X, Y) {
    const [cur, dot, tip] = chart.querySelectorAll(".cur, .cdot, .tip");
    const cards = (H && H.gpus) || [];
    const on = hover != null && n > 0 && cards.some(c => isNum(c.used[hover]));
    [cur, dot, tip].forEach(e => e.hidden = !on);
    if (!on) return;
    const x = X(hover), c0 = cards.find(c => isNum(c.used[hover])), y = Y(c0.used[hover]);
    const ms = (H.end - (n - 1 - hover) * H.step_s) * 1000;
    cur.style.left = x + "%"; dot.style.left = x + "%"; dot.style.top = y + "%";
    tip.style.left = x + "%"; tip.style.top = Math.min(84, Math.max(16, y)) + "%";
    tip.classList.toggle("flip", x > 60);
    tip.replaceChildren(el("b", "", cards.filter(c => isNum(c.used[hover])).map(c => `${gb(c.used[hover])} GiB`).join(" · ")),
      el("span", "", clock(ms, H.range_s <= 3600)));
  }

  async function loadHistory() {
    if (historyBusy) return;
    historyBusy = true;
    try { H = await get(`api/vram/history?range=${range}`); historyAt = Date.now(); drawHistory(); }
    catch (e) { H = null; $("#vh-empty").hidden = false; }
    finally { historyBusy = false; }
  }
  const rangeBox = $("#vh-range");
  RANGES.forEach(([s, label]) => {
    const b = el("button", "", label); b.type = "button"; b.dataset.s = s;
    b.setAttribute("aria-pressed", String(s === range));
    b.onclick = () => {
      range = s; keep("tadamoo.vram.range", String(s));
      rangeBox.querySelectorAll("button").forEach(x => x.setAttribute("aria-pressed", String(x === b)));
      hover = null; loadHistory();
    };
    rangeBox.append(b);
  });
  chart.addEventListener("pointermove", e => {
    const n = H && H.gpus && H.gpus[0] ? H.gpus[0].used.length : 0;
    if (!n) return;
    const r = chart.getBoundingClientRect();
    hover = Math.min(n - 1, Math.max(0, Math.round((e.clientX - r.left) / r.width * (n - 1))));
    drawHistory();
  });
  chart.addEventListener("pointerleave", () => { hover = null; drawHistory(); });

  /* ---- the Explorer ---- */
  const pane = $("#pane-vram"), root = $("#vx-root");
  let gpu = 0, X = null, exploring = false, exploreAt = 0, built = false;
  const ui = {};

  function build() {
    built = true;
    const top = el("div", "vx-top");
    ui.pick = el("select"); ui.pick.setAttribute("aria-label", "GPU");
    ui.pick.onchange = () => { gpu = Number(ui.pick.value); X = null; refresh(true); };
    ui.sum = el("div", "vx-sum");
    top.append(ui.pick, ui.sum);
    ui.bar = el("div", "vx-bar"); ui.bar.setAttribute("aria-hidden", "true");
    ui.leg = el("div", "leg vx-leg");
    ui.notes = el("div", "vx-notes");
    const wrap = el("div", "tw");
    const table = el("table");
    const head = el("tr");
    [["Process", ""], ["Kind", ""], ["VRAM", "n"], ["Shared", "n"], ["GPU", "n"], ["Inside", ""]].forEach(([label, cls]) => head.append(el("th", cls, label)));
    table.append(el("thead")); table.firstChild.append(head);
    ui.rows = el("tbody");
    table.append(ui.rows);
    ui.empty = el("div", "empty", "Reading the GPU...");
    wrap.append(table, ui.empty);
    ui.foot = el("p", "vx-foot", "This table reads the operating system's per-process counters; the chart and the header above read NVML, so their totals can differ by a few hundred MiB. ~ marks an estimate or an inference, n/a a value this system does not give.");
    root.append(top, ui.bar, ui.leg, ui.notes, wrap, ui.foot);
  }

  const shown = p => isNum(p.vram_bytes) ? p.vram_bytes : (isNum(p.vram_inferred_bytes) ? p.vram_inferred_bytes : null);

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
    const procs = (d.processes || []).map(p => ({ p, v: shown(p) })).sort((a, b) => (b.v || 0) - (a.v || 0));
    const used = isNum(d.vram_used) ? d.vram_used : 0, base = isNum(total) && total > 0 ? total : Math.max(used, 1);
    ui.bar.replaceChildren(); ui.leg.replaceChildren();
    const part = (label, v, hue, mark) => {
      if (!isNum(v) || v <= 0) return;
      const seg = el("i"); seg.style.width = (v / base * 100) + "%"; seg.style.background = hue; seg.title = `${label}: ${mb(v)}`;
      ui.bar.append(seg);
      const item = el("span"); item.style.setProperty("--c", hue);
      item.append(`${label} `, el("b", "", `${mark || ""}${mb(v)}`));
      ui.leg.append(item);
    };
    const big = procs.filter(x => isNum(x.v) && x.v >= base * 0.02).slice(0, 6);
    big.forEach((x, i) => part(x.p.name, x.v, `hsl(${(205 + i * 47) % 360} 55% 52%)`, isNum(x.p.vram_bytes) ? "" : "~"));
    const rest = procs.filter(x => !big.includes(x)).reduce((a, x) => a + (x.p.vram_bytes || 0), 0);
    part("Other processes", rest, "var(--fg-3)");
    part("Not attributed", Math.max(0, used - (d.attributed || 0)), "var(--warn)", "~");
    ui.notes.replaceChildren(...[d.counter_status, d.process_status, d.host_status, d.memory_note].filter(Boolean).map(t => el("p", "", t)));
    ui.rows.replaceChildren();
    procs.forEach(({ p, v }) => {
      const tr = el("tr");
      const name = el("td", "f", p.name); name.title = `PID ${p.pid}`;
      name.append(el("small", "", ` ${p.pid}`));
      const mark = isNum(p.vram_bytes) ? "" : "~";
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
      tr.append(name, cell(p.kind || ""), cell(v == null ? "n/a" : `${mark}${mb(v)}`, "n f"), cell(mb(p.shared_bytes), "n"),
        cell(isNum(p.gpu_pct) ? `${p.gpu_pct.toFixed(1)}%` : "n/a", "n"), inside);
      ui.rows.append(tr);
    });
    ui.empty.hidden = procs.length > 0;
    ui.empty.textContent = "No process is using this GPU.";
    PANE.vram = String(procs.length);
    text("#vx-count", PANE.vram);
    if (curPane() === "vram") text("#pane-count", PANE.vram);
  }

  async function refresh(force) {
    if (exploring || pane.hidden) return;
    if (!force && Date.now() - exploreAt < EXPLORER_MS) return;
    exploring = true;
    try { X = await get(`api/vram/explorer?gpu=${gpu}`); exploreAt = Date.now(); drawExplorer(); }
    catch (e) { X = { error: `The VRAM Explorer could not read the GPU (${e.message}).`, gpus: [] }; drawExplorer(); }
    finally { exploring = false; }
  }

  document.querySelector("#tab-vram").addEventListener("click", () => { if (!built) build(); refresh(true); });
  setInterval(() => {
    if (!onStats()) return;
    if (Date.now() - historyAt >= HISTORY_MS) loadHistory();
    if (!pane.hidden) { if (!built) build(); refresh(false); }
  }, 1000);
  document.addEventListener("tadamoo:view", e => { if (e.detail === "stats") loadHistory(); });
  loadHistory();
})();
