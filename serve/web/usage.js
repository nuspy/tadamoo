"use strict";
/* Models > Usage: the tokens every model call used, for every provider (cloud, a server on this PC, the local engine), over a fixed period (last hour, 12 hours, day, week,
   month, all) or over one given by date and time. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("ld-usage");
  if (!root) return;
  const PRESETS = [["1h", 3600], ["12h", 12 * 3600], ["1d", 86400], ["1w", 7 * 86400], ["1m", 30 * 86400], ["all", 0]];
  let pick = "1d", range = null, data = null, note = "", timer = null;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(path) {
    try {
      const r = await fetch(path, { cache: "no-store", headers: { ...authHeaders() } });
      let body = {};
      try { body = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data: body };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const num = n => Number(n || 0).toLocaleString();
  const when = t => new Date(t * 1000).toLocaleString();
  const local = name => name === "Local engine" ? T("us.local") : name;

  function query() {
    const now = Date.now() / 1000;
    if (range) return `api/usage?from=${range.from}&to=${range.to}`;
    const span = (PRESETS.find(p => p[0] === pick) || PRESETS[2])[1];
    return span ? `api/usage?from=${Math.floor(now - span)}&to=${Math.ceil(now)}` : `api/usage?to=${Math.ceil(now)}`;
  }
  async function load() {
    const r = await call(query());
    if (r.ok) { data = r.data; note = ""; draw(); } else { note = r.status === 0 ? T("us.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`); draw(); }
  }

  function tile(label, value) { return h("div", "us-tile", h("span", "us-tile-k", label), h("b", "us-tile-v", num(value))); }
  function share(value, total) { const bar = h("span", "us-share"); bar.setAttribute("aria-hidden", "true"); bar.style.setProperty("--w", total ? Math.max(1, Math.round(100 * value / total)) + "%" : "0%"); return bar; }

  function table(title, rows, label, key) {
    if (!rows.length) return null;
    const total = data.totals.total || 1;
    const head = h("tr", "", ["", T("us.calls"), T("us.in"), T("us.out"), T("us.cached"), T("us.all")].map((x, i) => Object.assign(h("th", i ? "us-n" : "", x), { scope: "col" })));
    const body = rows.map(r => h("tr", "", Object.assign(h("th", "", label(r)), { scope: "row" }), h("td", "us-n", num(r.calls)), h("td", "us-n", num(r.prompt)), h("td", "us-n", num(r.completion)),
      h("td", "us-n", num(r.cached)), h("td", "us-n", h("span", "", num(r.total)), share(r.total, total))));
    return h("section", "us-sec", h("h3", "", title), h("div", "pv-scroll", h("table", "us-table", h("thead", "", head), h("tbody", "", body))));
  }

  function barWidth(s) { return s < 3600 ? T("us.bar.m", Math.round(s / 60)) : s < 86400 ? T("us.bar.h", Math.round(s / 3600)) : T("us.bar.d", Math.round(s / 86400)); }
  function chart() {
    if (!data.bars.length) return null;
    const bars = data.bars.map(b => ({ t: b.t, total: Object.values(b.by_provider).reduce((a, c) => a + c, 0) }));
    const top = Math.max(...bars.map(b => b.total), 1);
    const row = h("div", "us-bars");
    row.setAttribute("role", "img");
    row.setAttribute("aria-label", T("us.chart") + ": " + bars.map(b => `${when(b.t)}: ${num(b.total)}`).join("; "));
    bars.forEach(b => {
      const col = h("span", "us-bar"); col.style.setProperty("--h", Math.max(2, Math.round(100 * b.total / top)) + "%"); col.title = `${when(b.t)}: ${num(b.total)}`;
      row.append(col);
    });
    return h("section", "us-sec", h("h3", "", T("us.chart")), h("p", "setnote kn-help", T("us.chart.help", barWidth(data.bar_s))), row,
      h("p", "setnote us-axis", h("span", "", when(bars[0].t)), h("span", "", when(bars[bars.length - 1].t))));
  }

  function draw() {
    const presets = h("div", "us-presets", PRESETS.map(([k]) => {
      const b = h("button", "btn" + (!range && pick === k ? " primary" : ""), T("us.p." + k)); b.type = "button"; b.setAttribute("aria-pressed", String(!range && pick === k));
      b.onclick = () => { pick = k; range = null; load(); }; return b;
    }));
    const from = h("input"); from.type = "datetime-local"; from.id = "us-from"; const to = h("input"); to.type = "datetime-local"; to.id = "us-to";
    const stamp = t => { const d = new Date(t * 1000 - new Date().getTimezoneOffset() * 60000); return d.toISOString().slice(0, 16); };
    if (range) { from.value = stamp(range.from); to.value = stamp(range.to); } else { to.value = stamp(Date.now() / 1000); from.value = stamp(Date.now() / 1000 - 86400); }
    const apply = h("button", "btn", T("us.apply")); apply.type = "button";
    apply.onclick = () => {
      const a = new Date(from.value).getTime() / 1000, z = new Date(to.value).getTime() / 1000;
      if (!isFinite(a) || !isFinite(z) || a >= z) { note = T("us.range.bad"); draw(); return; }
      range = { from: Math.floor(a), to: Math.ceil(z) }; load();
    };
    const fl = (id, label, input) => { const l = h("label", "", label); l.htmlFor = id; return h("div", "kn-field", l, input); };
    const status = h("p", "setnote warnnote", note); status.setAttribute("role", "status"); status.hidden = !note;

    const parts = [h("h2", "", T("us.title")), h("p", "setnote kn-help", T("us.help")), presets,
      h("fieldset", "csgroup us-range", h("legend", "", T("us.range")), fl("us-from", T("us.from"), from), fl("us-to", T("us.to"), to), h("div", "kn-actions", apply)), status];
    if (data) {
      parts.push(h("p", "setnote us-period", T("us.period", when(data.from), when(data.to))));
      if (!data.totals.calls) parts.push(h("p", "setnote", T("us.none")));
      else parts.push(h("div", "us-tiles", tile(T("us.calls"), data.totals.calls), tile(T("us.in"), data.totals.prompt), tile(T("us.out"), data.totals.completion),
        tile(T("us.cached"), data.totals.cached), tile(T("us.total"), data.totals.total)),
        chart(), table(T("us.by.provider"), data.providers, r => local(r.name)),
        table(T("us.by.model"), data.models, r => `${local(r.name)} · ${r.model || "?"}`),
        table(T("us.by.function"), data.functions, r => r.function || T("us.fn.none")));
    }
    root.replaceChildren(...parts.filter(Boolean));
  }

  const open = () => !root.hidden;
  document.addEventListener("basalt:models-sub", e => { if (e.detail === "usage") load(); });
  document.addEventListener("tadamoo:lang", () => { if (open()) draw(); });
  timer = setInterval(() => { if (open() && !range) load(); }, 30000);
})();
