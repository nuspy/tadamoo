"use strict";
/* Models > llama.cpp: llama-server (llama.cpp) as a second engine on this PC, mostly to hold a small model for the technical session.
   Shows the program found (or the path to give), starts and stops it on 127.0.0.1 with a GGUF, its health, and an update that first
   SHOWS what it would download (release, files, sizes, destination) and downloads only after the button under that plan is pressed.
   Everything the server says is put in the page as text. Routes: api/llamacpp (GET), api/llamacpp/<path|start|stop|update/check|update/apply>. */
(() => {
  const panel = document.getElementById("ld-llamacpp");
  if (!panel) return;
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const tr = (key, ...args) => { const t = T(key, ...args); return t === key ? "" : t; };
  const mb = n => (n >= 1073741824 ? (n / 1073741824).toFixed(2) + " GB" : Math.max(1, Math.round(n / 1048576)) + " MB");
  let data = null, plan = null, timer = 0, built = false, modelsKey = "";
  const ui = {};

  async function call(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...authHeaders(), ...(body ? { "Content-Type": "application/json" } : {}) }, body: body ? JSON.stringify(body) : undefined });
      let out = {};
      try { out = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data: out };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  function say(res) {
    const err = (res.data && res.data.error) || {};
    if (res.status === 0) return T("lc.e.offline");
    return (err.code && tr("lc.e." + err.code)) || err.message || `HTTP ${res.status}`;
  }
  const note = (text, bad) => { ui.note.textContent = text || ""; ui.note.dataset.bad = bad ? "1" : ""; ui.note.hidden = !text; };

  function field(id, label, help, input) {
    const lab = h("label", "", label);
    input.id = id;
    lab.htmlFor = id;
    const p = h("p", "help", help);
    p.id = id + "-help";
    input.setAttribute("aria-describedby", p.id);
    return h("div", "field", lab, input, p);
  }
  const num = (id, label, help, value, min, max) => {
    const i = h("input");
    i.type = "number"; i.inputMode = "numeric"; i.min = String(min); i.max = String(max); i.value = String(value);
    return [field(id, label, help, i), i];
  };
  const button = (text, onclick, cls) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  function build() {
    const [fPort, port] = num("lc-port", T("lc.port"), T("lc.port.help"), 8080, 1024, 65535);
    const [fCtx, ctx] = num("lc-ctx", T("lc.ctx"), T("lc.ctx.help"), 8192, 256, 1048576);
    const [fNgl, ngl] = num("lc-ngl", T("lc.ngl"), T("lc.ngl.help"), 99, 0, 999);
    const [fPar, par] = num("lc-par", T("lc.parallel"), T("lc.parallel.help"), 1, 1, 16);
    const model = h("select"), custom = h("input"), alias = h("input"), path = h("input"), auto = h("input");
    custom.type = alias.type = path.type = "text";
    custom.spellcheck = alias.spellcheck = path.spellcheck = false;
    auto.type = "checkbox";
    Object.assign(ui, { port, ctx, ngl, par, model, custom, alias, path, auto });
    ui.status = h("p", "eng-status"); ui.status.setAttribute("role", "status");
    ui.health = h("p", "setnote"); ui.health.setAttribute("role", "status");
    ui.note = h("p", "setnote eng-note"); ui.note.setAttribute("role", "status"); ui.note.hidden = true;
    ui.facts = h("dl", "eng-facts");
    ui.start = button(T("lc.start"), start, "primary");
    ui.stop = button(T("lc.stop"), stop);
    ui.tech = button(T("lc.technical"), useAsTechnical);
    ui.check = button(T("lc.update.check"), check);
    ui.bar = h("progress", "eng-bar"); ui.bar.max = 100; ui.bar.hidden = true;
    ui.plan = h("div", "eng-section"); ui.plan.hidden = true;
    const modelLab = h("label", "", T("lc.model")); model.id = "lc-model"; modelLab.htmlFor = "lc-model";
    ui.empty = h("p", "help", T("lc.model.none"));
    const autoRow = h("div", "field", h("label", "eng-check", auto, " ", T("lc.autostart")), h("p", "help", T("lc.autostart.help")));
    auto.id = "lc-auto";
    const pathBtns = h("div", "eng-actions", button(T("lc.path.use"), usePath), button(T("lc.path.forget"), forgetPath));
    panel.replaceChildren(
      h("h3", "eng-title", T("lc.title")), h("p", "setnote eng-desc", T("lc.help")), ui.status,
      h("div", "eng-section", h("h4", "eng-sub", T("lc.program")), ui.facts,
        h("div", "fields", field("lc-path", T("lc.path"), T("lc.path.help"), path)), pathBtns),
      h("div", "eng-section", h("h4", "eng-sub", T("lc.run")),
        h("div", "fields", h("div", "field", modelLab, model, ui.empty), field("lc-custom", T("lc.model.path"), T("lc.model.path.help"), custom), fPort, fCtx, fNgl, fPar,
          field("lc-alias", T("lc.alias"), T("lc.alias.help"), alias), autoRow),
        h("div", "eng-actions", ui.start, ui.stop, ui.tech), ui.health, h("p", "setnote eng-help", T("lc.technical.help"))),
      h("div", "eng-section", h("h4", "eng-sub", T("lc.update")), h("p", "setnote", T("lc.update.help")), h("div", "eng-actions", ui.check), ui.plan, ui.bar),
      ui.note);
    built = true;
  }

  function paintPlan() {
    ui.plan.hidden = !plan;
    if (!plan) { ui.plan.replaceChildren(); return; }
    const rows = [h("dt", "", T("lc.plan.release")), h("dd", "", `${plan.name || plan.release} (${plan.release}${plan.published ? ", " + plan.published.slice(0, 10) : ""})`),
      h("dt", "", T("lc.plan.files")), h("dd", "", plan.files.map(f => `${f.name} - ${mb(f.size)}`).join("\n")),
      h("dt", "", T("lc.plan.total")), h("dd", "", mb(plan.total_bytes)),
      h("dt", "", T("lc.plan.to")), h("dd", "", plan.destination),
      h("dt", "", T("lc.plan.installed")), h("dd", "", plan.installed || T("lc.plan.none"))];
    ui.plan.replaceChildren(h("h4", "eng-sub", T("lc.plan.title")), h("dl", "eng-facts", rows),
      h("p", "setnote", plan.up_to_date ? T("lc.plan.uptodate") : T("lc.plan.ask")),
      h("div", "eng-actions", button(T("lc.plan.go"), apply, "primary"), button(T("lc.plan.no"), () => { plan = null; paintPlan(); })));
    ui.plan.querySelector("dl").style.whiteSpace = "pre-line";
  }

  function paint() {
    if (!built || !data) return;
    const st = data.state, on = st === "ready", busy = st === "starting" || on;
    ui.status.textContent = tr("lc.state." + st) || st;
    ui.status.dataset.ok = on ? "1" : "";
    ui.facts.replaceChildren(h("dt", "", T("lc.found")), h("dd", "", data.found ? `${data.path} (${tr("lc.src." + data.source) || data.source})` : T("lc.none")),
      h("dt", "", T("lc.version")), h("dd", "", data.version || "-"),
      h("dt", "", T("lc.address")), h("dd", "", data.base_url));
    ui.health.textContent = data.state === "error" && data.error ? (tr("lc.e." + data.error.code) || data.error.message || "")
      : T("lc.health", data.health && data.health.code ? `${data.health.code} ${data.health.status || ""}`.trim() : T("lc.health.none"));
    ui.start.disabled = !data.found || busy || st === "external";
    ui.stop.disabled = !busy;
    ui.tech.disabled = !on;
    const key = JSON.stringify((data.models || []).map(m => m.path));
    if (key !== modelsKey) {
      modelsKey = key;
      ui.model.replaceChildren(...(data.models || []).map(m => Object.assign(h("option", "", m.name), { value: m.path })));
      ui.empty.hidden = (data.models || []).length > 0;
      ui.model.hidden = (data.models || []).length === 0;
    }
    if (!ui.filled) {                                           // the saved settings once, so that what is being typed is never overwritten
      const s = data.settings || {};
      ui.port.value = s.port; ui.ctx.value = s.ctx; ui.ngl.value = s.ngl; ui.par.value = s.parallel; ui.alias.value = s.alias || ""; ui.auto.checked = !!s.autostart;
      if (s.model) { if ((data.models || []).some(m => m.path === s.model)) ui.model.value = s.model; else ui.custom.value = s.model; }
      ui.path.value = data.found && data.source === "saved" ? data.path : "";
      ui.filled = true;
    }
    const job = data.job;
    ui.bar.hidden = !(job && (job.status === "running" || job.status === "done"));
    ui.bar.value = job && job.total ? Math.round(100 * job.done / job.total) : 0;
    ui.check.disabled = !!(job && job.status === "running");
    if (job && job.status === "running") note(T("lc.update.running", job.file || job.release, mb(job.done), mb(job.total)), false);
    else if (job && job.status === "error") note(tr("lc.e." + job.error.code) || job.error.message, true);
    else if (job && job.status === "done" && plan) { note(T("lc.update.done", job.release), false); plan = null; paintPlan(); }
  }

  async function refresh() {
    const res = await call("GET", "api/llamacpp");
    if (!res.ok) { if (!data) panel.replaceChildren(h("p", "setnote warnnote", say(res))); return; }
    if (!built) build();
    data = res.data;
    paint();
  }

  const values = () => ({ model: ui.custom.value.trim() || ui.model.value, port: Number(ui.port.value), ctx: Number(ui.ctx.value), ngl: Number(ui.ngl.value),
      parallel: Number(ui.par.value), alias: ui.alias.value.trim(), autostart: ui.auto.checked });
  async function start() { const r = await call("POST", "api/llamacpp/start", values()); note(r.ok ? "" : say(r), !r.ok); refresh(); poll(); }
  async function stop() { const r = await call("POST", "api/llamacpp/stop", {}); note(r.ok ? "" : say(r), !r.ok); refresh(); }
  async function usePath() { const r = await call("POST", "api/llamacpp/path", { path: ui.path.value.trim() }); note(r.ok ? T("lc.path.saved") : say(r), !r.ok); ui.filled = false; refresh(); }
  async function forgetPath() { ui.path.value = ""; await usePath(); }
  async function check() {
    note(T("lc.update.looking"), false);
    const r = await call("POST", "api/llamacpp/update/check", {});
    if (r.ok) { plan = r.data.plan; note("", false); } else { plan = null; note(say(r), true); }
    paintPlan();
  }
  async function apply() {
    if (!plan) return;
    const r = await call("POST", "api/llamacpp/update/apply", { token: plan.token, confirm: true });
    note(r.ok ? "" : say(r), !r.ok);
    refresh(); poll();
  }
  async function useAsTechnical() {
    const r = await call("POST", "api/llm/technical", { type: "llamacpp" });
    note(r.ok ? T("lc.technical.done") : say(r), !r.ok);
  }

  function poll() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (panel.hidden || document.hidden) return;
      refresh();
    }, data && (data.state === "starting" || (data.job && data.job.status === "running")) ? 1000 : 4000);
  }
  document.addEventListener("basalt:models-sub", e => {
    if (e.detail === "llamacpp") { refresh().then(poll); } else clearInterval(timer);
  });
  document.addEventListener("tadamoo:lang", () => { if (built) { ui.filled = false; build(); paint(); paintPlan(); } });
  if (!panel.hidden) refresh().then(poll);
})();
