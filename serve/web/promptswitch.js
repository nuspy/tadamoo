"use strict";
/* The Prompt analysis switch: how a request is looked at before the model works (off, rules, full). There is one value; every place that
   hosts the switch (the Chat toolbar, Settings > Inference) shows it, and a change in one is drawn in all at once and saved through
   api/intake. A host is an element with class "ps" and the id of its explanation panel in data-panel. */
(() => {
  const MODES = ["off", "rules", "full"], DEFAULT = "off", SAVE_DELAY_MS = 300;
  const hosts = [];
  let mode = DEFAULT, saved = DEFAULT, loaded = false, note = "", noteBad = false, timer = 0;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("kn.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));

  /* ---- drawing ---- */
  function build(host, n) {
    const key = host.id, label = h("span", host.dataset.label === "off" ? "ps-label sr" : "ps-label", T("ps.title"));
    label.id = key + "-l";
    const seg = h("div", "ps-seg"); seg.setAttribute("role", "radiogroup"); seg.setAttribute("aria-labelledby", label.id);
    const descs = MODES.map(m => { const d = h("span", "sr", T("ps." + m + ".d")); d.id = `${key}-d-${m}`; return d; });
    MODES.forEach((m, i) => {
      const b = h("button", "ps-opt", T("ps." + m)); b.type = "button"; b.dataset.mode = m;
      b.setAttribute("role", "radio"); b.title = T("ps." + m + ".d"); b.setAttribute("aria-describedby", descs[i].id);
      b.addEventListener("click", () => choose(m));
      seg.append(b);
    });
    seg.addEventListener("keydown", e => {
      const opts = [...seg.querySelectorAll(".ps-opt")], i = opts.indexOf(document.activeElement);
      const to = { ArrowRight: i + 1, ArrowDown: i + 1, ArrowLeft: i - 1, ArrowUp: i - 1, Home: 0, End: opts.length - 1 }[e.key];
      if (i < 0 || to === undefined) return;
      e.preventDefault();
      const next = opts[(to + opts.length) % opts.length];
      next.focus();
      choose(next.dataset.mode);
    });
    const panelId = host.dataset.panel;
    const help = h("button", "ps-help", "?"); help.type = "button"; help.setAttribute("aria-expanded", "false"); help.setAttribute("aria-controls", panelId);
    help.title = T("ps.help"); help.setAttribute("aria-label", T("ps.help"));
    help.addEventListener("click", () => { const p = document.getElementById(panelId); p.hidden = !p.hidden; paintOne(host); });
    const status = h("span", "ps-status", note); status.setAttribute("role", "status");
    host.replaceChildren(label, seg, help, ...descs, status);
    host.dataset.built = String(n);
  }
  function paintPanel(host) {
    const panel = document.getElementById(host.dataset.panel);
    if (!panel) return;
    const help = host.querySelector(".ps-help");
    help.setAttribute("aria-expanded", String(!panel.hidden));
    if (panel.hidden) return;
    const dl = h("dl", "ps-defs");
    MODES.forEach(m => {
      const now = m === mode;
      const dt = h("dt", "", T("ps." + m), now ? h("span", "ps-now", " - " + T("ps.current")) : null);
      if (now) dt.setAttribute("aria-current", "true");
      dl.append(dt, h("dd", "", T("ps." + m + ".d")));
    });
    panel.replaceChildren(dl);
  }
  function paintOne(host) {
    if (host.dataset.built !== LANG) build(host, LANG);
    host.querySelectorAll(".ps-opt").forEach(b => {
      const on = b.dataset.mode === mode;
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
      b.disabled = !loaded;
    });
    const status = host.querySelector(".ps-status");
    status.textContent = note; status.dataset.bad = noteBad ? "1" : "";
    paintPanel(host);
  }
  const paint = () => hosts.forEach(paintOne);

  /* ---- the value ---- */
  async function flush() {
    timer = 0;
    const want = mode;
    if (want === saved) return;
    const r = await call("POST", "api/intake", { intake: want });
    if (r.ok) { saved = r.data.intake; if (mode === want) { note = T("kn.saved"); noteBad = false; } } else if (mode === want) { mode = saved; note = why(r); noteBad = true; }
    paint();
  }
  function choose(m) {
    if (!loaded || !MODES.includes(m) || m === mode) return;
    mode = m; note = ""; noteBad = false;
    paint();
    clearTimeout(timer);
    timer = setTimeout(flush, SAVE_DELAY_MS);                          // arrowing across the three states saves where it stops
  }
  function sync(m) {                                                   // the value another part of the page read or saved
    if (!MODES.includes(m)) return;
    if (!timer) mode = m;
    saved = m; loaded = true;
    paint();
  }
  async function load() {
    const r = await call("GET", "api/intake");
    if (r.ok) { sync(r.data.intake); note = ""; noteBad = false; } else { note = why(r); noteBad = true; }
    paint();
  }

  document.querySelectorAll(".ps[data-panel]").forEach(el => hosts.push(el));
  document.addEventListener("tadamoo:lang", paint);
  document.addEventListener("tadamoo:view", () => { if (!loaded) load(); });
  document.addEventListener("click", e => {                            // a click elsewhere closes an open explanation
    hosts.forEach(host => {
      const panel = document.getElementById(host.dataset.panel);
      if (panel && !panel.hidden && !host.contains(e.target) && !panel.contains(e.target)) { panel.hidden = true; paintOne(host); }
    });
  });
  document.addEventListener("keydown", e => {
    if (e.key !== "Escape") return;
    hosts.forEach(host => {
      const panel = document.getElementById(host.dataset.panel);
      if (panel && !panel.hidden && (host.contains(document.activeElement) || panel.contains(document.activeElement))) {
        panel.hidden = true; paintOne(host); host.querySelector(".ps-help").focus();
      }
    });
  });

  window.PromptSwitch = { mode: () => mode, sync, choose };
  paint();
  load();
})();
