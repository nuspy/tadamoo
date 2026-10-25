"use strict";
/* Chat settings: sampling, reasoning, output length, stop strings, system prompt, structured output, the context policy
   and named presets. The current settings stay in this client (localStorage when it allows it): the app and each
   browser keep their own. Presets are kept by the server (GET/POST /api/profiles, shared with the desktop app) and in
   this browser when the server cannot. A request carries only the fields that are set, so the model file's defaults
   apply to the rest.
   On opening, a client takes the preset it used last when it still exists; otherwise, and when it used the default,
   it follows the current default ("Machine Default" from the configuration wizard, else the model's own defaults).
   Settings changed by hand after that are kept as they are. */
const chatSettings = (() => {
  const STORE = "basalt.chatset", PRESETS = "basalt.presets", LAST = "basalt.chatset.last";
  const MODEL = "@model";                     // LAST.name of the model's own defaults chosen by hand
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } };
  // the request fields the engine samples with (serve/engine/process.py sampling_keys)
  const SAMPLING = [
    { key: "temperature", min: 0, max: 2, step: 0.05, kind: "float", help: true },
    { key: "top_p", min: 0.01, max: 1, step: 0.01, kind: "float" },
    { key: "top_k", min: 1, max: 64, step: 1, kind: "int", help: true },
    { key: "min_p", min: 0, max: 1, step: 0.01, kind: "float" },
    { key: "repetition_penalty", min: 0.01, max: 3, step: 0.01, kind: "float", help: true },
    { key: "presence_penalty", min: 0, max: 2, step: 0.05, kind: "float" },
    { key: "frequency_penalty", min: 0, max: 2, step: 0.05, kind: "float" },
    { key: "penalty_last_n", min: 1, max: 8192, step: 1, kind: "int", help: true },
    { key: "seed", min: 1, max: 2147483647, step: 1, kind: "int", help: true },
    { key: "end_turn_bias", min: -20, max: 0, step: 0.1, kind: "float", help: true },
  ];
  const OTHER = [
    { key: "reasoning_budget_tokens", id: "cs-budget", min: 0, kind: "int", label: "cs.budget" },
    { key: "max_tokens", id: "cs-max_tokens", min: 1, kind: "int", label: "cs.max_tokens" },
    { key: "retries", id: "cs-retries", min: 0, max: 5, kind: "int", label: "cs.retries" },
  ];
  // what the engine does with a key that neither the request nor the model file sets (the GEN line's defaults)
  const ENGINE_SAMPLING = { temperature: 0, top_p: 1, top_k: 20, min_p: 0, repetition_penalty: 1,
    presence_penalty: 0, frequency_penalty: 0, penalty_last_n: 64, end_turn_bias: 0 };
  let modelDefaults = {}, serverPresets = null, inferenceDefault = null, started = false;
  // the preset this client used last: a name, "" (follow the default) or MODEL; edited: changed by hand since
  let last = load(LAST, null);
  if (!last || typeof last !== "object" || typeof last.name !== "string") {
    // the first opening with this version: settings a user typed before are kept, as changed by hand
    const before = load(STORE, {}) || {}, plain = { policy: "stop", effort: "", format: "" };
    last = { name: "", edited: Object.entries(before).some(([k, v]) => v !== "" && v != null && plain[k] !== v) };
    keep(LAST, last);
  }

  const grid = $("#cs-sampling");
  for (const f of SAMPLING) {
    const d = document.createElement("div");
    d.className = "csnum";
    d.innerHTML = `<label for="cs-${f.key}" data-t="cs.${f.key}"></label>` +
      `<input id="cs-${f.key}" type="number" inputmode="${f.kind === "int" ? "numeric" : "decimal"}" min="${f.min}" max="${f.max}" step="${f.step}"` +
      (f.help ? ` aria-describedby="cs-${f.key}-h">` + `<p class="help" id="cs-${f.key}-h" data-t="cs.${f.key}.help"></p>` : ">");
    grid.append(d);
  }
  applyT($("#chatset"));
  applyT($(".chathead"));

  const fieldsOf = () => [...SAMPLING.map(f => ({ ...f, id: `cs-${f.key}`, label: `cs.${f.key}` })), ...OTHER];
  function read() {
    const s = { system: $("#cs-system").value, stop: $("#cs-stop").value,
      policy: (document.querySelector('input[name="cs-policy"]:checked') || {}).value || "stop",
      threshold: $("#cs-threshold").value, effort: $("#effort").value,
      format: $("#cs-format").value, schema: $("#cs-schema").value, schema_name: $("#cs-schema_name").value,
      context_patterns: window.ctxPatterns ? window.ctxPatterns.read() : {} };
    for (const f of fieldsOf()) s[f.key] = $("#" + f.id).value.trim();
    return s;
  }
  function write(s) {
    s = s || {};
    $("#cs-system").value = s.system || "";
    $("#cs-stop").value = s.stop || "";
    const p = document.querySelector(`input[name="cs-policy"][value="${s.policy || "stop"}"]`);
    if (p) p.checked = true;
    $("#cs-threshold").value = s.threshold || "";
    $("#cs-format").value = s.format || "";
    $("#cs-schema").value = s.schema || "";
    $("#cs-schema_name").value = s.schema_name || "";
    if (window.ctxPatterns) window.ctxPatterns.write(s.context_patterns);
    if (s.effort != null) { $("#effort").value = s.effort; $("#effort").dispatchEvent(new Event("change")); }
    for (const f of fieldsOf()) $("#" + f.id).value = s[f.key] ?? "";
    show();
  }
  const save = () => keep(STORE, read());
  function say(text, kind) { const e = $("#cs-status"); e.textContent = text; e.dataset.kind = kind || ""; }

  /* structured output: the schema is checked as the server checks it (an object schema at the root) */
  function schemaProblem() {
    const raw = $("#cs-schema").value.trim();
    if (!raw) return T("cs.schema.root");
    let s;
    try { s = JSON.parse(raw); } catch (e) { return e.message; }
    if (!s || typeof s !== "object" || Array.isArray(s) || s.type !== "object") return T("cs.schema.root");
    return "";
  }
  function show() {
    const s = read();
    $("#cs-threshold-box").hidden = s.policy !== "compact";
    const json = s.format === "json_object" || s.format === "json_schema";
    $("#cs-schema-box").hidden = s.format !== "json_schema";
    $("#cs-retries-box").hidden = !json;
    $("#cs-format-note").hidden = !json;
    if (s.format === "json_schema") {
      const p = schemaProblem();
      $("#cs-schema").setAttribute("aria-invalid", String(!!p));
      $("#cs-schema-h").textContent = p ? T("cs.schema.bad", p) : T("cs.schema.ok");
      $("#cs-schema-h").dataset.kind = p ? "err" : "ok";
    }
  }

  // a field's problem in words, or ""
  function problem(f, raw) {
    if (raw === "") return "";
    const n = Number(raw);
    if (!Number.isFinite(n) || (f.kind === "int" && !Number.isInteger(n))) return f.kind === "int" ? "a whole number" : "a number";
    if (f.min != null && n < f.min) return `≥ ${f.min}`;
    if (f.max != null && n > f.max) return `≤ ${f.max}`;
    return "";
  }
  function mark(f) {
    const el = $("#" + f.id), p = problem(f, el.value.trim());
    el.setAttribute("aria-invalid", String(!!p));
    return p;
  }

  /* the request fields: only those set, and only valid ones */
  function body() {
    const s = read(), out = {};
    for (const f of fieldsOf()) {
      if (s[f.key] === "" || problem(f, s[f.key])) continue;
      const n = Number(s[f.key]);
      if (f.key === "reasoning_budget_tokens" && n === 0) continue;
      if (f.key === "retries") continue;
      out[f.key] = n;
    }
    const stops = s.stop.split("\n").map(x => x.replace(/\r$/, "")).filter(Boolean).slice(0, 4);
    if (stops.length) out.stop = stops;
    if (s.format === "json_object") out.response_format = { type: "json_object" };
    if (s.format === "json_schema" && !schemaProblem())
      out.response_format = { type: "json_schema", json_schema: { name: (s.schema_name || "answer").trim() || "answer", strict: true, schema: JSON.parse(s.schema) } };
    if (out.response_format && s.retries !== "" && !problem(OTHER[2], s.retries)) out.structured_retries = Number(s.retries);
    return out;
  }
  const system = () => read().system.trim();
  const policy = () => read().policy;
  const threshold = () => { const v = Number(read().threshold); return Number.isFinite(v) && v >= 30 && v <= 100 ? v / 100 : 0.8; };
  const structured = () => { const f = read().format; return f === "json_object" || f === "json_schema"; };

  /* presets: the server's, else this browser's */
  function localPresets() { const p = load(PRESETS, {}); return p && typeof p === "object" && !Array.isArray(p) ? p : {}; }
  function presets() {
    if (serverPresets) return Object.fromEntries(serverPresets.map(p => [p.name, p]));
    return Object.fromEntries(Object.entries(localPresets()).map(([n, s]) => [n, { name: n, settings: s, summary: "" }]));
  }
  async function fetchPresets() {
    try {
      const r = await fetch("api/profiles", { cache: "no-store", headers: authHeaders() });
      if (!r.ok) throw new Error();
      const all = await r.json();
      serverPresets = all.inference || [];
      inferenceDefault = all.inference_default || null;
      const local = localPresets();                    // presets kept here while the server could not: move them
      for (const [name, settings] of Object.entries(local)) {
        if (serverPresets.some(p => p.name === name)) continue;
        if (await change({ action: "save", kind: "inference", name, settings })) delete local[name];
      }
      keep(PRESETS, local);
      if (Object.keys(local).length === 0) {
        const r2 = await fetch("api/profiles", { cache: "no-store", headers: authHeaders() });
        if (r2.ok) serverPresets = (await r2.json()).inference || [];
      }
    } catch (e) { serverPresets = null; }
    drawPresets($("#cs-preset").value);
    if (!started) { started = true; startWith(); }
    showActive();
  }

  /* the settings a client opens with (see the top); the Active line says which */
  function modelOnly() {
    const s = read();
    return { system: s.system, policy: s.policy, threshold: s.threshold, effort: s.effort, format: s.format,
      schema: s.schema, schema_name: s.schema_name };
  }
  function startWith() {
    if (last.edited) return;
    const all = presets();
    if (last.name && last.name !== MODEL && all[last.name]) { write(all[last.name].settings); save(); drawPresets(last.name); return; }
    if (last.name === MODEL) { write(modelOnly()); save(); return; }
    if (last.name) { last = { name: "", edited: false }; keep(LAST, last); }   // the preset was deleted: the default
    followDefault();
  }
  function followDefault() {
    const p = inferenceDefault && presets()[inferenceDefault];
    write(p ? p.settings : modelOnly()); save();
    drawPresets(p ? inferenceDefault : "");
  }
  function remember(name, edited) { last = { name, edited }; keep(LAST, last); showActive(); }
  function showActive() {
    const box = $("#cs-active");
    if (!box) return;
    const def = inferenceDefault && presets()[inferenceDefault] ? inferenceDefault : "";
    let text;
    if (last.name === MODEL) text = T("cs.active.model");
    else if (last.name) text = T("cs.active.preset", last.name);
    else text = def ? T("cs.active.default", def) : T("cs.active.model.default");
    box.textContent = last.edited ? T("cs.active.edited", text) : text;
    $("#cs-machine").hidden = !(serverPresets && serverPresets.some(p => p.name === "Machine Default"));
  }
  async function change(req) {
    try {
      const r = await fetch("api/profiles", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(req) });
      return r.ok ? true : (await r.json().catch(() => ({}))).error || false;
    } catch (e) { return false; }
  }
  function drawPresets(sel) {
    const box = $("#cs-preset"), all = presets(), names = Object.keys(all).sort((a, b) => a.localeCompare(b));
    box.replaceChildren(Object.assign(document.createElement("option"), { value: "", textContent: T("cs.preset.none") }),
      ...names.map(n => Object.assign(document.createElement("option"), { value: n, textContent: all[n].summary ? `${n} — ${all[n].summary}` : n })));
    box.value = names.includes(sel) ? sel : "";
    $("#cs-preset-del").disabled = $("#cs-preset-rename").disabled = !box.value;
  }
  $("#cs-preset").onchange = () => {
    const name = $("#cs-preset").value, p = presets()[name];
    $("#cs-preset-del").disabled = $("#cs-preset-rename").disabled = !name;
    if (!p) return;
    write(p.settings); save(); say(T("cs.preset.loaded", name), "ok");
    remember(name === inferenceDefault ? "" : name, false);
    $("#cs-preset-name").value = name;
    document.dispatchEvent(new CustomEvent("basalt:chatset"));
  };
  $("#cs-preset-save").onclick = async () => {
    const name = $("#cs-preset-name").value.trim();
    if (!name) { say(T("cs.preset.noname"), "err"); $("#cs-preset-name").focus(); return; }
    const ok = await change({ action: "save", kind: "inference", name, settings: read() });
    if (ok !== true) {
      const all = localPresets(); all[name] = read();
      if (!keep(PRESETS, all)) { say(T("cs.preset.nostore"), "err"); return; }
      serverPresets = null; drawPresets(name); say(T("cs.preset.local"), "ok"); return;
    }
    await fetchPresets(); drawPresets(name); say(T("cs.preset.saved", name), "ok");
  };
  $("#cs-preset-rename").onclick = async () => {
    const name = $("#cs-preset").value, to = $("#cs-preset-name").value.trim();
    if (!name) return;
    if (!to) { say(T("cs.preset.noname"), "err"); $("#cs-preset-name").focus(); return; }
    if (serverPresets) {
      const ok = await change({ action: "rename", kind: "inference", name, new_name: to });
      if (ok !== true) { say((ok && ok.message) || "error", "err"); return; }
      await fetchPresets();
    } else {
      const all = localPresets(); all[to] = all[name]; delete all[name]; keep(PRESETS, all);
    }
    drawPresets(to); say(T("cs.preset.renamed", to), "ok");
  };
  $("#cs-preset-del").onclick = async () => {
    const name = $("#cs-preset").value;
    if (!name) return;
    if (serverPresets) { await change({ action: "delete", kind: "inference", name }); await fetchPresets(); }
    else { const all = localPresets(); delete all[name]; keep(PRESETS, all); }
    drawPresets(""); say(T("cs.preset.deleted", name), "ok");
  };
  $("#cs-preset-import").onchange = async e => {
    const f = e.target.files[0];
    e.target.value = "";
    if (!f) return;
    try {
      const data = JSON.parse(await f.text());
      const ok = await change({ action: "import", data });
      if (ok !== true) throw new Error((ok && ok.message) || "the server did not take the file");
      await fetchPresets(); say(T("cs.preset.imported"), "ok");
    } catch (err) { say(err.message, "err"); }
  };
  $("#cs-reset").onclick = () => {
    write(modelOnly());
    save(); say(T("cs.reset.done"), "ok");
    drawPresets(""); remember(MODEL, false);
    document.dispatchEvent(new CustomEvent("basalt:chatset"));
  };
  $("#cs-machine").onclick = async () => {
    await fetchPresets();
    const p = presets()["Machine Default"];
    if (!p) { say(T("cs.machine.none"), "err"); return; }
    write(p.settings); save(); drawPresets("Machine Default");
    remember(inferenceDefault === "Machine Default" ? "" : "Machine Default", false);
    say(T("cs.machine.done"), "ok");
    document.dispatchEvent(new CustomEvent("basalt:chatset"));
  };

  /* panel */
  function openPanel(on) {
    $("#chatset").hidden = !on;
    $("#cs-open").setAttribute("aria-expanded", String(on));
    $("#view-chat").classList.toggle("withset", on);
    keep("basalt.chatset.open", on);
    if (on) { $("#cs-title").scrollIntoView({ block: "nearest" }); fetchPresets(); }
  }
  $("#cs-open").onclick = () => openPanel($("#chatset").hidden);
  $("#cs-close").onclick = () => { openPanel(false); $("#cs-open").focus(); };
  $("#chatset").addEventListener("keydown", e => { if (e.key === "Escape") { openPanel(false); $("#cs-open").focus(); } });
  // a setting changed by hand (not the preset controls): kept as it is on the next opening
  const byHand = e => e.target.closest && !e.target.closest("#cs-preset-box") && e.target.id !== "cs-show-reasoning";
  $("#cs-form").addEventListener("input", e => {
    const f = fieldsOf().find(x => x.id === e.target.id);
    if (f) { const p = mark(f); say(p ? T("cs.bad", T(f.label), p) : "", p ? "err" : ""); }
    show(); save();
    if (byHand(e) && !last.edited) remember(last.name, true);
    document.dispatchEvent(new CustomEvent("basalt:chatset"));
  });
  $("#cs-form").addEventListener("change", e => {
    show(); save();
    if (byHand(e) && !last.edited) remember(last.name, true);
    document.dispatchEvent(new CustomEvent("basalt:chatset"));
  });
  $("#effort").addEventListener("change", e => { if (e.isTrusted && !last.edited) remember(last.name, true); });
  $("#cs-form").onsubmit = e => e.preventDefault();

  /* the model's defaults as placeholders (GET /props) */
  async function defaults() {
    try {
      const h = await fetch("health", { cache: "no-store" }).then(r => r.json());
      if (h.standby) throw new Error("no model loaded yet");   // /props answers once a model is loaded
      const r = await fetch("props", { cache: "no-store", headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const p = (((await r.json()).default_generation_settings) || {}).params || {};
      const names = { repeat_penalty: "repetition_penalty", repeat_last_n: "penalty_last_n" };
      modelDefaults = Object.fromEntries(Object.entries(p).map(([k, v]) => [names[k] || k, v]));
    } catch (e) { /* the engine's own values until the model answers */ }
    for (const f of SAMPLING) {
      const v = modelDefaults[f.key], e = ENGINE_SAMPLING[f.key];
      $("#cs-" + f.key).placeholder = v != null ? T("cs.default", v) : e != null ? T("cs.engine", e) : T("cs.model");
    }
  }
  document.addEventListener("basalt:key", () => { defaults(); fetchPresets(); });
  // the wizard saved a new Machine Default: a client that follows the default takes it now
  document.addEventListener("basalt:machine", async () => {
    await fetchPresets();
    if (!last.edited && !last.name) { followDefault(); showActive(); }
  });
  document.addEventListener("basalt:view", e => { if (e.detail === "chat") defaults(); });

  // whether the chat shows the model's reasoning: a display choice of this client, not part of a preset
  const SHOW = "basalt.showreasoning";
  const showReasoning = () => load(SHOW, true) !== false;
  $("#cs-show-reasoning").checked = showReasoning();
  $("#cs-show-reasoning").addEventListener("change", e => {
    keep(SHOW, e.target.checked);
    document.dispatchEvent(new CustomEvent("basalt:showreasoning"));
  });

  write(load(STORE, {}));
  drawPresets("");
  defaults();
  fetchPresets();
  if (load("basalt.chatset.open", false) && innerWidth > 980) openPanel(true);
  const patterns = () => read().context_patterns || {};
  return { body, system, policy, threshold, structured, showReasoning, patterns };
})();
