"use strict";
/* Inference settings: sampling, reasoning, output length, stop strings, system prompt, structured output, the context policy and named presets.
   One panel, bound to one thing at a time: a conversation that is open (its settings are saved with it, POST /api/agent/sessions/<id>/inference), a plan
   (saved in the plan's profile), the machine default (the inference preset "Machine Default"), or no conversation yet (the changes wait in this browser until
   the conversation is made, which takes them). Every new conversation and every plan starts from the machine default. Presets are kept by the server in
   presets.json in the data folder (GET/POST /api/profiles, shared with the desktop app); moving them to another PC is copying that file. A request carries
   only the fields that are set, so the model file's defaults apply to the rest. */
const chatSettings = (() => {
  const PENDING = "tadamoo.chatset";           // the changes made while no conversation was open, until one is made
  const MACHINE = "Machine Default";
  const SAVE_AFTER_MS = 500;
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); return true; } catch (e) { return false; } };
  const forget = k => { try { localStorage.removeItem(k); } catch (e) { /* kept as it is */ } };
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
    { key: "reasoning_budget_tokens", id: "cs-budget", min: -1, kind: "int", label: "cs.budget" },
    { key: "max_tokens", id: "cs-max_tokens", min: -1, kind: "int", label: "cs.max_tokens" },
    { key: "retries", id: "cs-retries", min: 0, max: 5, kind: "int", label: "cs.retries" },
    { key: "max_turns", id: "cs-max_turns", min: -1, kind: "int", label: "cs.max_turns" },     // for the Agent, not for the model: -1 or empty is no limit
    { key: "max_answer_tokens", id: "cs-max_answer_tokens", min: -1, kind: "int", label: "cs.max_answer_tokens" },
    { key: "auto_continue", id: "cs-auto_continue", min: 0, max: 20, kind: "int", label: "cs.auto_continue" },     // for the Chat's own continuing, not sent to the model
  ];
  const NO_LIMIT = ["reasoning_budget_tokens", "max_tokens", "max_answer_tokens"];
  const AUTO_CONTINUE = 4;
  // what the engine does with a key that neither the request nor the model file sets (the GEN line's defaults)
  const ENGINE_SAMPLING = { temperature: 0, top_p: 1, top_k: 20, min_p: 0, repetition_penalty: 1,
    presence_penalty: 0, frequency_penalty: 0, penalty_last_n: 64, end_turn_bias: 0 };
  let modelDefaults = {}, serverPresets = null;
  // what the panel is bound to: {kind: "new"} or {kind: "session" | "plan" | "machine", label, save(settings)}; edited: changed by hand since it was bound
  let scope = { kind: "new" }, edited = false, saveTimer = 0, hosted = null, quiet = 0;      // quiet: the panel is being written, not changed by hand

  const grid = $("#cs-sampling");
  for (const f of SAMPLING) {
    const d = document.createElement("div");
    d.className = "csnum";
    d.innerHTML = `<label for="cs-${f.key}" data-t="cs.${f.key}"></label>` +
      `<input id="cs-${f.key}" type="number" inputmode="${f.kind === "int" ? "numeric" : "decimal"}" min="${f.min}" max="${f.max}" step="${f.step}"` +
      (f.help ? ` aria-describedby="cs-${f.key}-h">` + `<p class="help" id="cs-${f.key}-h" data-t="cs.${f.key}.help"></p>` : ">");
    grid.append(d);
  }
  const effortBox = $("#cs-effort");
  applyT($("#chatset"));
  applyT($(".chathead"));

  const fieldsOf = () => [...SAMPLING.map(f => ({ ...f, id: `cs-${f.key}`, label: `cs.${f.key}` })), ...OTHER];
  function read() {
    const s = { system: $("#cs-system").value, stop: $("#cs-stop").value,
      policy: (document.querySelector('input[name="cs-policy"]:checked') || {}).value || "compact",
      threshold: $("#cs-threshold").value, effort: effortBox.value,
      format: $("#cs-format").value, schema: $("#cs-schema").value, schema_name: $("#cs-schema_name").value,
      context_patterns: window.ctxPatterns ? window.ctxPatterns.read() : {},
      show_reasoning: $("#cs-show_reasoning").value, show_tooling: $("#cs-show_tooling").value, ...extrasRead() };
    for (const f of fieldsOf()) s[f.key] = $("#" + f.id).value.trim();
    return s;
  }
  /* the other controls of the panel a preset keeps: the old answers as a short record (chat.js), Billion Context (billioncontext.js), pictures and charts (agentchat.js); a key a preset lacks leaves the control as it is */
  const box = id => ({ read: () => $("#" + id).checked, write: v => { $("#" + id).checked = !!v; } });
  const field = id => ({ read: () => $("#" + id).value, write: v => { $("#" + id).value = v ?? ""; } });
  const level = (on, id) => ({ read: () => $("#" + on).checked ? $("#" + id).value : "", write: v => { $("#" + on).checked = !!v; if (v) $("#" + id).value = v; $("#" + id).disabled = !v; } });
  const radio = name => ({ read: () => (document.querySelector(`input[name="${name}"]:checked`) || {}).value || "", write: () => {} });      // the level the agent works at without asking: the live one, written only by `apply`
  const dw = key => ({ read: () => (window.dwSet ? window.dwSet.read()[key] : undefined), write: () => {} });      // the dark web option (darkwebset.js): read from the page, written to the server only when a preset is loaded by hand
  const EXTRAS = { darkweb: dw("darkweb"), darkweb_search: dw("darkweb_search"), darkweb_read: dw("darkweb_read"), darkweb_navigation: dw("darkweb_navigation"), darkweb_watches: dw("darkweb_watches"), darkweb_leaks: dw("darkweb_leaks"), autonomy: radio("cs-perm"), abstract_on: box("cs-abs-on"), abstract_keep: field("cs-abs-keep"), code_keep: field("cs-code-keep"),
    billion_on: box("cs-bc-on"), billion_compress: box("cs-bc-compress"), billion_port: field("cs-bc-port"),
    images: level("cs-img-on", "cs-img-level"), charts: level("cs-chart-on", "cs-chart-level") };
  const extrasRead = () => Object.fromEntries(Object.entries(EXTRAS).map(([k, f]) => [k, f.read()]));
  /* written: the three of the old answers take effect by themselves (chat.js keeps them when one changes); pictures and charts reach the open conversation, Billion Context its process and the autonomy level the Chat, only when a preset is loaded by hand (`apply`), never when the panel is bound to another conversation */
  function writeExtras(s, apply) {
    quiet++;
    try {
      for (const [k, f] of Object.entries(EXTRAS)) if (s[k] !== undefined) f.write(s[k]);
      const fire = id => $("#" + id).dispatchEvent(new Event("change", { bubbles: true }));
      ["cs-abs-on", "cs-abs-keep", "cs-code-keep"].forEach(fire);
      if (apply && (s.images !== undefined || s.charts !== undefined)) fire("cs-img-on");
      if (apply && s.billion_on !== undefined) fire("cs-bc-on");
      if (apply && window.dwSet && (s.darkweb !== undefined || s.darkweb_search !== undefined || s.darkweb_read !== undefined || s.darkweb_navigation !== undefined || s.darkweb_watches !== undefined || s.darkweb_leaks !== undefined)) window.dwSet.write(s);
      const level = apply && s.autonomy ? document.querySelector(`input[name="cs-perm"][value="${s.autonomy}"]`) : null;
      if (level && !level.checked) { level.checked = true; level.dispatchEvent(new Event("change", { bubbles: true })); }      // agentchat.js sets the Chat's level, as a click on it does
    } finally { quiet--; }
  }
  function write(s, apply) {
    s = s || {};
    $("#cs-system").value = s.system || "";
    $("#cs-stop").value = Array.isArray(s.stop) ? s.stop.join("\n") : (s.stop || "");
    const p = document.querySelector(`input[name="cs-policy"][value="${s.policy || "compact"}"]`);
    if (p) p.checked = true;
    $("#cs-threshold").value = s.threshold || "";
    $("#cs-format").value = s.format || "";
    $("#cs-schema").value = s.schema || "";
    $("#cs-schema_name").value = s.schema_name || "";
    if (window.ctxPatterns) window.ctxPatterns.write(s.context_patterns);
    effortBox.value = s.effort || "";
    if (scope.kind === "new" || scope.kind === "session") $("#effort").value = s.effort || "";      // the header of the conversation shows its own level
    for (const f of fieldsOf()) $("#" + f.id).value = s[f.key] ?? "";
    $("#cs-show_reasoning").value = s.show_reasoning || "full";
    $("#cs-show_tooling").value = s.show_tooling || "full";
    writeExtras(s, apply);
    show();
    document.dispatchEvent(new CustomEvent("tadamoo:display"));
  }
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
    if (f.key === "max_turns" && n === 0) return T("cs.max_turns.bad");
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
      if (NO_LIMIT.includes(f.key) && n <= 0) continue;         // 0 or -1: no limit, the field is left out
      if (f.key === "retries" || f.key === "max_turns" || f.key === "auto_continue") continue;
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
  const autoContinue = () => { const v = read().auto_continue, n = v === "" ? AUTO_CONTINUE : Number(v); return Number.isInteger(n) && n >= 0 && n <= 20 ? n : AUTO_CONTINUE; };
  const structured = () => { const f = read().format; return f === "json_object" || f === "json_schema"; };

  /* presets: the server's (presets.json) */
  function presets() { return Object.fromEntries((serverPresets || []).map(p => [p.name, p])); }
  async function fetchPresets() {
    try {
      const r = await fetch("api/profiles", { cache: "no-store", headers: authHeaders() });
      if (!r.ok) throw new Error();
      serverPresets = (await r.json()).inference || [];
    } catch (e) { serverPresets = null; }
    drawPresets($("#cs-preset").value);
    showActive();
  }
  const machineSettings = () => { const p = presets()[MACHINE]; return p ? p.settings : null; };

  /* what the panel starts with when nothing is open: the changes made while no conversation was open, else the machine default */
  function startNew(clear) {
    if (clear) { forget(PENDING); edited = false; }
    const pending = clear ? null : load(PENDING, null);
    edited = !!pending;
    write(pending || machineSettings() || {});
    drawPresets(edited ? $("#cs-preset").value : (machineSettings() ? MACHINE : ""));
    showActive();
  }
  function showActive() {
    const box = $("#cs-active");
    if (!box) return;
    const text = scope.kind === "machine" ? T("cs.active.machine")
      : scope.kind === "plan" ? T("cs.active.plan", scope.label || "")
      : scope.kind === "session" ? T("cs.active.session")
      : machineSettings() ? T("cs.active.new") : T("cs.active.new.model");
    box.textContent = scope.kind === "new" && edited ? T("cs.active.edited", text) : text;
    $("#chatset").dataset.scope = scope.kind;
    $("#cs-machine").hidden = scope.kind === "machine" || !machineSettings();
    $("#cs-preset-box").hidden = scope.kind === "machine";
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

  /* saving: the open conversation, the plan or the machine default takes the panel's values a moment after the last change; with nothing open they wait here */
  function changed() {
    if (scope.kind === "new") { edited = true; keep(PENDING, read()); showActive(); document.dispatchEvent(new CustomEvent("tadamoo:chatset")); return; }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(flush, SAVE_AFTER_MS);
    document.dispatchEvent(new CustomEvent("tadamoo:chatset"));
  }
  async function flush() {
    clearTimeout(saveTimer); saveTimer = 0;
    if (scope.kind === "new" || !scope.save) return;
    const bad = fieldsOf().map(f => problem(f, read()[f.key])).find(Boolean);
    if (bad) return;                                          // a value that is not valid waits for the person to put it right
    const r = await scope.save(read());
    say(r === true ? T("cs.panel.saved") : (r || T("cs.panel.failed")), r === true ? "ok" : "err");
  }
  $("#cs-preset").onchange = () => {
    const name = $("#cs-preset").value, p = presets()[name];
    $("#cs-preset-del").disabled = $("#cs-preset-rename").disabled = !name;
    if (!p) return;
    write(p.settings, true); say(T("cs.preset.loaded", name), "ok");
    $("#cs-preset-name").value = name;
    changed();
  };
  $("#cs-preset-save").onclick = async () => {
    const name = $("#cs-preset-name").value.trim();
    if (!name) { say(T("cs.preset.noname"), "err"); $("#cs-preset-name").focus(); return; }
    const ok = await change({ action: "save", kind: "inference", name, settings: read() });
    if (ok !== true) { say((ok && ok.message) || "error", "err"); return; }
    await fetchPresets(); drawPresets(name); say(T("cs.preset.saved", name), "ok");
  };
  $("#cs-preset-rename").onclick = async () => {
    const name = $("#cs-preset").value, to = $("#cs-preset-name").value.trim();
    if (!name) return;
    if (!to) { say(T("cs.preset.noname"), "err"); $("#cs-preset-name").focus(); return; }
    const ok = await change({ action: "rename", kind: "inference", name, new_name: to });
    if (ok !== true) { say((ok && ok.message) || "error", "err"); return; }
    await fetchPresets();
    drawPresets(to); say(T("cs.preset.renamed", to), "ok");
  };
  $("#cs-preset-del").onclick = async () => {
    const name = $("#cs-preset").value;
    if (!name) return;
    await change({ action: "delete", kind: "inference", name }); await fetchPresets();
    drawPresets(""); say(T("cs.preset.deleted", name), "ok");
  };
  $("#cs-reset").onclick = () => {
    const s = read();
    write({ system: s.system, policy: s.policy, threshold: s.threshold, effort: s.effort, format: s.format, schema: s.schema, schema_name: s.schema_name });      // the model's own sampling
    say(T("cs.reset.done"), "ok");
    drawPresets("");
    changed();
  };
  $("#cs-machine").onclick = async () => {
    await fetchPresets();
    const s = machineSettings();
    if (!s) { say(T("cs.machine.none"), "err"); return; }
    write(s, true); drawPresets(MACHINE);
    say(T("cs.machine.done"), "ok");
    changed();
  };

  /* panel */
  function openPanel(on) {
    $("#chatset").hidden = !on;
    $("#cs-open").setAttribute("aria-expanded", String(on));
    $("#view-chat").classList.toggle("withset", on);
    keep("tadamoo.chatset.open", on);
    if (on) { $("#cs-title").scrollIntoView({ block: "nearest" }); fetchPresets(); }
  }
  const closePanel = () => { if (hosted) endEdit(); else { openPanel(false); $("#cs-open").focus(); } };
  $("#cs-open").onclick = () => openPanel($("#chatset").hidden);
  $("#cs-close").onclick = closePanel;
  $("#chatset").addEventListener("keydown", e => { if (e.key === "Escape") closePanel(); });
  // a setting changed by hand (not the preset controls)
  const byHand = e => e.target.closest && !e.target.closest("#cs-preset-box");
  $("#cs-form").addEventListener("input", e => {
    if (quiet) return;
    const f = fieldsOf().find(x => x.id === e.target.id);
    if (f) { const p = mark(f); say(p ? T("cs.bad", T(f.label), p) : "", p ? "err" : ""); }
    show();
    if (byHand(e)) changed();
  });
  $("#cs-form").addEventListener("change", e => { if (quiet) return; show(); if (byHand(e)) changed(); });
  // the header's thinking level and the panel's are one setting of the open conversation
  $("#effort").addEventListener("change", () => {
    if ((scope.kind === "new" || scope.kind === "session") && effortBox.value !== $("#effort").value) { effortBox.value = $("#effort").value; if (scope.kind === "new") changed(); }
  });
  effortBox.addEventListener("change", () => {
    if (scope.kind === "session" || scope.kind === "new") { $("#effort").value = effortBox.value; if (scope.kind === "session") $("#effort").dispatchEvent(new Event("change")); }
  });
  $("#cs-form").onsubmit = e => e.preventDefault();

  /* binding the panel */
  function bind(next) { flushNow(); scope = next; edited = false; }
  function flushNow() { if (saveTimer) flush(); }
  /* a conversation is open: its own settings, or the machine default for one that has none saved */
  function useSession(target) {
    bind({ kind: "session", id: target.id, save: target.save });
    forget(PENDING);                                            // the changes made before the conversation was made are in it now
    write(target.settings || machineSettings() || {}); drawPresets(""); showActive();
  }
  /* no conversation: a new one starts from the machine default; `clear` drops the changes made for it so far */
  function fresh(clear) {
    if (scope.kind === "new" && !clear) return;
    bind({ kind: "new" });
    startNew(clear);
  }
  /* the panel in another place of the page, for a plan or the machine default; `end` is told when it is closed */
  async function edit(host, target) {
    if (hosted) endEdit();
    if (!target.settings && serverPresets === null) await fetchPresets();      // a plan with no settings of its own shows the machine default: it is known before the panel is shown
    const panel = $("#chatset");
    hosted = { host, home: panel.parentNode, next: panel.nextSibling, was: scope, hidden: panel.hidden, end: target.end };
    host.append(panel);
    panel.hidden = false;
    bind({ kind: target.kind, label: target.label, save: target.save });
    write(target.settings || machineSettings() || {}); drawPresets(""); showActive();
    $("#cs-title").scrollIntoView({ block: "nearest" });
  }
  function endEdit() {
    if (!hosted) return;
    flushNow();
    const panel = $("#chatset"), h = hosted;
    hosted = null;
    h.home.insertBefore(panel, h.next);
    panel.hidden = h.hidden;
    const end = h.end;
    scope = { kind: "new" };
    startNew(false);
    document.dispatchEvent(new CustomEvent("tadamoo:chatset-released"));      // the conversation that was open takes the panel back
    if (end) end();
  }
  const editing = () => (hosted ? scope.kind : "");

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
  document.addEventListener("tadamoo:key", () => { defaults(); fetchPresets(); });
  // the wizard saved a new Machine Default: a new conversation that was not changed by hand takes it now
  document.addEventListener("basalt:machine", async () => {
    await fetchPresets();
    if (scope.kind === "new" && !edited) startNew(false);
  });
  document.addEventListener("tadamoo:view", e => { if (e.detail === "chat" || e.detail === "agent") defaults(); });

  // how the chat shows the reasoning and the actions (full, compact or absent): settings a preset keeps like the others
  const shows = kind => { const v = $("#cs-show_" + kind).value; return v === "compact" || v === "absent" ? v : "full"; };
  $("#cs-form").addEventListener("change", e => { if (/^cs-show_/.test(e.target.id)) document.dispatchEvent(new CustomEvent("tadamoo:display")); });

  startNew(false);
  drawPresets("");
  defaults();
  fetchPresets().then(() => { if (scope.kind === "new" && !edited) startNew(false); });
  if (load("tadamoo.chatset.open", false) && innerWidth > 980) openPanel(true);
  const patterns = () => read().context_patterns || {};
  return { body, system, policy, threshold, structured, shows, autoContinue, patterns, read, useSession, fresh, edit, endEdit, editing, machineSettings };
})();
