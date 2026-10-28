"use strict";
/* Models > the engine tabs (RTX 30/40, Mac, AMD, Intel, Unified memory): one tab per engine the hardware of this PC
   supports, next to the Qwen Loader, which stays as it is. In this version the other engines are not installable yet:
   each tab says what the engine is, whether this PC fits it, whether it is installed, and how memory is split. The server
   decides what is shown (GET api/loader/engines); "All engines" lists every tab, also those this PC cannot run. */
(() => {
  const SUB_KEY = "basalt.models.sub", ALL_KEY = "basalt.engines.all";
  const view = document.getElementById("view-loader");
  const strip = view && view.querySelector(".subtabs");
  const anchor = document.getElementById("ld-qwen");
  if (!view || !strip || !anchor) return;
  const load = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* this page only */ } };
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const tr = (key, ...args) => { const t = T(key, ...args); return t === key ? "" : t; };
  const BUILT_IN = ["providers", "qwen", "converter"];

  let data = null, showAll = load(ALL_KEY, "0") === "1", current = "";
  const notes = {}, buttons = new Map(), panels = new Map();
  const allBtn = h("button", "btn subtab eng-all");
  allBtn.type = "button";

  async function call(method, path) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...authHeaders() } });
      let body = {};
      try { body = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data: body };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function select(id) {
    current = id;
    for (const [key, b] of buttons) b.setAttribute("aria-selected", String(key === id));
    for (const [key, p] of panels) p.hidden = key !== id;
    if (!id) return;
    for (const name of BUILT_IN) {
      const t = document.getElementById("sub-" + name);
      if (t) t.setAttribute("aria-selected", "false");
    }
    for (const pid of ["ld-providers", "ld-qwen", "ld-converter"]) { const p = document.getElementById(pid); if (p) p.hidden = true; }
    keep(SUB_KEY, "engine:" + id);
  }

  async function install(tab, out) {
    const res = await call("POST", `api/loader/engines/${encodeURIComponent(tab.engine)}/install`);
    const err = (res.data && res.data.error) || {};
    if (res.status === 0) out.textContent = T("eng.e.offline");
    else if (res.ok) out.textContent = T("eng.state.installed");
    else out.textContent = tr("eng.e." + err.code) || err.message || `HTTP ${res.status}`;
    out.dataset.bad = res.ok ? "" : "1";
    out.hidden = false;
    notes[tab.id] = out.textContent;
  }

  const row = (label, value) => [h("dt", "", label), h("dd", "", value)];

  function fill(tab, panel) {
    const eng = (data.engines || []).find(e => e.id === tab.engine) || {};
    const hw = data.hardware || {};
    const gpus = (hw.gpus || []).map(g => g.name || g.vendor).join(", ");
    const why = tab.reason_code ? (tr("eng.why." + tab.reason_code) || tab.reason || "") : "";
    const status = h("p", "eng-status", tab.compatible ? T("eng.compat.yes") : T("eng.compat.no", why));
    status.dataset.ok = tab.compatible ? "1" : "";
    status.setAttribute("role", "status");
    const out = h("p", "setnote eng-note", notes[tab.id] || "");
    out.setAttribute("role", "status");
    out.hidden = !notes[tab.id];
    const kids = [h("h3", "eng-title", tr("eng.tab." + tab.id) || tab.label), h("p", "setnote eng-desc", tr("eng.desc." + tab.id)), status,
      h("dl", "eng-facts", row(T("eng.engine"), eng.label || tab.engine),
        row(T("eng.state"), tab.installed ? T("eng.state.installed") : T("eng.state.not")),
        row(T("eng.hw"), gpus || T("eng.hw.none")), row(T("eng.models"), (eng.models || []).join(", ")))];
    if (!tab.installed) {
      const b = h("button", "btn eng-install", T("eng.install"));
      b.type = "button";
      b.disabled = !tab.compatible;
      b.onclick = () => install(tab, out);
      kids.push(h("div", "eng-actions", b), h("p", "setnote eng-help", T("eng.install.help")));
    }
    kids.push(out);
    kids.push(h("p", "setnote eng-mem", T(hw.unified_memory === true ? "eng.mem.single" : "eng.mem.split")),
      h("p", "setnote eng-help", T("eng.all.help")));
    panel.replaceChildren(...kids);
  }

  function labelAll() {
    allBtn.textContent = T("eng.all");
    allBtn.title = T("eng.all.help");
    allBtn.setAttribute("aria-pressed", String(showAll));
  }

  function draw() {
    for (const b of buttons.values()) b.remove();
    for (const p of panels.values()) p.remove();
    buttons.clear(); panels.clear();
    labelAll();
    let after = anchor;
    for (const tab of (data && data.tabs) || []) {
      if (tab.builtin) continue;
      const b = h("button", "btn subtab", tr("eng.tab." + tab.id) || tab.label);
      b.type = "button";
      b.id = "sub-eng-" + tab.id;
      b.setAttribute("role", "tab");
      b.setAttribute("aria-controls", "ld-eng-" + tab.id);
      b.setAttribute("aria-selected", "false");
      if (!tab.compatible) b.dataset.incompatible = "1";
      b.onclick = () => select(tab.id);
      const p = h("div", "eng-panel");
      p.id = "ld-eng-" + tab.id;
      p.setAttribute("role", "tabpanel");
      p.setAttribute("aria-labelledby", b.id);
      p.hidden = true;
      fill(tab, p);
      strip.insertBefore(b, allBtn);
      after.after(p);
      after = p;
      buttons.set(tab.id, b);
      panels.set(tab.id, p);
    }
    if (current && !panels.has(current)) current = "";
    if (current) select(current);
  }

  async function refresh() {
    const res = await call("GET", "api/loader/engines" + (showAll ? "?all=1" : ""));
    if (!res.ok) return;
    data = res.data;
    draw();
  }

  allBtn.onclick = () => { showAll = !showAll; keep(ALL_KEY, showAll ? "1" : "0"); refresh(); };
  strip.append(allBtn);
  labelAll();

  document.addEventListener("basalt:models-sub", e => {
    if (!BUILT_IN.includes(e.detail)) return;
    current = "";
    for (const b of buttons.values()) b.setAttribute("aria-selected", "false");
    for (const p of panels.values()) p.hidden = true;
  });
  document.addEventListener("basalt:lang", () => { if (data) draw(); else labelAll(); });

  const saved = load(SUB_KEY, "qwen");
  if (saved.startsWith("engine:")) current = saved.slice(7);
  refresh();
})();
