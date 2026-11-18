"use strict";
/* Settings > Knowledge: the general settings of the knowledge archives (one copy for the whole app, not per project): how Docling reads, semantic
   search and its own embeddings environment, where images are kept, and our own PostgreSQL. The per-project parts (projects, archives, documents,
   sources, try a question) stay on the Knowledge tab itself. Everything the server returns enters the page as text. */
(() => {
  const root = document.getElementById("rgset-root");
  if (!root) return;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rg.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const field = (id, labelText, help, control) => {
    const wrap = el("div", "rg-field"), lab = el("label", "", labelText); lab.htmlFor = id; control.id = id;
    wrap.append(lab, control); if (help) wrap.append(el("p", "rg-help", help));
    return wrap;
  };
  const textInput = (value, max) => { const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max; i.autocomplete = "off"; i.spellcheck = false; return i; };
  const numberInput = (value, lo, hi, step) => { const i = el("input"); i.type = "number"; i.min = String(lo); i.max = String(hi); i.step = String(step); i.value = String(value); i.inputMode = "decimal"; return i; };
  const checkField = (id, labelText, help, on) => {
    const box = el("input"), wrap = el("div", "rg-field rg-check"), lab = el("label", "");
    box.type = "checkbox"; box.checked = !!on; box.id = id; lab.htmlFor = id;
    lab.append(box, el("span", "", labelText));
    wrap.append(lab, el("p", "rg-help", help));
    return { wrap, box };
  };
  const ENV_DEVICE = { cuda: "rg.sem.env.device.cuda", cpu: "rg.sem.env.device.cpu" };

  let G = null, note = "", bad = false, envBusy = false, envStage = "", envDevice = "";

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  async function load() {
    const r = await api("GET", "api/rag/settings");
    if (r.ok) G = r.data; else { note = why(r); bad = true; }
    draw();
  }
  async function saveSettings(body) {
    const r = await api("POST", "api/rag/settings", body);
    note = r.ok ? T("rg.saved") : why(r); bad = !r.ok;
    await load();
  }
  async function testEmbedding() {
    note = T("rg.sem.testing"); bad = false; draw();
    const r = await api("POST", "api/rag/embedding/test", {});
    if (!r.ok) { note = why(r); bad = true; }
    else if (r.data.ok) { note = T("rg.sem.test.ok", r.data.model, String(r.data.device || "").toUpperCase(), r.data.seconds, r.data.dim); bad = false; }
    else { note = T("rg.sem.test.bad", r.data.error || ""); bad = true; }
    await load();
  }
  async function installEmbedEnv() {
    if (envBusy) return;
    envBusy = true; envStage = ""; draw();
    const r = await api("POST", "api/rag/embedding/env/install", {});
    if (!r.ok) { envBusy = false; note = why(r); bad = true; draw(); return; }
    let job = r.data.job;
    for (;;) {
      envStage = job.stage || ""; draw();
      if (job.state !== "running") break;
      await new Promise(res => setTimeout(res, 800));
      const p = await api("GET", `api/rag/jobs/${encodeURIComponent(job.id)}`);
      if (!p.ok) { envBusy = false; note = why(p); bad = true; draw(); return; }
      job = p.data.job;
    }
    envBusy = false; envStage = "";
    if (job.state === "done" && job.result && job.result.ok) { note = T("rg.sem.env.done"); bad = false; }
    else { note = (job.state === "done" && job.result && job.result.error) || T("rg.sem.env.failed"); bad = true; }
    await load();
  }
  async function probeEmbedEnv() {
    const r = await api("POST", "api/rag/embedding/env/probe", {});
    if (r.ok) { envDevice = r.data.device || ""; draw(); } else { note = why(r); bad = true; draw(); }
  }

  function settingsBlock() {
    const box = el("details", "rg-settings"), sum = el("summary", "rg-sum", T("rg.set.title"));
    const keep = el("select");
    [["keep", "rg.set.flagged.keep"], ["drop", "rg.set.flagged.drop"]].forEach(([k, key]) => { const o = el("option", "", T(key)); o.value = k; keep.append(o); });
    keep.value = G.settings.flagged;
    const device = el("select");
    [["auto", "rg.set.device.auto"], ["cpu", "rg.set.device.cpu"]].forEach(([k, key]) => { const o = el("option", "", T(key)); o.value = k; device.append(o); });
    device.value = G.settings.docling_device || "auto";
    const go = btn(T("rg.save"), "primary", () => saveSettings({ docling_device: device.value, flagged: keep.value }));
    box.append(sum, el("p", "rg-help", G.docling_python ? T("rg.set.docling.found", G.docling_python) : T("rg.set.docling.none")),
      field("rgset-device", T("rg.set.device"), T("rg.set.device.help"), device), field("rgset-flagged", T("rg.set.flagged"), T("rg.set.flagged.help"), keep), go);
    return box;
  }
  function envBlock(env) {
    const box = el("div", "rg-env");
    const line = env.installed ? T("rg.sem.env.installed") : (env.python_found ? T("rg.sem.env.none") : T("rg.sem.env.nopython"));
    box.append(el("p", "rg-help", line));
    if (envBusy) { box.append(el("p", "rg-help", envStage ? T("rg.sem.env.doing", envStage) : T("rg.sem.env.doing0"))); return box; }
    const row = el("div", "rd-actions");
    if (!env.installed) { const go = btn(T("rg.sem.env.install"), "", installEmbedEnv); go.disabled = !env.python_found; row.append(go); }
    else { const probe = btn(T("rg.sem.env.probe"), "", probeEmbedEnv); row.append(probe); if (ENV_DEVICE[envDevice]) row.append(el("span", "rg-note", T(ENV_DEVICE[envDevice]))); }
    box.append(row);
    return box;
  }
  function semanticBlock() {
    const e = G.embedding || {}, cfg = G.settings, box = el("details", "rg-settings"), models = (e.models || []).filter(m => m.kind === "embed");
    box.append(el("summary", "rg-sum", T("rg.sem.title")));
    if (e.enabled === undefined) return null;
    const on = checkField("rgset-sem-on", T("rg.sem.on"), T("rg.sem.on.help"), cfg.embed_enabled);
    const pick = el("select");
    const auto = el("option", "", T("rg.sem.model.auto")); auto.value = ""; pick.append(auto);
    models.forEach(m => { const o = el("option", "", T("rg.sem.model.opt", m.name, (m.bytes / (1 << 30)).toFixed(1))); o.value = m.name; pick.append(o); });
    pick.value = models.some(m => m.name === cfg.embed_model) ? cfg.embed_model : "";
    const roots = (e.roots || []).join(", ");
    const rerank = checkField("rgset-sem-rerank", T("rg.sem.rerank"), T("rg.sem.rerank.help"), cfg.rerank);
    const weight = numberInput(cfg.lexical_weight, 0, 0.3, 0.01), cos = numberInput(cfg.min_cosine, 0, 0.9, 0.05), rr = numberInput(cfg.min_rerank, 0, 0.95, 0.05);
    const idle = numberInput(cfg.idle_s, 10, 3600, 10), dir = textInput(cfg.embed_models_dir, 500);
    const go = btn(T("rg.save"), "primary", () => saveSettings({ embed_enabled: on.box.checked, embed_model: pick.value, rerank: rerank.box.checked, lexical_weight: Number(weight.value),
      min_cosine: Number(cos.value), min_rerank: Number(rr.value), idle_s: Number(idle.value), embed_models_dir: dir.value.trim() }));
    const test = btn(T("rg.sem.test"), "", testEmbedding); test.disabled = !e.available;
    box.append(on.wrap, field("rgset-sem-model", T("rg.sem.model"), models.length ? T("rg.sem.model.help", roots) : T("rg.sem.model.none", roots), pick), rerank.wrap,
      field("rgset-sem-weight", T("rg.sem.weight"), T("rg.sem.weight.help"), weight), field("rgset-sem-cos", T("rg.sem.mincos"), T("rg.sem.mincos.help"), cos),
      field("rgset-sem-rr", T("rg.sem.minrr"), T("rg.sem.minrr.help"), rr), field("rgset-sem-idle", T("rg.sem.idle"), T("rg.sem.idle.help"), idle),
      el("p", "rg-help", T("rg.sem.python.help", e.python || "-")), field("rgset-sem-dir", T("rg.sem.dir"), T("rg.sem.dir.help"), dir), go, test, envBlock(e.env || {}));
    return box;
  }

  function draw() {
    if (!G) { root.replaceChildren(el("p", "rg-help", note || T("rg.loading"))); return; }
    const box = x => { const w = el("section", "rg-box"); w.append(x); return w; };
    const status = el("p", "rg-note", note); status.setAttribute("role", "status"); status.dataset.bad = bad ? "1" : ""; status.hidden = !note;
    root.replaceChildren(status, ...[settingsBlock(), semanticBlock(), window.RagImages ? window.RagImages.block(draw) : "", window.RagPostgres ? window.RagPostgres.block(draw) : ""].filter(Boolean).map(box));
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "settings" && G) draw(); });
  if (document.body.dataset.view === "settings") load();
})();
