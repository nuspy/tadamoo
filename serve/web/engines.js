"use strict";
/* Models > the engine tabs (RTX 30/40, Mac, AMD, Intel, Unified memory): one tab per engine the hardware of this PC
   supports, next to the Qwen Loader, which stays as it is. A tab installs its engine from a folder or archive the person
   points to, loads a model into it (only the controls the engine supports are shown) and stops it. The server decides
   what is shown (GET api/loader/engines); "All engines" lists every tab, also those this PC cannot run. */
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
  const BUILT_IN = ["providers", "qwen", "converter", "llamacpp", "voicesvc"];
  const DETAIL = ["start_failed", "start_timeout", "load_failed", "flag_unsupported", "control_unsupported", "in_use", "copy_failed",
    "source_incomplete", "source_missing", "config_unreadable", "model_unsupported", "spawn_failed"];
  const KV = ["fp16", "int8", "q4_0", "k8v4"];
  // control: its input kind; the engine tells which of them its model family supports
  const CONTROLS = { max_context: ["number", { min: 1 }], kv: ["kv"], spec: ["number", { min: 0 }], mtp: ["number", { min: 0 }],
    expert_cache: ["number", { min: 0 }], vram_reserve: ["number", { min: 0 }], parallel: ["number", { min: 1, max: 8 }],
    tokenizer: ["text"] };

  let data = null, showAll = load(ALL_KEY, "0") === "1", current = "", signature = "", timer = 0;
  const buttons = new Map(), panels = new Map(), ui = new Map(), notes = {};
  const allBtn = h("button", "btn subtab eng-all");
  allBtn.type = "button";

  async function call(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store",
        headers: { ...authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
      let out = {};
      try { out = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data: out };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function say(res) {
    const err = (res.data && res.data.error) || {};
    if (res.status === 0) return T("eng.e.offline");
    const known = tr("eng.e." + err.code);
    if (known && DETAIL.includes(err.code)) return `${known} ${err.message || ""}`.trim();
    return known || tr("eng.why." + err.code) || err.message || `HTTP ${res.status}`;
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
    for (const pid of ["ld-providers", "ld-qwen", "ld-converter", "ld-llamacpp", "ld-voicesvc"]) { const p = document.getElementById(pid); if (p) p.hidden = true; }
    keep(SUB_KEY, "engine:" + id);
  }

  const row = (label, value) => [h("dt", "", label), h("dd", "", value)];
  const engineOf = tab => (data.engines || []).find(e => e.id === tab.engine) || {};

  // one labelled control with its explanation
  function field(id, label, help, input) {
    const lab = h("label", "", label);
    input.id = id;
    lab.htmlFor = id;
    const note = h("p", "help", help);
    note.id = id + "-help";
    input.setAttribute("aria-describedby", note.id);
    return h("div", "field", lab, input, note);
  }

  function control(tab, name) {
    const [kind, attrs] = CONTROLS[name];
    let input;
    if (kind === "kv") {
      input = h("select", "", Object.assign(h("option", "", T("eng.default")), { value: "" }),
        KV.map(v => Object.assign(h("option", "", v), { value: v })));
    } else {
      input = h("input");
      input.type = kind === "number" ? "number" : "text";
      if (kind === "number") input.inputMode = "numeric";
      Object.assign(input, attrs || {});
      input.placeholder = T("eng.default");
    }
    input.dataset.control = name;
    return field(`eng-${tab.id}-${name}`, T("eng.c." + name), T("eng.c." + name + ".help"), input);
  }

  function values(box) {
    const out = {};
    box.querySelectorAll("[data-control]").forEach(i => {
      if (i.closest("[hidden]") || i.value === "") return;
      out[i.dataset.control] = i.type === "number" ? Number(i.value) : i.value;
    });
    return out;
  }

  function showError(tab, text, bad) {
    notes[tab.id] = text;
    const o = ui.get(tab.id);
    if (o) { o.note.textContent = text; o.note.dataset.bad = bad ? "1" : ""; o.note.hidden = !text; }
  }

  async function install(tab) {
    const o = ui.get(tab.id);
    const src = o.source.value.trim();
    const res = await call("POST", `api/loader/engines/${encodeURIComponent(tab.engine)}/install`,
      { source: src || undefined, remember: !!src && o.remember.checked });
    if (res.ok && res.status === 202) { showError(tab, "", false); poll(); }
    else showError(tab, res.ok ? T("eng.state.installed") : say(res), !res.ok);
  }

  async function startLoad(tab) {
    const o = ui.get(tab.id);
    const model = o.custom.value.trim() || o.model.value;
    const res = await call("POST", `api/loader/engines/${encodeURIComponent(tab.engine)}/load`, { model, values: values(o.controls) });
    showError(tab, res.ok ? "" : say(res), !res.ok);
    if (res.ok) poll();
  }

  async function stop(tab) {
    const res = await call("POST", `api/loader/engines/${encodeURIComponent(tab.engine)}/stop`, {});
    showError(tab, res.ok ? "" : say(res), !res.ok);
    refresh();
  }

  async function saveProfile(tab) {
    const o = ui.get(tab.id);
    const name = o.pname.value.trim();
    const model = o.custom.value.trim() || o.model.value;
    const res = await call("POST", "api/profiles", { action: "save", name, engine: tab.engine, model, values: values(o.controls) });
    const err = (res.data && res.data.error) || {};
    showError(tab, res.ok ? T("eng.profile.saved") : (err.message || say(res)), !res.ok);
    if (res.ok) loadModels(tab);
  }

  async function deleteProfile(tab) {
    const o = ui.get(tab.id);
    if (!o.profile.value) return;
    const res = await call("POST", "api/profiles", { action: "delete", name: o.profile.value });
    showError(tab, res.ok ? "" : ((res.data.error || {}).message || say(res)), !res.ok);
    loadModels(tab);
  }

  // the controls shown follow the chosen model's family (all of them for a typed path)
  function applyFamily(tab) {
    const o = ui.get(tab.id), eng = engineOf(tab);
    const picked = (o.models || []).find(m => m.path === o.model.value);
    const fam = !o.custom.value.trim() && picked ? (eng.model_families || {})[picked.family] : null;
    const allowed = new Set(fam ? fam.controls : eng.controls || []);
    o.controls.querySelectorAll(".field").forEach(f => {
      const name = f.querySelector("[data-control]").dataset.control;
      f.hidden = name !== "tokenizer" && !allowed.has(name);
    });
  }

  async function loadModels(tab) {
    const res = await call("GET", `api/loader/engines/${encodeURIComponent(tab.engine)}/models`);
    const o = ui.get(tab.id);
    if (!o || !res.ok) return;
    o.models = res.data.models || [];
    o.model.replaceChildren(...o.models.map(m => {
      const why = m.ok ? "" : ` (${tr("eng.model.bad." + m.reason) || m.reason})`;
      const opt = h("option", "", `${m.name} - ${tr("eng.fam." + m.family) || m.family}${why}`);
      opt.value = m.path;
      opt.disabled = !m.ok;
      return opt;
    }));
    o.empty.hidden = o.models.length > 0;
    o.model.hidden = o.models.length === 0;
    const profiles = res.data.profiles || [];
    o.profile.replaceChildren(Object.assign(h("option", "", T("eng.profile.none")), { value: "" }),
      ...profiles.map(p => Object.assign(h("option", "", p.name), { value: p.name })));
    o.profiles = profiles;
    applyFamily(tab);
  }

  function useProfile(tab) {
    const o = ui.get(tab.id), p = (o.profiles || []).find(x => x.name === o.profile.value);
    if (!p) return;
    o.pname.value = p.name;
    if (p.model) {
      if ((o.models || []).some(m => m.path === p.model)) { o.model.value = p.model; o.custom.value = ""; } else o.custom.value = p.model;
    }
    o.controls.querySelectorAll("[data-control]").forEach(i => { i.value = p.values[i.dataset.control] ?? ""; });
    applyFamily(tab);
  }

  function installSection(tab, eng) {
    const o = ui.get(tab.id);
    const source = h("input");
    source.type = "text";
    source.placeholder = "E:\\...\\build";
    source.value = eng.source || "";
    const remember = h("input");
    remember.type = "checkbox";
    remember.checked = true;
    o.bar = h("progress", "eng-bar");
    o.bar.max = 100;
    o.bar.hidden = true;
    o.go = h("button", "btn eng-install", T("eng.install"));
    o.go.type = "button";
    o.go.onclick = () => install(tab);
    o.source = source;
    o.remember = remember;
    return h("div", "eng-section", h("h4", "eng-sub", T("eng.install.title")),
      h("div", "fields", field(`eng-${tab.id}-src`, T("eng.src"), T("eng.src.help"), source),
        h("div", "field", h("label", "eng-check", remember, " ", T("eng.remember")), h("p", "help", T("eng.remember.help")))),
      h("div", "eng-actions", o.go), o.bar, h("p", "setnote eng-help", T("eng.install.help")));
  }

  function loadSection(tab) {
    const o = ui.get(tab.id);
    o.model = h("select");
    o.model.id = `eng-${tab.id}-model`;
    o.model.setAttribute("aria-describedby", `eng-${tab.id}-model-help`);
    o.model.onchange = () => applyFamily(tab);
    o.empty = h("p", "help", T("eng.model.none"));
    o.custom = h("input");
    o.custom.type = "text";
    o.custom.oninput = () => applyFamily(tab);
    o.profile = h("select");
    o.profile.onchange = () => useProfile(tab);
    o.pname = h("input");
    o.pname.type = "text";
    o.controls = h("div", "fields", Object.keys(CONTROLS).map(name => control(tab, name)));
    const modelHelp = h("p", "help", T("eng.model.help"));
    modelHelp.id = `eng-${tab.id}-model-help`;
    const lab = h("label", "", T("eng.model"));
    lab.htmlFor = o.model.id;
    const pick = h("div", "fields", h("div", "field", lab, o.model, o.empty, modelHelp),
      field(`eng-${tab.id}-path`, T("eng.model.path"), T("eng.model.path.help"), o.custom));
    const prof = h("div", "fields", field(`eng-${tab.id}-prof`, T("eng.profile"), T("eng.profile.help"), o.profile),
      field(`eng-${tab.id}-pname`, T("eng.profile.name"), T("eng.profile.name.help"), o.pname));
    const mk = (label, fn, cls) => { const b = h("button", "btn " + (cls || ""), label); b.type = "button"; b.onclick = fn; return b; };
    o.loadBtn = mk(T("eng.load"), () => startLoad(tab), "eng-load");
    o.stopBtn = mk(T("eng.stop"), () => stop(tab), "eng-stop");
    return h("div", "eng-section", h("h4", "eng-sub", T("eng.load.title")), pick, o.controls, prof,
      h("div", "eng-actions", o.loadBtn, o.stopBtn, mk(T("eng.profile.save"), () => saveProfile(tab)),
        mk(T("eng.profile.delete"), () => deleteProfile(tab))),
      h("p", "setnote eng-help", T("eng.load.help")));
  }

  function fill(tab, panel) {
    const eng = engineOf(tab);
    const o = {};
    ui.set(tab.id, o);
    const why = tab.reason_code ? (tr("eng.why." + tab.reason_code) || tab.reason || "") : "";
    const status = h("p", "eng-status", tab.compatible ? T("eng.compat.yes") : T("eng.compat.no", why));
    status.dataset.ok = tab.compatible ? "1" : "";
    status.setAttribute("role", "status");
    o.note = h("p", "setnote eng-note", notes[tab.id] || "");
    o.note.setAttribute("role", "status");
    o.note.hidden = !notes[tab.id];
    o.state = h("dd", "");
    o.run = h("p", "setnote eng-run");
    o.run.setAttribute("role", "status");
    const gpus = ((data.hardware || {}).gpus || []).map(g => g.name || g.vendor).join(", ");
    const kids = [h("h3", "eng-title", tr("eng.tab." + tab.id) || tab.label), h("p", "setnote eng-desc", tr("eng.desc." + tab.id)), status,
      h("dl", "eng-facts", row(T("eng.engine"), eng.label || tab.engine), [h("dt", "", T("eng.state")), o.state],
        row(T("eng.hw"), gpus || T("eng.hw.none")), row(T("eng.models"), (eng.models || []).join(", ")))];
    if (tab.compatible && tab.engine === "strata") {
      kids.push(installSection(tab, eng));
      if (tab.installed) kids.push(loadSection(tab));
    }
    kids.push(o.run, o.note, h("p", "setnote eng-mem", T((data.hardware || {}).unified_memory === true ? "eng.mem.single" : "eng.mem.split")),
      h("p", "setnote eng-help", T("eng.all.help")));
    panel.replaceChildren(...kids);
    if (o.model) loadModels(tab);
  }

  // what changes while an install or a load runs, without rebuilding the form
  function paint(tab) {
    const o = ui.get(tab.id), eng = engineOf(tab);
    if (!o) return;
    const st = eng.state === "not_installed" || !eng.installed ? "not" : eng.state;
    o.state.textContent = tr("eng.state." + st) || st;
    const job = eng.install;
    if (o.bar) {
      const busy = job && job.status === "running";
      o.bar.hidden = !(busy || (job && job.status === "done"));
      o.bar.value = job && job.total ? Math.round(100 * job.copied / job.total) : 0;
      o.go.disabled = !!busy;
      if (busy) showError(tab, T("eng.installing", Math.round(job.copied / 1048576), Math.round(job.total / 1048576), job.file || ""), false);
      else if (job && job.status === "error") showError(tab, say({ data: { error: job.error }, status: 400 }), true);
    }
    const running = eng.state === "running", loading = eng.state === "starting" || eng.state === "loading";
    if (o.stopBtn) {
      o.stopBtn.disabled = !(running || loading || eng.state === "error");
      o.loadBtn.disabled = loading;
    }
    o.run.textContent = running ? T("eng.running", eng.label) : loading ? T("eng.loading.model") :
      eng.state === "error" && eng.error ? say({ data: { error: eng.error }, status: 400 }) : "";
    o.run.dataset.bad = eng.state === "error" ? "1" : "";
  }

  function labelAll() {
    allBtn.textContent = T("eng.all");
    allBtn.title = T("eng.all.help");
    allBtn.setAttribute("aria-pressed", String(showAll));
  }

  function paintAll() { for (const tab of (data && data.tabs) || []) if (!tab.builtin) paint(tab); }

  function draw() {
    for (const b of buttons.values()) b.remove();
    for (const p of panels.values()) p.remove();
    buttons.clear(); panels.clear(); ui.clear();
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
    paintAll();
  }

  const sign = d => JSON.stringify([(d.tabs || []).map(t => [t.id, t.installed, t.compatible]), showAll]);

  async function refresh() {
    const res = await call("GET", "api/loader/engines" + (showAll ? "?all=1" : ""));
    if (!res.ok) return;
    data = res.data;
    if (sign(data) !== signature) { signature = sign(data); draw(); } else paintAll();
    const busy = (data.engines || []).some(e => e.state === "starting" || e.state === "loading" || (e.install && e.install.status === "running"));
    if (busy && !timer) timer = setInterval(refresh, 1000);
    if (!busy && timer) { clearInterval(timer); timer = 0; }
  }

  function poll() { refresh(); if (!timer) timer = setInterval(refresh, 1000); }

  allBtn.onclick = () => { showAll = !showAll; keep(ALL_KEY, showAll ? "1" : "0"); refresh(); };
  strip.append(allBtn);
  labelAll();

  document.addEventListener("basalt:models-sub", e => {
    if (!BUILT_IN.includes(e.detail)) return;
    current = "";
    for (const b of buttons.values()) b.setAttribute("aria-selected", "false");
    for (const p of panels.values()) p.hidden = true;
  });
  document.addEventListener("tadamoo:lang", () => { if (data) draw(); else labelAll(); });

  const saved = load(SUB_KEY, "qwen");
  if (saved.startsWith("engine:")) current = saved.slice(7);
  refresh();
})();
