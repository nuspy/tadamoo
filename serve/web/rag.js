"use strict";
/* Knowledge: the archives of documents of a project (a workspace folder, or the Chat's own folder). Pick the project, make an archive, add files or
   a path, see how each is indexed, try a question. Every conversation held in the project draws on its archives, whatever personality speaks.
   Every archive is searched by words; with an embedding model installed it is searched by meaning too. Everything the server returns enters the
   page as text. */
(function () {
  const host = document.getElementById("rg-root");
  if (!host) return;

  const ARM_MS = 4000, TOP_K = 4, OTHERS = ["ask_dialog", "allow", "deny", "ask_voice"];
  const OTHERS_WORDS = { ask_dialog: "rg.others.ask_dialog", allow: "rg.others.allow", deny: "rg.others.deny", ask_voice: "rg.others.ask_voice" };
  let jobNow = "", S = null, projects = [], project = "", picked = false, chatKey = "", list = [], chosen = "", creating = false, detail = null, note = "", noteBad = false, armed = "", armTimer = 0, question = "", results = null, asking = false, working = false;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rg.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const size = n => (n >= 1 << 20 ? `${(n / (1 << 20)).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`);
  const say = (text, bad) => { note = text; noteBad = !!bad; };
  const knowledgeOn = () => document.body.dataset.view === "rag";

  async function api(method, path, body, raw) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: raw || (body ? JSON.stringify(body) : undefined) });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function arm(key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = ""; paint(); }, ARM_MS);
    paint();
  }

  const norm = p => String(p || "").replace(/\\/g, "/").replace(/\/+$/, "").toLowerCase();
  /* the project of the conversation that is open, else the Chat's own folder */
  function defaultProject() {
    const rec = window.agentChat && window.agentChat.record ? window.agentChat.record() : null;
    const ws = rec && rec.workspace ? norm(rec.workspace) : "";
    const mine = ws && projects.find(p => norm(p.path) === ws);
    return ((mine || projects.find(p => p.chat) || projects[0]) || {}).key || "";
  }
  async function loadProjects() {
    const r = await api("GET", "api/rag/projects");
    if (!r.ok) return r;
    projects = r.data.projects || [];
    if (!projects.some(p => p.key === project)) { project = ""; picked = false; }
    if (!picked) project = defaultProject();
    return r;
  }
  async function loadArchives() {
    const r = await api("GET", `api/rag?project=${encodeURIComponent(project)}`);
    if (r.ok) { S = r.data; list = r.data.archives || []; }
    return r;
  }
  async function refresh() {
    const p = await loadProjects();
    if (!p.ok) { S = null; say(why(p), true); paint(); return; }
    const r = await loadArchives();
    if (!r.ok) { S = null; say(why(r), true); paint(); return; }
    if (chosen && !list.some(a => a.id === chosen)) { chosen = ""; detail = null; }
    if (S.jobs && S.jobs.length && !jobNow && !working) {                 // a document is still being read: show how it goes
      const job = S.jobs[0];
      working = true;
      followJob(job, String(job.label || "").replace(/^(add|index) /, "")).then(out => { working = false; say(out.text, !out.ok); reloadAll(); });
    }
    if (chosen) await reloadDetail(); else paint();
  }
  async function reloadDetail() {
    const r = await api("GET", `api/rag/archives/${encodeURIComponent(chosen)}`);
    if (!r.ok) { say(why(r), true); detail = null; if (r.status === 404) chosen = ""; paint(); return; }
    detail = r.data;
    paint();
  }
  async function open(id) {
    chosen = id; creating = false; armed = ""; detail = null; results = null; question = ""; say("", false);
    await reloadDetail();
  }
  async function reloadAll() {
    await loadProjects();
    await loadArchives();
    if (chosen) await reloadDetail(); else paint();
  }

  /* ---- actions ---- */
  async function create(name, description) {
    const r = await api("POST", "api/rag/archives", { name, description, project });
    if (!r.ok) { say(why(r), true); paint(); return; }
    creating = false; chosen = r.data.archive.id; say(T("rg.created"), false); results = null; question = "";
    await reloadAll();
  }
  async function save(body) {
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/update`, body);
    say(r.ok ? T("rg.saved") : why(r), !r.ok);
    await reloadAll();
  }
  async function remove() {
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/delete`, { confirm: true });
    armed = "";
    if (!r.ok) { say(why(r), true); paint(); return; }
    chosen = ""; detail = null; results = null; say(T("rg.deleted"), false);
    await reloadAll();
  }
  /* a document is added by a job on the server (a big one takes minutes): follow it, show the stage and the page, end with its result */
  const JOB_MS = 800;
  const STAGES = { reading: "rg.job.stage.reading", cutting: "rg.job.stage.cutting", indexing: "rg.job.stage.indexing" };
  const READ_BY = { docling: "rg.doc.read.docling", basic: "rg.doc.read.basic" };
  const REASON = { off: "rg.sem.reason.off", detecting: "rg.sem.reason.detecting", no_python: "rg.sem.reason.no_python", no_model: "rg.sem.reason.no_model" };
  const LEASE = { busy: "rg.sem.lease.busy", unavailable: "rg.sem.lease.unavailable", no_engine: "rg.sem.lease.no_engine", not_enough: "rg.sem.lease.not_enough", no_cuda: "rg.sem.lease.no_cuda" };
  async function followJob(job, name) {
    jobNow = job.id;
    for (;;) {
      const stage = T(STAGES[job.stage] || STAGES.reading);
      const counting = job.stage === "indexing" ? "rg.job.passages" : "rg.job.pages";
      say((job.pages ? T(counting, name, stage, job.page, job.pages) : T("rg.job.working", name, stage)) + (job.stage === "indexing" && job.device === "cpu" ? "\n" + T("rg.job.cpu") : ""), false); paint();
      if (job.state !== "running") break;
      await new Promise(r => setTimeout(r, JOB_MS));
      const r = await api("GET", `api/rag/jobs/${encodeURIComponent(job.id)}`);
      if (!r.ok) { jobNow = ""; return { ok: false, text: T("rg.add.fail", name, why(r)) }; }
      job = r.data.job;
    }
    jobNow = "";
    if (job.state === "done") {
      const got = job.result;
      return { ok: true, text: (got.document ? T("rg.add.done", name, got.document.chunks) : T("rg.index.done", got.chunks)) + (got.note ? " " + got.note : "") };
    }
    return { ok: false, text: job.state === "cancelled" ? T("rg.job.cancelled", name) : T("rg.add.fail", name, job.error) };
  }
  async function cancelJob() {
    if (jobNow) await api("POST", `api/rag/jobs/${encodeURIComponent(jobNow)}/cancel`, {});
  }
  async function addFiles(files) {
    if (!files.length || working) return;
    working = true;
    const lines = []; let bad = false;
    for (const f of files) {
      say(T("rg.add.reading", f.name), false); paint();
      const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/upload?name=${encodeURIComponent(f.name)}`, null, f);
      const out = r.ok ? await followJob(r.data.job, f.name) : { ok: false, text: T("rg.add.fail", f.name, why(r)) };
      lines.push(out.text); bad = bad || !out.ok;
    }
    working = false;
    say(lines.join("\n"), bad);
    await reloadAll();
  }
  async function addPath(path) {
    if (!path.trim() || working) return;
    working = true; say(T("rg.add.reading", path), false); paint();
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/documents`, { path: path.trim() });
    const out = r.ok ? await followJob(r.data.job, path.trim().split(/[\\/]/).pop()) : { ok: false, text: why(r) };
    working = false;
    say(out.text, !out.ok);
    await reloadAll();
  }
  async function saveSettings(body) {
    const r = await api("POST", "api/rag/settings", body);
    say(r.ok ? T("rg.saved") : why(r), !r.ok);
    await reloadAll();
  }
  async function removeDoc(doc) {
    armed = "";
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/documents/${encodeURIComponent(doc.id)}/remove`, {});
    say(r.ok ? T("rg.doc.removed", doc.name) : why(r), !r.ok);
    results = null;
    await reloadAll();
  }
  async function reindex() {
    if (working) return;
    working = true; say(T("rg.index.working"), false); paint();
    const name = detail ? detail.archive.name : "";
    const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/reindex`, {});
    const out = r.ok ? await followJob(r.data.job, name) : { ok: false, text: why(r) };
    working = false;
    say(out.text, !out.ok);
    await reloadAll();
  }
  async function testEmbedding() {
    say(T("rg.sem.testing"), false); paint();
    const r = await api("POST", "api/rag/embedding/test", {});
    if (!r.ok) say(why(r), true);
    else if (r.data.ok) say(T("rg.sem.test.ok", r.data.model, String(r.data.device || "").toUpperCase(), r.data.seconds, r.data.dim), false);
    else say(T("rg.sem.test.bad", r.data.error || ""), true);
    await reloadAll();
  }
  async function ask() {
    if (!question.trim() || asking) return;
    asking = true; results = null; paint();
    const r = await api("POST", "api/rag/search", { query: question.trim(), archives: [chosen], top_k: TOP_K });
    asking = false;
    if (!r.ok) { say(why(r), true); results = null; } else { say("", false); results = r.data; }
    paint();
  }

  /* ---- the page ---- */
  function field(id, labelText, help, control, target) {
    const wrap = el("div", "rg-field"), lab = el("label", "", labelText);
    lab.htmlFor = id; (target || control).id = id;
    wrap.append(lab, control);
    if (help) wrap.append(el("p", "rg-help", help));
    return wrap;
  }
  function textInput(value, max) {
    const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max; i.autocomplete = "off"; i.spellcheck = false;
    return i;
  }
  function projectPicker() {
    const sel = el("select");
    projects.forEach(p => { const o = el("option", "", T("rg.proj.opt", p.chat ? T("rg.proj.chat") : p.name, p.archives)); o.value = p.key; sel.append(o); });
    sel.value = project;
    sel.onchange = () => {
      project = sel.value; picked = true; chosen = ""; creating = false; detail = null; results = null; question = ""; armed = ""; say("", false);
      refresh();
    };
    const wrap = field("rg-project", T("rg.proj.label"), T("rg.proj.help"), sel), cur = projects.find(p => p.key === project);
    wrap.classList.add("rg-project");
    if (cur) wrap.append(el("p", "rg-help rg-path", cur.path));
    return wrap;
  }
  /* where the semantic search stands: the model and where it runs, or why it is not on */
  function semanticLine() {
    const e = S.embedding || {};
    if (e.enabled === undefined) return "";
    if (!e.available) return T("rg.sem.words", REASON[e.reason] ? T(REASON[e.reason]) : (e.message || ""));
    if (e.device === "cuda") return T("rg.sem.status.gpu", e.model);
    if (e.device === "cpu") return T("rg.sem.status.cpu", e.model, LEASE[e.lease_reason] ? T(LEASE[e.lease_reason]) : (e.lease_message || T("rg.sem.lease.other")));
    return T("rg.sem.status.idle", e.model, S.settings.idle_s);
  }
  function banner() {
    const on = S.embedding && S.embedding.available, box = el("div", "rg-banner"), line = semanticLine();
    box.setAttribute("role", "status"); box.dataset.mode = on ? "hybrid" : "text";
    box.append(el("p", "rg-banner-t", on ? T("rg.hybrid", S.embedding.model) : T("rg.textonly")));
    if (line) box.append(el("p", "rg-help", line));
    else if (!on) box.append(el("p", "rg-help", T("rg.textonly.help")));
    if (!S.pdf && S.extraction !== "docling") box.append(el("p", "rg-help", T("rg.nopdf")));
    box.append(el("p", "rg-help", S.extraction === "docling" ? T("rg.read.docling") : T("rg.read.basic")));
    return box;
  }
  function settingsBlock() {
    const box = el("details", "rg-settings"), sum = el("summary", "rg-sum", T("rg.set.title"));
    const keep = el("select");
    [["keep", "rg.set.flagged.keep"], ["drop", "rg.set.flagged.drop"]].forEach(([k, key]) => { const o = el("option", "", T(key)); o.value = k; keep.append(o); });
    keep.value = S.settings.flagged;
    const device = el("select");
    [["auto", "rg.set.device.auto"], ["cpu", "rg.set.device.cpu"]].forEach(([k, key]) => { const o = el("option", "", T(key)); o.value = k; device.append(o); });
    device.value = S.settings.docling_device || "auto";
    const go = btn(T("rg.save"), "primary", () => saveSettings({ docling_device: device.value, flagged: keep.value }));
    box.append(sum, el("p", "rg-help", S.docling_python ? T("rg.set.docling.found", S.docling_python) : T("rg.set.docling.none")),
      field("rg-device", T("rg.set.device"), T("rg.set.device.help"), device), field("rg-flagged", T("rg.set.flagged"), T("rg.set.flagged.help"), keep), go);
    return box;
  }
  function numberInput(value, lo, hi, step) {
    const i = el("input"); i.type = "number"; i.min = String(lo); i.max = String(hi); i.step = String(step); i.value = String(value); i.inputMode = "decimal";
    return i;
  }
  function checkField(id, labelText, help, on) {
    const box = el("input"), wrap = el("div", "rg-field rg-check"), lab = el("label", "");
    box.type = "checkbox"; box.checked = !!on; box.id = id; lab.htmlFor = id;
    lab.append(box, el("span", "", labelText));
    wrap.append(lab, el("p", "rg-help", help));
    return { wrap, box };
  }
  /* semantic search: the embedding model and the reranker, how they are blended, and when the GPU is given back */
  function semanticBlock() {
    const e = S.embedding || {}, cfg = S.settings, box = el("details", "rg-settings"), models = (e.models || []).filter(m => m.kind === "embed");
    box.append(el("summary", "rg-sum", T("rg.sem.title")));
    if (e.enabled === undefined) return null;
    const on = checkField("rg-sem-on", T("rg.sem.on"), T("rg.sem.on.help"), cfg.embed_enabled);
    const pick = el("select");
    const auto = el("option", "", T("rg.sem.model.auto")); auto.value = ""; pick.append(auto);
    models.forEach(m => { const o = el("option", "", T("rg.sem.model.opt", m.name, (m.bytes / (1 << 30)).toFixed(1))); o.value = m.name; pick.append(o); });
    pick.value = models.some(m => m.name === cfg.embed_model) ? cfg.embed_model : "";
    const roots = (e.roots || []).join(", ");
    const rerank = checkField("rg-sem-rerank", T("rg.sem.rerank"), T("rg.sem.rerank.help"), cfg.rerank);
    const weight = numberInput(cfg.lexical_weight, 0, 0.3, 0.01), cos = numberInput(cfg.min_cosine, 0, 0.9, 0.05), rr = numberInput(cfg.min_rerank, 0, 0.95, 0.05);
    const idle = numberInput(cfg.idle_s, 10, 3600, 10), dir = textInput(cfg.embed_models_dir, 500);
    const go = btn(T("rg.save"), "primary", () => saveSettings({ embed_enabled: on.box.checked, embed_model: pick.value, rerank: rerank.box.checked, lexical_weight: Number(weight.value),
      min_cosine: Number(cos.value), min_rerank: Number(rr.value), idle_s: Number(idle.value), embed_models_dir: dir.value.trim() }));
    const test = btn(T("rg.sem.test"), "", testEmbedding); test.disabled = !e.available;
    box.append(on.wrap, field("rg-sem-model", T("rg.sem.model"), models.length ? T("rg.sem.model.help", roots) : T("rg.sem.model.none", roots), pick), rerank.wrap,
      field("rg-sem-weight", T("rg.sem.weight"), T("rg.sem.weight.help"), weight), field("rg-sem-cos", T("rg.sem.mincos"), T("rg.sem.mincos.help"), cos),
      field("rg-sem-rr", T("rg.sem.minrr"), T("rg.sem.minrr.help"), rr), field("rg-sem-idle", T("rg.sem.idle"), T("rg.sem.idle.help"), idle),
      el("p", "rg-help", T("rg.sem.python.help", e.python || "-")), field("rg-sem-dir", T("rg.sem.dir"), T("rg.sem.dir.help"), dir), go, test);
    return box;
  }
  function sideList() {
    const side = el("nav", "rg-side"), ul = el("ul", "rg-names");
    side.setAttribute("aria-label", T("rg.list.title"));
    list.forEach(a => {
      const li = el("li"), on = a.id === chosen && !creating;
      const b = btn("", "rg-pick" + (on ? " on" : ""), () => open(a.id));
      b.setAttribute("aria-current", String(on));
      b.append(el("span", "rg-pname", a.name), el("span", "rg-pcount", T("rg.count", a.documents)));
      li.append(b); ul.append(li);
    });
    side.append(el("h3", "rg-h2", T("rg.list.title")), list.length ? ul : el("p", "rg-help", T("rg.list.none")),
      btn(T("rg.new"), "primary rg-newbtn", () => { creating = true; chosen = ""; detail = null; results = null; say("", false); paint(); }));
    return side;
  }
  function createForm() {
    const form = el("form", "rg-form"), name = textInput("", S.limits.name), desc = el("textarea");
    desc.rows = 3; desc.maxLength = S.limits.description;
    form.noValidate = true;
    form.append(field("rg-new-name", T("rg.f.name"), "", name), field("rg-new-desc", T("rg.f.desc"), "", desc));
    const bar = el("div", "rg-bar"), go = btn(T("rg.create"), "primary", () => create(name.value, desc.value));
    go.type = "submit";
    bar.append(go, btn(T("rg.cancel"), "", () => { creating = false; say("", false); paint(); }));
    form.append(bar);
    form.onsubmit = e => { e.preventDefault(); create(name.value, desc.value); };
    return form;
  }
  function editForm(a) {
    const form = el("form", "rg-form"), name = textInput(a.name, S.limits.name), desc = el("textarea"), cloud = el("input");
    desc.rows = 3; desc.maxLength = S.limits.description; desc.value = a.description;
    cloud.type = "checkbox"; cloud.checked = !!a.cloud_ok;
    form.noValidate = true;
    const check = el("div", "rg-field rg-check"), lab = el("label", "");
    cloud.id = "rg-cloud";
    lab.htmlFor = "rg-cloud"; lab.append(cloud, el("span", "", T("rg.f.cloud")));
    check.append(lab, el("p", "rg-help", T("rg.f.cloud.help")));
    const others = el("select");
    OTHERS.forEach(v => { const o = el("option", "", T(OTHERS_WORDS[v])); o.value = v; o.selected = v === (a.others_ok || "ask_dialog"); others.append(o); });
    form.append(field("rg-name", T("rg.f.name"), "", name), field("rg-desc", T("rg.f.desc"), "", desc), check, field("rg-others", T("rg.f.others"), T("rg.f.others.help"), others));
    const bar = el("div", "rg-bar"), go = btn(T("rg.save"), "primary", () => {});
    go.type = "submit";
    bar.append(go);
    form.append(bar);
    form.onsubmit = e => { e.preventDefault(); save({ name: name.value, description: desc.value, cloud_ok: cloud.checked, others_ok: others.value }); };
    return form;
  }
  /* the figures of a document: a button, a grid of thumbnails, and a dialog with the whole picture and everything kept with it. Every text comes from the document: it enters as text. */
  const figs = {};                                                     // document id -> { open, total, list, busy }
  async function loadFigures(a, d, more) {
    const f = figs[d.id] = figs[d.id] || { open: false, total: 0, list: [], busy: false };
    f.busy = true; paint();
    const r = await api("GET", `api/rag/archives/${encodeURIComponent(a.id)}/documents/${encodeURIComponent(d.id)}/figures?offset=${more ? f.list.length : 0}`);
    f.busy = false;
    if (r.ok) { f.total = r.data.total; f.list = more ? f.list.concat(r.data.figures) : r.data.figures; } else say(why(r), true);
    paint();
  }
  async function showFigure(a, d, n) {
    const r = await api("GET", `api/rag/archives/${encodeURIComponent(a.id)}/documents/${encodeURIComponent(d.id)}/figures/${n}`);
    if (!r.ok) { say(why(r), true); paint(); return; }
    const g = r.data, dlg = el("dialog", "rg-figdlg"), body = el("div", "rg-figbody");
    dlg.setAttribute("aria-label", g.label || T("rg.fig.one"));
    const img = el("img", "rg-figimg"); img.src = g.image; img.alt = g.alt || g.caption || g.label || "";
    const info = el("div", "rg-figinfo");
    info.append(el("h4", "rg-h2", [g.label || T("rg.fig.one"), g.page ? T("rg.fig.page", g.page) : ""].filter(Boolean).join(" · ")));
    [["", g.caption], ["rg.fig.alt", g.alt], ["rg.fig.summary", g.summary], ["rg.fig.ocr", g.ocr]].forEach(([key, text]) => { if (text) { const p = el("p", "rg-dmeta"); p.append(key ? el("b", "", T(key) + " ") : "", text); info.append(p); } });
    if ((g.refs || []).length) { info.append(el("p", "rg-dmeta", T("rg.fig.refs"))); const ul = el("ul", "rg-figrefs"); g.refs.forEach(t => ul.append(el("li", "", t))); info.append(ul); }
    info.append(el("p", "rg-dmeta", T("rg.fig.from", g.document, g.section || "-", g.source || "pdf") + (g.url ? " · " + g.url : "")));
    const close = btn(T("rg.fig.close"), "primary", () => { dlg.close(); dlg.remove(); });
    dlg.onclose = () => dlg.remove();
    body.append(img, info); dlg.append(body, close);
    document.body.append(dlg); dlg.showModal();
  }
  function figuresBlock(a, d) {
    const f = figs[d.id], box = el("div", "rg-figs");
    if (!f || !f.open) return null;
    if (!f.list.length && f.busy) { box.append(el("p", "rg-help", T("rg.fig.loading"))); return box; }
    const grid = el("div", "rg-figgrid");
    f.list.forEach(g => {
      const b = el("button", "rg-figthumb"); b.type = "button";
      const im = el("img"); im.src = g.thumb; im.alt = ""; im.loading = "lazy";
      b.setAttribute("aria-label", [g.label || T("rg.fig.one"), g.caption].filter(Boolean).join(": "));
      b.append(im, el("span", "rg-figlabel", g.label || String(g.n)));
      b.onclick = () => showFigure(a, d, g.n);
      grid.append(b);
    });
    box.append(grid, el("p", "rg-dmeta", T("rg.fig.showing", f.list.length, f.total)));
    if (f.list.length < f.total) { const more = btn(T("rg.fig.more"), "", () => loadFigures(a, d, true)); more.disabled = f.busy; box.append(more); }
    return box;
  }
  function docsBlock(a) {
    const box = el("section", "rg-block"), ul = el("ul", "rg-docs");
    box.setAttribute("aria-labelledby", "rg-docs-h");
    const h = el("h4", "rg-h2", T("rg.docs.title")); h.id = "rg-docs-h";
    box.append(h);
    a.document_list.forEach(d => {
      const li = el("li", "rg-doc"), text = el("div", "rg-doc-t");
      text.append(el("span", "rg-dname", d.name), el("span", "rg-dmeta", T("rg.doc.meta", size(d.bytes), d.chunks)),
        el("span", "rg-dmeta", T(READ_BY[d.extraction] || READ_BY.basic, d.pages || 0) + (d.flagged ? " " + T("rg.doc.flagged", d.flagged) : "")));
      if (d.figures) text.append(el("span", "rg-dmeta", T("rg.doc.figures", d.figures) + ((d.coverage && (d.coverage.figure_pages || []).length) ? " " + T("rg.doc.figpages", d.coverage.figure_pages.join(", ")) : "")));
      if (window.ragSync) text.append(...window.ragSync.docLines(d));
      if ((d.skipped_pages || []).length) { const w = el("span", "rg-dmeta rg-warnline", T("rg.doc.skipped", d.skipped_pages.join(", "))); w.setAttribute("role", "status"); text.append(w); }
      const key = "d" + d.id, sure = armed === key;
      const b = btn(sure ? T("rg.doc.remove.sure") : T("rg.doc.remove"), sure ? "rg-warn" : "", () => { if (!sure) arm(key); else removeDoc(d); });
      b.id = "rg-rm-" + d.id;
      li.append(text, b);
      if (d.figures) {
        const fb = btn(T((figs[d.id] && figs[d.id].open) ? "rg.fig.hide" : "rg.fig.show", d.figures), "", () => {
          const f = figs[d.id] = figs[d.id] || { open: false, total: 0, list: [], busy: false };
          f.open = !f.open;
          if (f.open && !f.list.length) loadFigures(a, d, false); else paint();
        });
        fb.id = "rg-fig-" + d.id; fb.setAttribute("aria-expanded", String(!!(figs[d.id] && figs[d.id].open)));
        li.append(fb);
        const grid = figuresBlock(a, d);
        if (grid) li.append(grid);
      }
      ul.append(li);
    });
    box.append(a.document_list.length ? ul : el("p", "rg-help", T("rg.docs.none")));
    const pick = el("input"); pick.type = "file"; pick.multiple = true; pick.accept = S.formats.join(","); pick.disabled = working;
    pick.onchange = () => addFiles([...pick.files]);
    const path = textInput("", 500); path.inputMode = "url";
    const addp = btn(T("rg.add.path.btn"), "", () => addPath(path.value)); addp.disabled = working;
    const row = el("div", "rg-row");
    row.append(path, addp);
    box.append(field("rg-file", T("rg.add.file"), T("rg.add.help", S.formats.join(" "), Math.round(S.limits.document_bytes / (1 << 20))), pick), field("rg-path", T("rg.add.path"), "", row, path));
    path.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); addPath(path.value); } };
    return box;
  }
  function indexBlock(a) {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-index-h");
    const h = el("h4", "rg-h2", T("rg.index.title")); h.id = "rg-index-h";
    box.append(h, el("p", "rg-line", T("rg.index.line", a.chunks, a.documents)),
      el("p", "rg-line", a.mode === "hybrid" ? T("rg.index.hybrid", a.embed_model) : T("rg.index.text")));
    if (a.reindex) box.append(el("p", "rg-help", T("rg.index.hint")));
    const go = btn(T("rg.index.again"), "", reindex); go.disabled = working || !a.chunks;
    box.append(go, el("p", "rg-help", T("rg.index.again.help")));
    return box;
  }
  function tryBlock(a) {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-try-h");
    const h = el("h4", "rg-h2", T("rg.try.title")); h.id = "rg-try-h";
    const form = el("form", "rg-row"), q = textInput(question, 500);
    q.placeholder = T("rg.try.ph"); q.setAttribute("aria-label", T("rg.try.title")); q.id = "rg-q";
    q.oninput = () => { question = q.value; };
    const go = btn(T("rg.try.go"), "primary", ask); go.type = "submit"; go.disabled = asking || !a.chunks;
    form.append(q, go);
    form.onsubmit = e => { e.preventDefault(); ask(); };
    box.append(h, form);
    if (asking) box.append(el("p", "rg-help", T("rg.try.working")));
    if (results) {
      box.append(el("p", "rg-help", (results.reranked ? T("rg.try.mode.reranked") : results.mode === "hybrid" ? T("rg.try.mode.hybrid") : T("rg.try.mode.lexical")) + (results.note ? " " + results.note : "")));
      if (!results.hits.length) box.append(el("p", "rg-line", T("rg.try.none")));
      const ul = el("ol", "rg-hits");
      results.hits.forEach(hit => {
        const li = el("li", "rg-hit"), head = el("div", "rg-hit-h");
        head.append(el("strong", "", T("rg.try.from", hit.archive, hit.document)),
          el("span", "rg-tag", hit.mode === "hybrid" ? T("rg.tag.hybrid") : T("rg.tag.lexical")), el("span", "rg-tag", T("rg.try.score", hit.score)),
          ...(hit.rerank != null ? [el("span", "rg-tag", T("rg.tag.rerank", hit.rerank))] : []));
        li.append(head, el("p", "rg-hit-text", hit.text)); ul.append(li);
      });
      box.append(ul);
    }
    return box;
  }
  function detailView() {
    const a = detail.archive, main = el("div", "rg-main");
    main.append(el("h3", "rg-h", a.name), editForm(a), docsBlock(a), ...(window.ragSync ? [window.ragSync.block(a, { reload: reloadAll })] : []), indexBlock(a), tryBlock(a));
    const sure = armed === "delete", bar = el("div", "rg-bar");
    const del = btn(sure ? T("rg.delete.sure") : T("rg.delete"), "rg-warn", () => { if (!sure) arm("delete"); else remove(); });
    del.id = "rg-delete";
    bar.append(del);
    main.append(bar);
    return main;
  }
  function paint() {
    if (!S) { host.replaceChildren(el("p", "rg-help", note || T("rg.loading"))); return; }
    const focus = document.activeElement && document.activeElement.id;
    const status = el("p", "rg-note", note); status.setAttribute("role", "status"); status.dataset.bad = noteBad ? "1" : ""; status.hidden = !note;
    let main;
    if (creating) { main = el("div", "rg-main"); main.append(el("h3", "rg-h", T("rg.new")), createForm()); }
    else if (detail) main = detailView();
    else { main = el("div", "rg-main"); main.append(el("p", "rg-help", T("rg.pick"))); }
    const layout = el("div", "rg-layout");
    layout.append(sideList(), main);
    const stop = jobNow ? btn(T("rg.job.cancel"), "", cancelJob) : null;
    host.replaceChildren(el("p", "rg-intro", T("rg.intro")), projectPicker(), banner(), ...[semanticBlock(), settingsBlock(), window.RagDatabase && project ? window.RagDatabase.block(project, paint, reloadAll) : null].filter(Boolean), status, ...(stop ? [stop] : []), layout);
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  /* what /rag lists: every archive with its project's name, and the key of the Chat's own folder */
  async function chatProject() {
    if (!chatKey) {
      const p = await api("GET", "api/rag/projects");
      const chat = p.ok && (p.data.projects || []).find(x => x.chat);
      if (chat) chatKey = chat.key;
    }
    return chatKey;
  }
  async function catalog() {
    const r = await api("GET", "api/rag/catalog");
    return r.ok ? { archives: r.data.archives || [], project: await chatProject() } : null;
  }
  window.ragUi = { catalog };

  document.addEventListener("tadamoo:view", e => { if (e.detail === "rag") refresh(); });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
  if (knowledgeOn()) refresh(); else paint();
})();
