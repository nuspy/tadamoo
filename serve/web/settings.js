"use strict";
/* Settings: the --config file's main fields through GET/POST /api/settings. The server checks every value with the
   engine's own rules and writes the file; nothing changes in the running server until it is started again. */
(() => {
  const FIELDS = {
    "server.port": { id: "f-port", kind: "int" },
    "server.parallel": { id: "f-parallel", kind: "int" },
    "server.vision": { id: "f-vision", kind: "choice" },
    "engine.max-context": { id: "f-max-context", kind: "int" },
    "engine.kv": { id: "f-kv", kind: "choice" },
    "engine.spec": { id: "f-spec", kind: "int" },
    "engine.spec-min-p": { id: "f-spec-min-p", kind: "float" },
    "engine.expert-cache": { id: "f-expert-cache", kind: "text" },
    "engine.prefill": { id: "f-prefill", kind: "text" },
  };
  let S = null;                                   // the last GET /api/settings
  let loaded = false;
  const form = $("#set-form"), fields = $("#set-fields"), statusEl = $("#set-status");
  const show = v => v == null || v === "" ? "default" : String(v);
  // what an absent key means, so "1" running and an empty field are the same setting
  const DEFAULTS = { "server.port": 7232, "server.parallel": 1, "server.vision": "off" };
  const same = (name, a, b) => String(a ?? DEFAULTS[name] ?? "") === String(b ?? DEFAULTS[name] ?? "");

  function say(text, kind) {
    statusEl.textContent = text;
    statusEl.dataset.kind = kind || "";
    statusEl.setAttribute("role", kind === "err" ? "alert" : "status");
  }

  // the value in a field as JSON: "" is the default (the key leaves the file)
  function read(name) {
    const f = FIELDS[name], raw = $("#" + f.id).value.trim();
    if (raw === "") return null;
    if (f.kind === "int" || f.kind === "float") {
      const n = Number(raw);
      return Number.isFinite(n) ? n : raw;           // the server names what is wrong with it
    }
    if (f.kind === "text" && /^\d+$/.test(raw)) return Number(raw);
    return raw;
  }
  // the browser's own check for the obvious cases, before the server's
  function local(name) {
    const el = $("#" + FIELDS[name].id), v = read(name);
    if (v == null) return "";
    if (FIELDS[name].kind === "int" && (!Number.isInteger(v) || (el.min !== "" && v < +el.min) || (el.max !== "" && v > +el.max)))
      return `expected a whole number ${el.min}${el.max ? ` to ${el.max}` : " or more"}`;
    if (FIELDS[name].kind === "float" && (typeof v !== "number" || v < +el.min || v > +el.max)) return `expected a number from ${el.min} to ${el.max}`;
    if (name === "engine.expert-cache" && !(v === "auto" || Number.isInteger(v))) return "auto or a whole number";
    if (name === "engine.prefill" && !(Number.isInteger(v) || /^auto(:(8192|16384|32768))?$/.test(v))) return "auto, auto:8192, auto:16384, auto:32768 or a whole number";
    return "";
  }
  function mark(name, problem) {
    const box = form.querySelector(`.field[data-name="${name}"]`), el = $("#" + FIELDS[name].id);
    let p = box.querySelector(".ferr");
    if (!p) { p = document.createElement("p"); p.className = "ferr"; p.id = FIELDS[name].id + "-err"; box.append(p); }
    p.textContent = problem ? `Problem: ${problem}` : "";
    p.hidden = !problem;
    el.setAttribute("aria-invalid", String(!!problem));
    const desc = el.getAttribute("aria-describedby").split(" ").filter(x => x !== p.id);
    if (problem) desc.push(p.id);
    el.setAttribute("aria-describedby", desc.join(" "));
  }

  function fill() {
    const vals = S.values || {}, run = S.running || {}, cli = S.from_command_line || [];
    $("#set-path").textContent = S.config || "None: the server was started without --config";
    $("#set-model").textContent = vals["server.model"] || S.model || "—";
    for (const [name, f] of Object.entries(FIELDS)) {
      const el = $("#" + f.id), v = vals[name];
      el.value = v == null ? "" : String(v);
      mark(name, "");
      const box = form.querySelector(`.field[data-name="${name}"]`);
      let now = box.querySelector(".now");
      if (!now) { now = document.createElement("p"); now.className = "now"; box.insertBefore(now, box.querySelector(".help")); }
      const parts = [];
      if (name in run) parts.push(`Running now: ${show(run[name])}`);
      if (cli.includes(name)) parts.push("set on the command line, which wins over the file");
      if (name in run && !same(name, run[name], v) && !cli.includes(name)) parts.push("restart to apply");
      now.textContent = parts.join(" · ");
      now.hidden = !parts.length;
      now.classList.toggle("pending", parts.some(p => p === "restart to apply"));
    }
    fields.disabled = !S.editable;
    $("#set-reason").hidden = S.editable || !S.reason;
    $("#set-reason").textContent = S.reason ? `Read-only: ${S.reason}` : "";
  }

  async function refresh() {
    try {
      const r = await fetch("api/settings", { cache: "no-store", headers: authHeaders() });
      if (r.status === 401) { say("This server needs an API key: enter it above.", "err"); askKey(); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      S = await r.json();
      loaded = true;
      fill();
      if (!statusEl.dataset.kind || statusEl.dataset.kind === "err") say("");
    } catch (e) {
      say(`Cannot read the settings: ${e.message}`, "err");
    }
  }

  form.onsubmit = async e => {
    e.preventDefault();
    if (!S || !S.editable) return;
    const changes = {}, vals = S.values || {};
    let bad = null;
    for (const name of Object.keys(FIELDS)) {
      const problem = local(name);
      mark(name, problem);
      if (problem && !bad) bad = name;
      const v = read(name), was = vals[name] == null ? null : vals[name];
      if (String(v) !== String(was)) changes[name] = v;
    }
    if (bad) { say("Fix the marked fields first.", "err"); $("#" + FIELDS[bad].id).focus(); return; }
    if (!Object.keys(changes).length) { say("Nothing to save: the file already has these values."); return; }
    $("#set-save").disabled = true;
    say("Saving…");
    try {
      const r = await fetch("api/settings", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ changes }) });
      const j = await r.json().catch(() => ({}));
      if (r.status === 401) { askKey(); throw new Error("this server needs an API key: enter it above"); }
      if (!r.ok) {
        const msg = (j.error && j.error.message) || `HTTP ${r.status}`;
        const name = Object.keys(FIELDS).find(n => msg.startsWith(n + ":"));
        if (name) { mark(name, msg.slice(name.length + 1).trim()); $("#" + FIELDS[name].id).focus(); }
        throw new Error(msg);
      }
      await refresh();
      say(j.changed && j.changed.length ? `Saved ${j.changed.join(", ")}. Restart the server to apply them.` : "Nothing changed.", "ok");
    } catch (err) {
      say(`Not saved: ${err.message}`, "err");
    } finally {
      $("#set-save").disabled = false;
    }
  };
  $("#set-revert").onclick = () => { if (S) { fill(); say("Reverted to the file's values."); } };
  // a marked field is cleared as soon as it is right; problems are shown on Save (marking them on blur would move
  // the Save button under the pointer that is clicking it)
  for (const name of Object.keys(FIELDS)) {
    $("#" + FIELDS[name].id).addEventListener("input", e => {
      if (e.target.getAttribute("aria-invalid") === "true" && !local(name)) mark(name, "");
    });
  }

  /* the user's language (serve/lang.py): the app, this page, the scripts and the command-line tools follow it */
  applyT($(".stlang"));
  async function langRefresh() {
    try { const p = await (await fetch("api/prefs", { cache: "no-store" })).json(); $("#st-lang").value = p.chosen || "auto"; }
    catch (e) { /* the select keeps Automatic */ }
  }
  $("#st-lang").addEventListener("change", async e => {
    const v = e.target.value;
    const r = await fetch("api/prefs", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ lang: v === "auto" ? null : v }) });
    if (!r.ok) { $("#st-lang-msg").textContent = `HTTP ${r.status}`; $("#st-lang-msg").dataset.kind = "err"; return; }
    try { localStorage.removeItem("basalt.lang"); } catch (err) { /* nothing kept here */ }
    $("#st-lang-msg").textContent = T("st.lang.saved"); $("#st-lang-msg").dataset.kind = "ok";
    setTimeout(() => location.reload(), 600);
  });
  document.addEventListener("basalt:view", e => { if (e.detail === "settings") langRefresh(); });
  if (viewOn === "settings") langRefresh();
  document.addEventListener("basalt:view", e => { if (e.detail === "settings") refresh(); });
  document.addEventListener("basalt:key", () => { if (viewOn === "settings" || loaded) refresh(); });
  if (viewOn === "settings") refresh();
})();
