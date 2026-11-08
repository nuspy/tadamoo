"use strict";
/* Loader: the engine settings by hand or from a saved load profile, with the memory they need next to what this PC
   has free now and the speed they should give (both from the server: GET /api/loader, POST /api/loader/plan). Load
   writes the config file and restarts the engine inside the server (POST /api/loader/load). Every value decided when
   the model loads is here, with the load profiles and the default one (★); the Settings view only shows what runs now
   and the profile it matches, and its button opens this view on that profile. */
(() => {
  const FIELDS = [
    { group: "reserve", name: "engine.vram-reserve-mib", kind: "reserve" },
    { group: "context", name: "engine.max-context", kind: "int", min: 256, max: 262144, step: 1024,
      list: [4096, 8192, 16384, 32768, 65536, 131072, 262144] },
    { group: "context", name: "engine.kv", kind: "choice", options: ["fp16", "int8", "k8v4", "q4_0"] },
    { group: "context", name: "engine.kv-resident", kind: "int", min: 0, max: 262144, step: 1024 },
    { group: "context", name: "engine.rope-scaling", kind: "choice", options: ["none", "linear", "yarn"], defText: "d.model" },
    { group: "context", name: "engine.rope-scale", kind: "num", min: 1, max: 1024, step: "any" },
    { group: "context", name: "engine.rope-freq-base", kind: "num", min: 0, step: "any" },
    { group: "context", name: "server.parallel", kind: "int", min: 1, max: 8, step: 1 },
    { group: "context", name: "server.slot-full-context", kind: "bool" },
    { group: "context", name: "server.side-slot", kind: "int", min: 0, max: 262144, step: 1024 },
    { group: "speed", name: "engine.prefill", kind: "text", list: ["auto", "auto:8192", "auto:16384", "auto:32768", "2048", "512"] },
    { group: "speed", name: "mtp", kind: "bool", virtual: true },
    { group: "speed", name: "engine.spec", kind: "int", min: 2, max: 8, step: 1 },
    { group: "speed", name: "engine.relaxed-drafting", kind: "choice", options: ["0.5", "0.3", "0.2"] },
    { group: "speed", name: "engine.prefill-cpu-share", kind: "num", min: 0, max: 1, step: 0.05 },
    { group: "speed", name: "engine.adapt-swaps", kind: "int", min: 0, max: 1048576, step: 1 },
    { group: "memory", name: "engine.expert-cache", kind: "text", list: ["auto"] },
    { group: "memory", name: "engine.resident-experts", kind: "bool" },
    { group: "memory", name: "engine.resident-budget-gib", kind: "num", min: 0.1, max: 65536, step: "any" },
    { group: "memory", name: "engine.mmap-experts", kind: "bool" },
    { group: "memory", name: "engine.vram-elastic", kind: "bool" },
    { group: "memory", name: "engine.keep-vram-commit", kind: "bool", windows: true },
    { group: "memory", name: "engine.ple-io", kind: "choice", options: ["direct", "mmap", "ram"] },
    { group: "memory", name: "engine.pool-workers", kind: "int", min: 1, max: 256, step: 1 },
    { group: "cache", name: "engine.prompt-cache", kind: "int", min: 0, max: 1024, step: 1 },
    { group: "cache", name: "engine.prompt-cache-every", kind: "int", min: 0, max: 2147483639, step: 1024 },
    { group: "cache", name: "engine.prompt-cache-root", kind: "int", min: 0, max: 2147483639, step: 256 },
    { group: "cache", name: "engine.conversation-cache-mib", kind: "int", min: 0, max: 16777216, step: 256 },
    { group: "cache", name: "engine.conversation-cache-slots", kind: "int", min: 0, max: 4096, step: 1 },
    { group: "cache", name: "engine.conversation-cache-min-free-mib", kind: "int", min: 0, max: 16777216, step: 256 },
    { group: "server", name: "server.port", kind: "int", min: 1, max: 65535, step: 1 },
    { group: "server", name: "server.vision", kind: "choice", options: ["off", "dynamic", "cpu", "0"] },
    { group: "server", name: "server.vision-projector", kind: "choice", options: ["embedded"] },   // the options come with each plan
    { group: "server", name: "server.vision-hold", kind: "choice", options: ["keep", "release"] },
    { group: "server", name: "server.grammar", kind: "bool" },
    { group: "server", name: "server.reasoning-budget-tokens", kind: "int", min: 0, max: 2147483639, step: 256 },
    { group: "server", name: "server.answer-room", kind: "bool" },
    { group: "server", name: "server.thinking-loop-tokens", kind: "int", min: 0, max: 2147483639, step: 64 },
    { group: "server", name: "server.max-answer-tokens", kind: "int", min: 0, max: 2147483639, step: 256 },
    { group: "server", name: "server.measure-length", kind: "bool" },
    { group: "server", name: "server.passage-loop-tokens", kind: "int", min: 0, max: 2147483639, step: 1 },
    { group: "server", name: "server.passage-loop-times", kind: "int", min: 0, max: 2147483639, step: 1 },
    { group: "server", name: "server.passage-loop-window", kind: "int", min: 0, max: 2147483639, step: 100 },
    { group: "advanced", name: "engine.expert-profile-save", kind: "path" },
    { group: "advanced", name: "engine.expert-profile-save-every", kind: "num", min: 0, max: 525600, step: "any" },
  ];
  const isNum = f => f.kind === "int" || f.kind === "num";
  const fid = name => "lf-" + name.replace(/[^a-z0-9]+/gi, "-");
  const GiBn = 2 ** 30;
  const gib = n => nfmt(n / GiBn, 1);
  let INFO = null, PROF = null, seq = 0, timer = 0, poll = 0;
  const RX = "engine.relaxed-drafting", SIDE = "server.side-slot";
  const fallback = { "engine.relaxed-drafting": "off", "server.vision": "off", "server.vision-projector": "auto", "server.vision-hold": "keep", "engine.kv": "fp16", "engine.ple-io": "direct" };

  /* ---- the form ---- */
  function build() {
    const box = $("#ld-fields");
    const groups = [...new Set(FIELDS.map(f => f.group))];
    box.replaceChildren(...groups.map(g => {
      const adv = g === "advanced";                     // rarely changed: folded away until opened
      const head = adv ? "summary" : "legend";
      const fs = document.createElement(adv ? "details" : "fieldset");
      fs.className = "ldgroup" + (adv ? " ldadv" : "");
      fs.innerHTML = `<${head}>${esc(T("ld.g." + g))}</${head}><div class="fields"></div>`;
      const grid = fs.querySelector(".fields");
      for (const f of FIELDS.filter(x => x.group === g)) {
        const id = fid(f.name), help = `${id}-h`, key = f.name === "mtp" ? "mtp" : f.name;
        const d = document.createElement("div");
        d.className = "field" + (f.kind === "bool" ? " check" : "");
        d.dataset.name = f.name;
        let input;
        if (f.kind === "reserve") {
          d.className = "field rsv";
          d.innerHTML = reserveHtml("lf", id, help);
          grid.append(d);
          continue;
        }
        if (f.kind === "bool") {
          input = `<input id="${id}" type="checkbox" aria-describedby="${help}">`;
          d.innerHTML = `<label for="${id}">${input}<span>${esc(T("f." + key))}</span></label>`;
        } else if (f.kind === "choice") {
          const opts = f.options.map(o => `<option value="${o}">${esc(optionText(f.name, o))}</option>`).join("");
          d.innerHTML = `<label for="${id}">${esc(T("f." + key))}</label><select id="${id}" aria-describedby="${help}">` +
            `<option value="" data-default></option>${opts}</select>`;
        } else {
          const list = f.list ? `<datalist id="${id}-l">${f.list.map(o => `<option value="${o}"></option>`).join("")}</datalist>` : "";
          const num = isNum(f);
          const range = `${f.min != null ? ` min="${f.min}"` : ""}${f.max != null ? ` max="${f.max}"` : ""} step="${f.step}"`;
          d.innerHTML = `<label for="${id}">${esc(T("f." + key))}</label>` +
            `<input id="${id}" type="${num ? "number" : "text"}" ${num ? `inputmode="${f.kind === "num" ? "decimal" : "numeric"}"${range}` : 'autocapitalize="off" spellcheck="false"'}` +
            `${f.list ? ` list="${id}-l"` : ""} aria-describedby="${help}">${list}`;
        }
        d.insertAdjacentHTML("beforeend", `<p class="help" id="${help}">${esc(T("h." + key))}</p>`);
        if (f.name === RX) d.insertAdjacentHTML("beforeend", '<div class="rx-note" id="rx-note" role="status" aria-live="polite"></div>');
        if (f.name === SIDE) d.insertAdjacentHTML("beforeend", '<div class="rx-note" id="side-note" role="status" aria-live="polite" hidden></div>');
        d.insertAdjacentHTML("beforeend", `<p class="help unsup" hidden>${esc(T(f.windows ? "ld.windows" : "ld.unsupported"))}</p>`);
        grid.append(d);
      }
      if (g === "server") fs.querySelector(".fields").append(secretRow());
      return fs;
    }));
  }
  /* ---- the secret of the changes: the switch and the chosen text are saved on their own (POST /api/control-secret), never with the load values ---- */
  function secretRow() {
    const d = document.createElement("div");
    d.className = "cs";
    d.innerHTML = `<div class="field check"><label for="ld-cs-on"><input id="ld-cs-on" type="checkbox" aria-describedby="ld-cs-h"><span>${esc(T("cs.on"))}</span></label></div>` +
      `<div class="pfrow wrap"><input id="ld-cs-value" type="password" autocomplete="new-password" autocapitalize="off" spellcheck="false" maxlength="200" aria-label="${esc(T("cs.value"))}" placeholder="${esc(T("cs.value"))}">` +
      `<button class="btn primary" id="ld-cs-save" type="button">${esc(T("cs.save"))}</button><button class="btn ghost" id="ld-cs-clear" type="button" hidden>${esc(T("cs.clear"))}</button></div>` +
      `<p class="help" id="ld-cs-h">${esc(T("cs.help"))}</p><p class="pfmsg" id="ld-cs-msg" role="status" aria-live="polite"></p>`;
    const q = id => d.querySelector("#ld-cs-" + id);
    const msg = (text, kind) => { q("msg").textContent = text; q("msg").dataset.kind = kind || ""; };
    const save = async body => {
      const { r, j } = await post("api/control-secret", body);
      if (!r.ok) { msg((j.error && j.error.message) || T("cs.err"), "err"); return; }
      paintSecret(j);
      q("value").value = "";
      msg(T(j.running === j.enabled ? "cs.saved.same" : "cs.saved"), "");
    };
    q("save").onclick = () => { const v = q("value").value; save({ enabled: q("on").checked, ...(v ? { secret: v } : {}) }); };
    q("clear").onclick = () => save({ secret: null });
    return d;
  }
  function paintSecret(state) {
    const on = $("#ld-cs-on");
    if (!on) return;
    on.checked = !!state.enabled;
    $("#ld-cs-value").placeholder = T(state.set ? "cs.kept" : "cs.value");
    $("#ld-cs-clear").hidden = !state.set;
  }
  async function loadSecret() {
    try { paintSecret(await get("api/control-secret")); } catch (e) { /* no config file: the row stays as it is */ }
  }
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const el = name => $("#" + fid(name));
  // what a choice means, next to its name: o.<field>.<value> when the page has a sentence for it
  const optionText = (name, value) => {
    if (name === "server.vision" && /^\d+$/.test(value)) return gpuLabel(value);
    const k = `o.${name}.${value}`, t = T(k);
    return t !== k ? t : value;
  };
  // the GPU an image encoder can sit on: its number and, when the server found it, the card's name
  const gpuName = n => { const g = (((INFO && INFO.limits) || {}).gpus || []).find(x => String(x.index) === String(n)); return g && g.name ? g.name : ""; };
  const gpuLabel = n => gpuName(n) ? T("o.server.vision.gpu", n, gpuName(n)) : `GPU ${n}`;
  const gpuShort = n => gpuName(n) ? `GPU ${n} (${gpuName(n)})` : `GPU ${n}`;

  /* ---- the VRAM left free: a slider and a number in GB (GiB, as Windows shows them), engine.vram-reserve-mib ---- */
  function reserveHtml(prefix, id, help) {
    return `<label for="${id}" class="${prefix === "lf" ? "sr" : ""}">${esc(T("rsv.title"))}</label>
      <div class="rsv-row"><input type="range" id="${prefix}-rsv-range" min="0.25" max="32" step="0.25" aria-label="${esc(T("rsv.title"))}" aria-describedby="${help}">
        <span class="rsv-num"><input id="${id}" type="number" inputmode="decimal" min="0.25" max="64" step="0.25" aria-describedby="${help}"><span aria-hidden="true">GB</span></span></div>
      <p class="help" id="${help}">${esc(T("rsv.help"))}</p>
      <div class="rsv-live" id="${prefix}-rsv-live" aria-live="polite"></div>`;
  }
  function reserveMax() {
    const v = INFO && INFO.reserve && INFO.reserve.vram_total;
    return v ? Math.max(4, Math.min(64, Math.floor(v / GiBn))) : 32;
  }
  function syncReserve(prefix, num, from) {
    const range = $(`#${prefix}-rsv-range`);
    if (!range || !num) return;
    range.max = String(reserveMax());
    if (from === "range") num.value = range.value;
    else range.value = num.value === "" ? String((((INFO && INFO.defaults) || {})["engine.vram-reserve-mib"] || 700) / 1024) : num.value;
  }
  const toMiB = raw => { const n = Number(String(raw).replace(",", ".")); return raw === "" ? null : Number.isFinite(n) ? Math.round(n * 1024) : raw; };
  const toGB = mib => mib == null ? "" : String(+(Number(mib) / 1024).toFixed(2));
  function reserveLive(p, target) {
    const box = $(target);
    if (!box || !p) return;
    const v = p.vram, s = p.speed || {}, c = p.commit;
    const cache = ((v.items || []).find(i => i.key === "experts") || {}).bytes || 0;
    const pct = v.expert_slots / Math.max(1, v.expert_slots_total) * 100;
    const auto = ["auto", 0, "0", null, undefined].includes((p.values || {})["engine.expert-cache"]);
    const procs = ((p.gpu || {}).processes || []).map(x => x.name || String(x.pid));
    const counts = procs.reduce((a, n) => (a[n] = (a[n] || 0) + 1, a), {});
    const list = Object.entries(counts).map(([n, k]) => k > 1 ? `${n} ×${k}` : n).join(", ");
    box.innerHTML = `<ul class="rsv-figs">
        <li><span>${esc(T("rsv.experts"))}</span><b>${gib(cache)} GB</b></li>
        <li><span>${esc(T("rsv.slots"))}</span><b>${esc(T("rsv.slots.v", nfmt(v.expert_slots), nfmt(v.expert_slots_total), nfmt(pct, 0)))}</b></li>
        <li><span>${esc(T("rsv.hit"))}</span><b>${nfmt((s.hit_rate || 0) * 100, 1)}%</b></li>
        <li><span>${esc(T("rsv.speed"))}</span><b>${nfmt(s.decode, 0)} · ${nfmt(s.prefill, 0)} tok/s</b></li>
        <li><span>${esc(T("rsv.commit"))}</span><b>${gib(c.need)} GB</b></li></ul>
      <p class="help">${esc(auto ? T("rsv.trade") : T("rsv.fixed"))}</p>
      ${(p.warnings || []).map(x => `<p class="rsv-warn" role="alert">⚠ ${esc(LANG === "it" ? x.text_it : x.text)}</p>`).join("")}
      ${list ? `<p class="help">${esc(T("rsv.procs", list))}</p>` : ""}`;
  }

  function defaults() {
    const d = (INFO && INFO.defaults) || {};
    FIELDS.forEach(f => {
      const e = el(f.name);
      if (!e || f.virtual) return;
      const v = d[f.name];
      if (f.kind === "choice") e.querySelector("[data-default]").textContent = T("opt.default", v ?? (f.defText ? T(f.defText) : fallback[f.name]) ?? "—");
      else if (f.kind === "reserve") e.placeholder = toGB(v);
      else if (f.kind !== "bool") e.placeholder = v == null ? "—" : String(v);
    });
  }

  function readForm() {
    const out = {};
    for (const f of FIELDS) {
      if (f.virtual) continue;
      const e = el(f.name);
      if (f.kind === "bool") { out[f.name] = e.checked; continue; }
      if (f.kind === "reserve") { out[f.name] = toMiB(e.value.trim()); continue; }
      const raw = e.value.trim();
      if (raw === "") { out[f.name] = null; continue; }
      if (isNum(f)) { const n = Number(raw.replace(",", ".")); out[f.name] = Number.isFinite(n) ? n : raw; continue; }
      out[f.name] = f.kind !== "path" && /^\d+$/.test(raw) ? Number(raw) : raw;
    }
    if (!el("mtp").checked) out["engine.spec"] = 0;
    else if (out["engine.spec"] == null || out["engine.spec"] < 2) out["engine.spec"] = 7;
    return out;
  }
  function writeForm(values) {
    const v = values || {};
    for (const f of FIELDS) {
      if (f.virtual) continue;
      const e = el(f.name), x = v[f.name];
      if (f.kind === "bool") e.checked = x == null ? ((INFO && INFO.defaults) || {})[f.name] === true : x === true;     // absent = the engine's default
      else if (f.kind === "reserve") { e.value = toGB(x); syncReserve("lf", e); }
      else if (f.kind === "choice") {                 // a value the list does not have (written in the file by hand) is added, not lost
        const s = x == null ? "" : x === true ? "0.5" : String(x);
        if (s && !e.querySelector(`option[value="${s}"]`)) e.insertAdjacentHTML("beforeend", `<option value="${esc(s)}">${esc(optionText(f.name, s))}</option>`);
        e.value = s;
      } else e.value = x == null ? "" : String(x);
    }
    const spec = Number(v["engine.spec"] ?? 0);
    el("mtp").checked = spec >= 2;
    if (spec < 2) el("engine.spec").value = "";
    rxResync();
    syncMtp();
  }

  /* ---- relaxed drafting: the settings it needs are set, shown with what they do, and given back when it is switched off ---- */
  const rxKey = () => "basalt.relaxed:" + ((INFO && INFO.config) || "");
  const rxStore = v => { try { if (v) localStorage.setItem(rxKey(), JSON.stringify(v)); else localStorage.removeItem(rxKey()); } catch (e) { /* no storage: nothing is remembered */ } };
  const rxStored = () => { try { return JSON.parse(localStorage.getItem(rxKey()) || "null"); } catch (e) { return null; } };
  const rxRead = name => { const e = el(name); return e.type === "checkbox" ? e.checked : e.value.trim(); };
  const rxWrite = (name, v) => { const e = el(name); if (e.type === "checkbox") e.checked = !!v; else e.value = v == null ? "" : String(v); };
  const rxCurrent = () => Object.fromEntries(TadamooRelaxed.NAMES.map(n => [n, rxRead(n)]));
  let rxUndo = null;
  function rxResync() {                               // the form was filled (a reload, a profile): is what was set still there?
    const stored = el(RX).value ? rxStored() : null;
    rxUndo = stored && TadamooRelaxed.holds(rxCurrent(), stored) ? stored : null;
    if (!rxUndo) rxStore(null);
    rxNote(null);
  }
  function rxChanged() {
    const on = !!el(RX).value;
    let gave = null;
    if (on && !rxUndo) {
      rxUndo = TadamooRelaxed.enter(rxCurrent());
      Object.entries(rxUndo.set).forEach(([n, v]) => rxWrite(n, v));
      rxStore(Object.keys(rxUndo.set).length ? rxUndo : null);
    } else if (!on && rxUndo) {
      const r = TadamooRelaxed.leave(rxCurrent(), rxUndo);
      Object.entries(r.restore).forEach(([n, v]) => rxWrite(n, v));
      gave = { restored: Object.keys(r.restore), kept: r.kept, before: rxUndo.before };
      rxUndo = null;
      rxStore(null);
    }
    rxNote(gave);
  }
  const rxLabel = name => T(name === "mtp" ? "f.mtp" : "f." + name);
  const rxShow = (name, v) => typeof v === "boolean" ? T(v ? "opt.on" : "opt.off") : (v === "" || v == null ? T("rx.unset") : String(v));
  function rxNote(gave) {
    const box = $("#rx-note");
    if (!box) return;
    const parts = [];
    if (el(RX).value) {
      const set = (rxUndo && rxUndo.set) || {};
      parts.push(`<p>${esc(T("rx.on"))}</p>`);
      const rows = Object.keys(set).map(n => `<li>${esc(T("rx.chg", rxLabel(n), rxShow(n, rxUndo.before[n]), rxShow(n, set[n])))} ${esc(T("rx.why." + n))}</li>`);
      if (rows.length) parts.push(`<ul>${rows.join("")}</ul>`);
      parts.push(`<p>${esc(T("rx.fixed"))}</p><p>${esc(T("rx.exact"))}</p>`);
    } else if (gave && (gave.restored.length || gave.kept.length)) {
      if (gave.restored.length) parts.push(`<p>${esc(T("rx.restored", gave.restored.map(n => `${rxLabel(n)}: ${rxShow(n, gave.before[n])}`).join("; ")))}</p>`);
      if (gave.kept.length) parts.push(`<p>${esc(T("rx.kept", gave.kept.map(rxLabel).join(", ")))}</p>`);
    }
    box.innerHTML = parts.join("");
    box.hidden = !parts.length;
  }
  function syncMtp() {
    const on = el("mtp").checked;
    el("engine.spec").disabled = !on;
    const par = Number(el("server.parallel").value || 1);
    el("server.slot-full-context").disabled = par < 2;
    // the projector and its hold matter only when images are read: any mode for the projector, in-engine ("dynamic") for the hold
    const mode = el("server.vision").value || ((INFO && INFO.defaults) || {})["server.vision"] || "off";
    el("server.vision-projector").disabled = mode === "off";
    el("server.vision-hold").disabled = mode !== "dynamic";
    // a setting of another system, or of a newer engine than the binary in use, is shown but cannot be set
    const bad = new Set((INFO && INFO.unsupported) || []), win = !INFO || !INFO.limits || INFO.limits.windows !== false;
    for (const f of FIELDS.filter(x => x.windows || bad.has(x.name))) {
      const off = bad.has(f.name) || (f.windows && !win);
      el(f.name).disabled = off;
      $(`#ld-fields .field[data-name="${f.name}"] .unsup`).hidden = !off;
    }
    // mmap-experts keeps the experts in the file mapping: it wins over the resident copy and its budget
    const mm = el("engine.mmap-experts").checked;
    if (mm) { el("engine.resident-experts").checked = false; el("engine.resident-budget-gib").value = ""; }
    el("engine.resident-experts").disabled = mm || bad.has("engine.resident-experts");
    el("engine.resident-budget-gib").disabled = mm || bad.has("engine.resident-budget-gib");
    // while relaxed drafting is on, the drafter stays on and one conversation at a time is served (what it needs)
    const rx = !!el(RX).value;
    el("mtp").disabled = rx && el("mtp").checked;
    el("server.parallel").disabled = rx && par <= 1;
    // the technical session: relaxed drafting and it exclude each other, and it takes one of the engine's 8 slots
    const side = Number(el(SIDE).value || 0);
    el(SIDE).disabled = rx && !(side > 0);
    el("server.parallel").max = side > 0 ? "7" : "8";
    const note = $("#side-note");
    if (note) { note.hidden = !(side > 0); note.textContent = side > 0 ? T("side.on", nfmt(side)) : ""; }
  }

  /* ---- the prediction: resources, speed, the Load button ---- */
  function bar(kind, need, have, keep, items, notes, share, vgpu) {
    const known = typeof have === "number";
    const ok = !known || need <= have - (keep || 0);
    const top = Math.max(need + (keep || 0), known ? have : need, 1);
    const pct = x => `${Math.min(100, x / top * 100).toFixed(1)}%`;
    const label = i => i.key === "vision" && vgpu ? `${T("it.vision")} · ${gpuShort(vgpu.index)}` : T("it." + (kind === "ram" && i.key === "experts" ? "experts_ram" : i.key));
    const lis = (items || []).filter(i => i.bytes > 0).map(i => `<li><span>${esc(label(i))}</span><b>${gib(i.bytes)} GiB</b></li>`).join("");
    return `<div class="res" data-ok="${ok}">
      <div class="res-h"><b>${esc(T("pr." + kind))}</b><span class="fit">${ok ? "✓ " + esc(T("pr.fits")) : "✕ " + esc(T("pr.nofit"))}</span></div>
      <div class="res-n"><span>${esc(T("pr.need", gib(need)))}</span><span>${known ? esc(T("pr.have", gib(have))) : esc(T("pr.unknown"))}</span></div>
      <div class="resbar" aria-hidden="true"><i style="width:${pct(need)}"></i>${known && keep ? `<u style="left:${pct(have - keep)}"></u>` : ""}${known ? `<s style="left:${pct(have)}"></s>` : ""}</div>
      ${keep ? `<p class="res-note">${esc(T("pr.keep", gib(keep), nfmt((share ?? 0.15) * 100, 0) + "%"))}</p>` : ""}
      ${(notes || []).map(n => `<p class="res-note">${esc(n)}</p>`).join("")}
      ${lis ? `<ul class="res-items">${lis}</ul>` : ""}
    </div>`;
  }
  function when(iso) {
    try { return new Date(iso).toLocaleString(LANG === "it" ? "it-IT" : "en-US", { dateStyle: "medium", timeStyle: "short" }); } catch (e) { return iso; }
  }
  function lengthHtml(can) {
    const L = (INFO && INFO.length) || {};
    const run = INFO && INFO.status && INFO.status.job === "length" && ["waiting", "starting", "calibrating"].includes(INFO.status.phase);
    const body = L.known
      ? `<dl class="kv spd">
          <dt>${esc(T("ln.limit"))}</dt><dd><b>${nfmt(L.limit_tokens)}</b> tokens</dd>
          <dt>${esc(T("ln.words"))}</dt><dd><b>${nfmt(L.reliable_words)}</b></dd>
          <dt>${esc(T("ln.natural"))}</dt><dd><b>${nfmt(L.natural_tokens)}</b> tokens</dd>
          <dt>${esc(T("ln.tries"))}</dt><dd>${(L.rungs || []).map(r => esc(T("ln.try", r.prompt, nfmt(r.tokens), nfmt(r.clean_tokens), nfmt(r.cap), r.clean ? "" : T("ln.looped"))) + " " + (r.checkpoints || []).map(c => esc(T(c.clean ? "ln.cp.ok" : c.reached ? "ln.cp.bad" : "ln.cp.no", nfmt(c.at)))).join(" ")).join("<br>")}</dd>
        </dl>
        <p class="setnote">${esc(T("ln.at", when(L.measured_at), (L.model && L.model.quant) || "?"))}</p>`
      : `<p class="setnote">${esc(T("ln.unknown"))}</p>`;
    return `<section class="sec ldlen" aria-label="${esc(T("ln.title"))}">
        <div class="sh"><b>${esc(T("ln.title"))}</b></div>
        ${body}
        <div class="setactions"><p class="setstatus">${esc(T("ln.help"))}</p>
          <button type="button" class="btn" data-act="length" ${can ? "" : "disabled"}>${esc(T("ln.measure"))}</button>
          <button type="button" class="btn" data-act="length-stop" ${run ? "" : "disabled"}>${esc(T("ln.stop"))}</button></div>
      </section>`;
  }
  function sideHtml(p) {
    const v = p.vram, r = p.ram, c = p.commit, s = p.speed || {}, h = p.engine_holds || {};
    const holds = ((h.ram || h.commit) ? [T("pr.holds", gib((h.commit || 0) + (h.vram || 0)))] : [])
      .concat(p.vision_hold && p.vision_hold.release ? [T("pr.vision.released", gib(p.vision_hold.bytes))] : []);
    const vnotes = [T("pr.reserve", gib(v.reserve)), T("pr.slots", nfmt(v.expert_slots), nfmt(v.expert_slots_total),
      nfmt(v.expert_slots / Math.max(1, v.expert_slots_total) * 100, 0))];
    // what the memory settings did to the figures: the KV split, the resident budget, the caches' formula
    const gb = n => gib(n || 0);
    const noteText = n => T("pn." + n.code, ...({
      kv_stream: [nfmt(n.cells), gb(n.vram), gb(n.host)], kv_fits: [nfmt(n.cells)], kv_k8v4: [], mmap_wins: [], side: [nfmt(n.context), gb(n.kv)], side_kv: [n.kv],
      budget: [gb(n.bytes)], caches: [nfmt(n.checkpoints), nfmt(n.slots), gb(n.parked)],
    }[n.code] || []));
    const pnotes = (p.notes || []).map(noteText);
    const src = s.source || {};
    const srcText = src.kind === "calibration" ? T(src.exact ? "sp.cal.exact" : "sp.cal", when(src.at)) : T("sp.seed");
    const problems = (p.problems || []).map(x => LANG === "it" ? x.text_it : x.text);
    const editable = INFO && INFO.editable, busy = INFO && INFO.status && ["waiting", "stopping", "starting", "calibrating"].includes(INFO.status.phase);
    const why = !editable ? T("ld.ro." + ((INFO && INFO.reason) || "no_config")) : busy ? T("ld.busy") : problems.length ? T("ld.refused", problems.join(" ")) : "";
    const blocked = !editable || busy;                          // not fitting only warns: the load can go ahead anyway
    const label = problems.length ? T("ld.loadanyway") : T("ld.load");
    return `<section class="sec ldres" aria-label="${esc(T("pr.title"))}">
        <div class="sh"><b>${esc(T("pr.title"))}</b></div>
        <div class="pad">
          ${bar("vram", v.need + v.reserve, v.available, 0, v.items, vnotes, undefined, p.vision_gpu)}
          ${bar("ram", r.need, r.available, r.keep, r.items, holds.concat(pnotes), r.keep_share)}
          ${bar("commit", c.need, c.available, c.keep, [], [T("pr.commit.parts", gib(c.ram_part), gib(c.vram_part))], c.keep_share)}
          ${p.relaxed && p.relaxed.p && !p.relaxed.effective ? `<p class="rsv-warn" role="alert">⚠ ${esc(T("rx.noeffect", p.relaxed.why.map(w => T("rx.no." + w)).join(" ")))}</p>` : ""}
        </div>
      </section>
      <section class="sec ldspeed" aria-label="${esc(T("sp.title"))}">
        <div class="sh"><b>${esc(T("sp.title"))}</b></div>
        <dl class="kv spd">
          <dt>${esc(T("sp.decode"))}</dt><dd><b>${nfmt(s.decode, 0)}</b> tok/s</dd>
          <dt>${esc(T("sp.reasoning"))}</dt><dd><b>${nfmt(s.decode_reasoning, 0)}</b> tok/s${s.decode_reasoning_visible ? ` · ${esc(T("sp.visible", nfmt(s.decode_reasoning_visible, 0)))}` : ""}</dd>
          <dt>${esc(T("sp.prefill"))}</dt><dd><b>${nfmt(s.prefill, 0)}</b> tok/s · ${nfmt(s.prefill_chunk)}</dd>
        </dl>
        <p class="setnote">${esc(srcText)} · ${esc(T("sp.hit", nfmt((s.hit_rate || 0) * 100, 1)))}</p>
        <div class="setactions"><p class="setstatus">${esc(T("sp.calibrate.help"))}</p>
          <button type="button" class="btn" data-act="calibrate" ${editable && !busy ? "" : "disabled"}>${esc(T("sp.calibrate"))}</button></div>
      </section>
      ${lengthHtml(editable && !busy)}
      <div class="sec ldgo">
        <p class="ldwhy" role="status" aria-live="polite" ${why ? "" : "hidden"}>${esc(why)}</p>
        <p class="ldstate setstatus" role="status" aria-live="polite"></p>
        <div class="setactions"><button type="button" class="btn primary" data-act="load" data-force="${problems.length ? "1" : ""}" ${blocked ? "disabled" : ""}>${esc(label)}</button></div>
      </div>`;
  }
  /* the projectors this model can read images with, each with the RAM it keeps: the model file's own and the files of the models folders */
  function projectorChoices(p) {
    const e = el("server.vision-projector");
    if (!e || !Array.isArray(p.projectors)) return;
    const keep = e.value, opt = (v, text, extra) => `<option value="${esc(v)}"${extra || ""}>${esc(text)}</option>`;
    const label = o => `${o.value === "embedded" ? T("pj.embedded") : o.name} (${gib(o.bytes)} GiB ${T("pj.inram")}` +
      `${o.fix ? " · " + T("pj.rewrite") : ""}${o.problem ? " · " + T("pj.refused") : ""})`;
    const first = (p.projector && p.projectors.find(o => o.value === p.projector.value)) || null;
    const html = opt("", T("opt.default", first ? T("pj.auto.is", label(first)) : T("pj.auto")), " data-default") +
      p.projectors.map(o => opt(o.value, label(o))).join("") +
      (keep && !p.projectors.some(o => o.value === keep) ? opt(keep, keep) : "");
    if (e.dataset.sig === html) return;                           // unchanged: leave an open list alone
    e.dataset.sig = html;
    e.innerHTML = html;
    e.value = keep;
  }
  /* the places the image encoder can sit: one option per GPU the server found, with the card's name; a saved GPU number
     that was not found stays selectable */
  function visionChoices() {
    const e = el("server.vision");
    if (!e) return;
    const keep = e.value, seen = (((INFO && INFO.limits) || {}).gpus || []).map(g => String(g.index));
    const cards = seen.length ? seen : ["0"];
    if (/^\d+$/.test(keep) && !cards.includes(keep)) cards.push(keep);
    const opt = (v, text, extra) => `<option value="${esc(v)}"${extra || ""}>${esc(text)}</option>`;
    const html = opt("", "", " data-default") + ["off", "dynamic", "cpu"].map(m => opt(m, optionText("server.vision", m))).join("") +
      cards.map(n => opt(n, optionText("server.vision", n))).join("");
    if (e.dataset.sig === html) return;
    e.dataset.sig = html;
    e.innerHTML = html;
    e.value = keep;
  }
  function renderSide(target, p) {
    const box = $(target);
    projectorChoices(p);
    box.innerHTML = sideHtml(p);
    sticky(p);
    reserveLive(p, "#lf-rsv-live");
    showStatus();
  }
  const modelNow = () => (window.TadamooModels && TadamooModels.selected()) || null;
  // on a phone the prediction is far below the fields: a summary bar stays at the bottom while editing
  function sticky(p) {
    const ok = k => !(p.problems || []).some(x => x.code === k || (k === "ram" && x.code === "ple_ram"));
    const part = (k, label) => `<span data-ok="${ok(k)}">${ok(k) ? "✓" : "✕"} ${esc(label)}</span>`;
    const go = $("#ld-side [data-act=load]");
    $("#ld-sticky").innerHTML = `<span class="stk-parts">${part("vram", T("pr.vram"))}${part("ram", T("pr.ram"))}${part("commit", "Commit")}` +
      `<span>~${nfmt((p.speed || {}).decode, 0)} tok/s</span></span>` +
      `<a class="btn ${go && !go.disabled && !(p.problems || []).length ? "primary" : "ghost"}" href="#ld-side">${esc(go && !go.disabled ? ((p.problems || []).length ? T("ld.loadanyway") : T("ld.load")) : T("pr.nofit"))}</a>`;
  }

  async function plan(values, model) {
    const r = await fetch("api/loader/plan", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ values, model: model === undefined ? modelNow() : model }) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }
  function replan() {
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const n = ++seq;
      try {
        const p = await plan(readForm());
        if (n === seq) { renderSide("#ld-side", p, "loader"); markField(""); }
      } catch (e) {
        if (n !== seq) return;
        const name = FIELDS.find(f => e.message.startsWith(f.name + ":"));
        markField(name ? name.name : "", e.message);
      }
    }, 200);
  }
  function markField(name, msg) {
    $$("#ld-fields .ferr").forEach(x => x.remove());
    $$("#ld-fields [aria-invalid]").forEach(x => x.removeAttribute("aria-invalid"));
    if (!msg) return;
    const box = name ? $(`#ld-fields .field[data-name="${name}"]`) : null;
    const p = document.createElement("p"); p.className = "ferr"; p.textContent = msg; p.setAttribute("role", "alert");
    if (box) { box.append(p); el(name).setAttribute("aria-invalid", "true"); } else $("#ld-fields").prepend(p);
  }

  /* ---- load, calibrate and their progress ---- */
  async function post(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) });
    const j = await r.json().catch(() => ({}));
    if (r.status === 401) askKey();
    return { r, j };
  }
  async function load(values, target, mode, model, force) {
    const m = model || modelNow();
    const body = m ? { values, model: m } : { values };
    if (force) body.force = true;
    const { r, j } = await post("api/loader/load", body);
    if (r.status === 422 && j.plan) { renderSide(target, j.plan, mode); return; }
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    if ((j.notes || []).includes("model_restart")) { say(T("ld.model.relaunch", TadamooModels.name(j.model)), "ok"); waitRestart(); return; }
    if ((j.notes || []).includes("model_on_restart")) { say(T("ld.model.restart", TadamooModels.name(j.model)), "ok"); return; }
    if ((j.notes || []).includes("port_on_restart")) say(T("ld.port.note"));
    INFO.status = { phase: "waiting" };
    watch();
  }
  // the server ends and its owner starts it again with the new model: reload the page once it answers again
  function waitRestart() {
    let down = false;
    const t0 = Date.now();
    const tick = async () => {
      let up = false;
      try { up = (await fetch("health", { cache: "no-store" })).ok; } catch (e) { up = false; }
      if (!up) down = true;
      if (up && (down || Date.now() - t0 > 15000)) { location.hash = "#loader"; location.reload(); return; }
      setTimeout(tick, 1000);
    };
    setTimeout(tick, 800);
  }
  async function calibrate() {
    const { r, j } = await post("api/loader/calibrate", {});
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    INFO.status = { phase: "calibrating" };
    watch();
  }
  async function measureLength(action) {
    const { r, j } = await post("api/loader/length", { action });
    if (!r.ok) { say(T("ld.phase.failed", (j.error && j.error.message) || `HTTP ${r.status}`), "err"); return; }
    if (action === "measure") { INFO.status = { phase: "calibrating", job: "length", step: "length" }; watch(); }
  }
  function say(text, kind) {
    $$(".ldstate").forEach(e => { e.textContent = text; e.dataset.kind = kind || ""; });
  }
  function showStatus() {
    const st = (INFO && INFO.status) || {};
    const ph = st.phase || "idle";
    if (ph === "idle") return;
    const text = ph === "calibrating" ? T("ld.phase.calibrating", T("ld.step." + (st.step || "decode"))) + (st.job === "length" && st.message ? " · " + st.message : "")
      : ph === "failed" ? T("ld.phase.failed", st.error || "") : T("ld.phase." + ph);
    say(text, ph === "failed" ? "err" : ph === "ready" ? "ok" : "");
    $$("[data-act=load], [data-act=calibrate], [data-act=length]").forEach(b => { b.disabled = b.disabled || ["waiting", "stopping", "starting", "calibrating"].includes(ph); });
  }
  function watch() {
    clearTimeout(poll);
    const tick = async () => {
      try {
        const st = await get("api/loader/status");
        INFO.status = st;
        showStatus();
        if (["waiting", "stopping", "starting", "calibrating"].includes(st.phase)) { poll = setTimeout(tick, 600); return; }
        await refresh();
        showStatus();
        document.dispatchEvent(new CustomEvent("basalt:loader"));          // a load ended: the technical session may have come or gone (optimize.js asks again)
      } catch (e) { poll = setTimeout(tick, 1500); }
    };
    showStatus();
    poll = setTimeout(tick, 300);
  }

  /* ---- profiles ---- */
  function profileLabel(p) {
    const s = p.summary || {};
    const parts = [s.text];
    if (s.model) parts.unshift(s.model + (s.model_missing ? " (?)" : ""));
    if (s.vram_gb != null) parts.push(`VRAM ~${nfmt(s.vram_gb, 1)} GB`);
    if (s.tok_s != null) parts.push(`~${nfmt(s.tok_s)} tok/s`);
    if (s.port && s.port !== 7232) parts.push(`:${s.port}`);
    const tags = [p.builtin ? T("ld.profile.builtin") : "", p.default ? T("ld.profile.isdefault") : ""].filter(Boolean);
    return `${p.name}${tags.length ? ` (${tags.join(", ")})` : ""} — ${parts.join(" · ")}`;
  }
  function headlessCmd(name) {
    return `powershell -ExecutionPolicy Bypass -File Avvia-TaDaMoo.ps1 -server --config "${name.replace(/"/g, "")}"`;
  }
  function profileBar(target) {
    const box = $(target);
    if (!PROF) { box.replaceChildren(); return; }
    const id = target.slice(1) + "-sel", sel = box.querySelector("select");
    const keep = sel ? sel.value : PROF.default;
    const editable = INFO && INFO.editable;
    box.innerHTML = `
      <div class="pfrow"><label for="${id}">${esc(T("ld.profile"))}</label>
        <select id="${id}">${PROF.load.map(p =>
          `<option value="${esc(p.name)}">${esc(profileLabel(p))}</option>`).join("")}</select>
        <button type="button" class="btn" data-pf="use">${esc(T("ld.profile.apply"))}</button></div>
      <div class="pfrow wrap">
        <input type="text" maxlength="60" autocomplete="off" aria-label="${esc(T("ld.profile.name"))}" placeholder="${esc(T("ld.profile.name"))}" data-pf-name>
        <button type="button" class="btn" data-pf="save" ${editable ? "" : "disabled"}>${esc(T("ld.profile.save"))}</button>
        <button type="button" class="btn ghost" data-pf="rename" ${editable ? "" : "disabled"}>${esc(T("ld.profile.rename"))}</button>
        <button type="button" class="btn ghost" data-pf="delete" ${editable ? "" : "disabled"}>${esc(T("ld.profile.delete"))}</button>
        <button type="button" class="btn ghost" data-pf="default" ${editable ? "" : "disabled"}>${esc(T("ld.profile.default"))}</button>
        <a class="btn ghost" href="api/profiles/export" download="basalt-load-profiles.json">${esc(T("ld.profile.export"))}</a>
        <label class="btn ghost filebtn">${esc(T("ld.profile.import"))}<input type="file" accept="application/json,.json" data-pf="import" ${editable ? "" : "disabled"}></label>
        <button type="button" class="btn" data-pf="safe">${esc(T("ld.safe"))}</button>
        <button type="button" class="btn" data-pf="machine" ${PROF.load.some(p => p.name === PROF.machine) ? "" : "disabled"}>${esc(T("ld.machine"))}</button>
        <button type="button" class="btn primary" data-pf="wizard">${esc(T("ld.wizard"))}</button></div>
      <div class="pfrow wrap headless"><span>${esc(T("ld.headless"))}:</span><code data-pf-cmd></code>
        <button type="button" class="btn ghost" data-pf="copy">${esc(T("ld.headless.copy"))}</button></div>
      <p class="setnote">${esc(T("ld.headless.help"))}</p>
      <p class="setstatus pfmsg" role="status" aria-live="polite"></p>`;
    const s = box.querySelector("select");
    s.value = [...s.options].some(o => o.value === keep) ? keep : s.options[0].value;
    const cmd = () => { box.querySelector("[data-pf-cmd]").textContent = headlessCmd(s.value || PROF.default); };
    cmd();
    s.onchange = cmd;
  }
  function pfSay(box, text, kind) { const m = box.querySelector(".pfmsg"); if (m) { m.textContent = text; m.dataset.kind = kind || ""; } }
  async function profileAction(box, act) {
    const s = box.querySelector("select"), name = s.value, input = box.querySelector("[data-pf-name]");
    const typed = input ? input.value.trim() : "";
    const prof = PROF.load.find(p => p.name === name);
    const done = async (key, arg) => { await refreshProfiles(); pfSay(box, T(key, arg), "ok"); };
    const fail = j => pfSay(box, (j.error && j.error.message) || "error", "err");
    if (act === "use" && prof) {
      writeForm(prof.values); syncMtp();
      if (prof.model) TadamooModels.select(prof.model);
      replan(); pfSay(box, T("ld.profile.used", name), "ok"); return;
    }
    if (act === "safe") {
      writeForm({ ...INFO.safe, "server.port": Number(el("server.port").value) || INFO.safe["server.port"] || 7232 });
      syncMtp(); replan(); $("#ld-safe-why").open = true; pfSay(box, T("ld.safe.done"), "ok"); return;
    }
    if (act === "machine") { applyMachine(box); return; }
    if (act === "wizard") { if (window.TadamooWizard) TadamooWizard.open(); return; }
    if (act === "copy") {
      const t = box.querySelector("[data-pf-cmd]").textContent;
      if (navigator.clipboard) navigator.clipboard.writeText(t).then(() => pfSay(box, T("ld.headless.copied"), "ok"), () => {});
      return;
    }
    let body = null;
    if (act === "save") { if (!typed) { pfSay(box, T("cs.preset.noname"), "err"); input.focus(); return; } body = { action: "save", kind: "load", name: typed, values: readForm(), model: modelNow() }; }
    if (act === "rename") { if (!typed) { pfSay(box, T("cs.preset.noname"), "err"); input.focus(); return; } body = { action: "rename", kind: "load", name, new_name: typed }; }
    if (act === "delete") body = { action: "delete", kind: "load", name };
    if (act === "default") body = { action: "default", kind: "load", name };
    if (!body) return;
    const { r, j } = await post("api/profiles", body);
    if (!r.ok) { fail(j); return; }
    if (act === "save") { await done("ld.profile.saved", typed); box.querySelector("select").value = typed; }
    if (act === "rename") { await done("ld.profile.renamed", typed); box.querySelector("select").value = typed; }
    if (act === "delete") await done("ld.profile.deleted", name);
    if (act === "default") await done("ld.profile.defaulted", name);
  }
  // "Ripristina Machine Default": the wizard's profile in the form, with its model
  function applyMachine(box) {
    const md = PROF && PROF.load.find(p => p.name === PROF.machine);
    if (!md) { pfSay(box, T("ld.machine.none"), "err"); return; }
    writeForm(md.values); syncMtp();
    if (md.model && window.TadamooModels) TadamooModels.select(md.model);
    const sel = box.querySelector("select"); if (sel) sel.value = md.name;
    formTouched = true;
    replan(); pfSay(box, T("ld.machine.done"), "ok");
  }
  document.addEventListener("basalt:machine", async e => {
    await refreshProfiles();
    if (e.detail && e.detail.apply) { view("loader"); applyMachine($("#ld-prof")); }
  });
  async function importProfiles(box, file) {
    try {
      const data = JSON.parse(await file.text());
      const { r, j } = await post("api/profiles", { action: "import", data });
      if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
      await refreshProfiles(); pfSay(box, T("ld.profile.imported"), "ok");
    } catch (e) { pfSay(box, e.message, "err"); }
  }
  {
    const box = $("#ld-prof");
    box.addEventListener("click", e => { const b = e.target.closest("[data-pf]"); if (b && b.tagName === "BUTTON") profileAction(box, b.dataset.pf); });
    box.addEventListener("change", e => { if (e.target.dataset.pf === "import" && e.target.files[0]) { importProfiles(box, e.target.files[0]); e.target.value = ""; } });
  }

  /* ---- the Settings view: what runs now and the load profile it matches; "Open the Loader" opens this view on
     that profile (the form already holds the config file's values, which are the running ones) ---- */
  const samePath = (a, b) => !!a && !!b && a.replace(/\\/g, "/").toLowerCase() === b.replace(/\\/g, "/").toLowerCase();
  function eff(f, v) {
    const x = v ?? ((INFO && INFO.defaults) || {})[f.name] ?? fallback[f.name] ?? (f.kind === "bool" ? false : null);
    return x == null ? "" : String(x);
  }
  function matchProfile(values) {
    if (!PROF || !values) return null;
    const fields = FIELDS.filter(f => !f.virtual);
    const list = [...PROF.load].sort((a, b) => (b.name === PROF.default) - (a.name === PROF.default));
    return list.find(p => {
      const pv = p.values || {};
      if (p.model && INFO.config_model && !samePath(p.model, INFO.config_model)) return false;
      // a profile without a port keeps the file's (the Loader's Use leaves the field as it is)
      return fields.every(f => (f.name === "server.port" && pv[f.name] == null) || eff(f, pv[f.name]) === eff(f, values[f.name]));
    }) || null;
  }
  const ctxText = n => { const v = Number(n); return Number.isFinite(v) && v >= 1024 && v % 1024 === 0 ? `${nfmt(v / 1024)}K` : nfmt(v); };
  function renderSettings() {
    const run = $("#st-run");
    if (!run || !INFO) return;
    const live = !INFO.standby && INFO.running && Object.keys(INFO.running).length;
    const v = live ? INFO.running : (INFO.values || {});
    const val = name => v[name] ?? ((INFO.defaults || {})[name]) ?? fallback[name];
    const spec = Number(val("engine.spec") || 0), par = Number(val("server.parallel") || 1);
    const parts = [(INFO.model && INFO.model.name) || "", ctxText(val("engine.max-context")), val("engine.kv"),
      spec >= 2 ? `spec ${spec}` : T("st.load.nomtp"), par > 1 ? T("st.load.slots", par) : "",
      T("st.load.free", toGB(val("engine.vram-reserve-mib")))].filter(Boolean);
    run.textContent = live ? T("st.load.running", parts.join(" · ")) : `${T("st.load.none")} ${T("st.load.next", parts.join(" · "))}`;
    const p = matchProfile(INFO.values);
    const prof = p ? T("st.load.profile", p.name + (p.name === PROF.default ? ` (${T("ld.profile.isdefault")})` : "")) : T("st.load.custom");
    $("#st-run-file").textContent = `${prof} · ${INFO.config ? T("st.load.file", INFO.config) : T("ld.ro.no_config")}`;
  }
  function openLoader() {
    const p = matchProfile(INFO && INFO.values);
    view("loader");
    const sel = $("#ld-prof select");
    if (sel && p) { sel.value = p.name; sel.dispatchEvent(new Event("change")); }
    (sel || $("#tab-loader")).focus();
  }
  $("#st-open-loader").addEventListener("click", openLoader);

  /* ---- reading ---- */
  async function refreshProfiles() {
    const m = modelNow();
    try { PROF = await get("api/profiles" + (m ? "?model=" + encodeURIComponent(m) : "")); } catch (e) { PROF = null; }
    profileBar("#ld-prof");
    renderSettings();
  }
  async function refresh() {
    try {
      INFO = await get("api/loader");
    } catch (e) {
      if (e.locked) askKey();
      return;
    }
    $("#ld-model").textContent = (INFO.model && INFO.model.name) || "";
    const gw = INFO.gpu_warning && (INFO.gpu_warning[LANG] || INFO.gpu_warning.en);
    $("#ld-gpu").hidden = !gw; $("#ld-gpu").textContent = gw || "";
    $("#ld-standby").hidden = !INFO.standby;
    if (window.TadamooModels) await TadamooModels.refresh();
    visionChoices();
    defaults();
    const reason = INFO.editable ? "" : T("ld.ro." + (INFO.reason || "no_config"));
    $("#ld-reason").hidden = !reason; $("#ld-reason").textContent = reason;
    $("#ld-cli").hidden = !(INFO.from_command_line || []).length;
    $("#ld-cli").textContent = T("ld.cli", (INFO.from_command_line || []).join(", "));
    if (!formTouched) writeForm(INFO.values);
    loadSecret();
    syncReserve("lf", el("engine.vram-reserve-mib"));
    $("#ld-safe-list").innerHTML = Object.entries(INFO.safe || {}).map(([k, v]) => {
      const why = (INFO.safe_why || {})[k];
      return `<dt>${esc(T("f." + k))}: <code>${esc(String(v))}</code></dt><dd>${esc(why ? why[LANG] || why.en : "")}</dd>`;
    }).join("");
    const m = modelNow();
    if (m && !(INFO.plan && INFO.plan.model && INFO.plan.model.path && INFO.plan.model.path.replace(/\\/g, "/").toLowerCase() === m.toLowerCase())) replan();
    else renderSide("#ld-side", INFO.plan, "loader");
    await refreshProfiles();
    if (["waiting", "stopping", "starting", "calibrating"].includes((INFO.status || {}).phase)) watch();
    if (viewOn === "loader") applyStart();
  }
  let formTouched = false;

  /* the first opening: ?profile=, ?model=, ?reserve= (GB) from the desktop app's command line, and ?wizard=1 when this
     PC has no Machine Default yet; a server without a loaded model starts from the default profile, not the file */
  const START = new URLSearchParams(location.search);
  let startDone = false;
  function applyStart() {
    if (startDone || !INFO) return;
    startDone = true;
    const prof = START.get("profile"), model = START.get("model"), reserve = START.get("reserve");
    if (model && window.TadamooModels) TadamooModels.select(model);
    const name = prof || (INFO.standby && PROF ? PROF.default : null);
    const p = name && PROF ? PROF.load.find(x => x.name === name) : null;
    if (p) {
      writeForm(p.values);
      if (p.model && !model && window.TadamooModels) TadamooModels.select(p.model);
      const sel = $("#ld-prof select"); if (sel) sel.value = p.name;
      formTouched = true;
    }
    if (reserve !== null && reserve !== "" && Number.isFinite(Number(reserve))) {
      el("engine.vram-reserve-mib").value = String(Number(reserve)); syncReserve("lf", el("engine.vram-reserve-mib")); formTouched = true;
    }
    if (p || model || reserve) replan();
    if (START.get("wizard") === "1" && window.TadamooWizard) TadamooWizard.offer();
    if (START.get("wizard") === "resume" && window.TadamooWizard) TadamooWizard.resume();
    if ([...START.keys()].length) history.replaceState(null, "", location.pathname + location.hash);
  }

  build();
  const formInput = e => {
    formTouched = true;
    if (e.target.id === "lf-rsv-range") syncReserve("lf", el("engine.vram-reserve-mib"), "range");
    else if (e.target === el("engine.vram-reserve-mib")) syncReserve("lf", e.target);
    if (e.target === el(RX)) rxChanged();
    syncMtp(); replan();
  };
  $("#ld-form").addEventListener("input", formInput);
  $("#ld-form").addEventListener("change", formInput);
  if (window.TadamooModels) TadamooModels.onChange(() => {
    if (viewOn === "loader") { replan(); refreshProfiles(); }
  });
  $("#ld-form").onsubmit = e => e.preventDefault();
  document.addEventListener("click", e => {
    const b = e.target.closest("[data-act]");
    if (!b || b.disabled) return;
    if (b.dataset.act === "load") load(readForm(), "#ld-side", "loader", undefined, b.dataset.force === "1");
    if (b.dataset.act === "calibrate") calibrate();
    if (b.dataset.act === "length") measureLength("measure");
    if (b.dataset.act === "length-stop") measureLength("stop");
  });
  document.addEventListener("tadamoo:view", e => { if (e.detail === "loader" || e.detail === "settings") refresh(); });
  document.addEventListener("tadamoo:key", () => { if (viewOn === "loader" || viewOn === "settings") refresh(); });
  applyT($("#view-loader"));
  $("#ld-models").setAttribute("aria-label", T("ld.model"));
  if (viewOn === "loader" || viewOn === "settings") refresh();
})();
