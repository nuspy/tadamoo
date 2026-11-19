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
  let wizardShown = false;

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
    checkWizard();
  }

  /* first-run wizard: the embeddings/Docling environment, offered once, the first time this tab opens and it is not already found on this PC. */
  async function checkWizard() {
    if (wizardShown) return;
    wizardShown = true;
    const r = await api("GET", "api/rag/setup/pending");
    if (r.ok && (r.data.pending || []).includes("embed_env")) showWizard();
  }
  function showWizard() {
    const dlg = el("dialog", "rg-dlg");
    const status = el("p", "rg-note", ""); status.hidden = true;
    let answered = false;
    const answer = async choice => { if (answered) return; answered = true; await api("POST", "api/rag/setup/answer", { step: "embed_env", choice }); };
    const skip = btn(T("rg.wizard.skip"), "", () => { answer("skipped"); dlg.close(); });
    const install = btn(T("rg.wizard.install"), "primary", async () => {
      skip.disabled = install.disabled = true; status.hidden = false; status.textContent = T("ri.running");
      const r = await api("POST", "api/rag/embedding/env/install", {});
      if (!r.ok) { status.textContent = why(r); skip.disabled = install.disabled = false; return; }
      let job = r.data.job;
      while (job.state === "running") {
        status.textContent = job.stage || T("ri.running");
        await new Promise(res => setTimeout(res, 1000));
        const p = await api("GET", `api/rag/jobs/${encodeURIComponent(job.id)}`);
        if (!p.ok) break;
        job = p.data.job;
      }
      await answer(job.state === "done" && job.result && job.result.ok ? "installed" : "skipped");
      dlg.close();
      reloadAll();
    });
    dlg.append(el("h3", "rg-h", T("rg.wizard.embed.title")), el("p", "rg-help", T("rg.wizard.embed.help")), status, install, skip);
    dlg.addEventListener("close", () => { answer("skipped"); dlg.remove(); });     // Escape or any other dismissal still counts as an answer: it must not nag again
    document.body.append(dlg);
    dlg.showModal();
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
    if (chosen && window.ragSync && window.ragSync.invalidate) window.ragSync.invalidate(chosen);              // the tree is drawn again from what the archive holds now
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
  /* what is dropped on the page: files, and folders walked entry by entry (a browser gives no path, so a dropped file is copied into the archive and is not followed) */
  async function droppedFiles(dt) {
    const out = [], seen = new Set();
    const keep = (f, name) => { const n = name.replace(/[\\/]+/g, " - "); if (!seen.has(n)) { seen.add(n); out.push(new File([f], n, { type: f.type })); } };
    const walk = async (entry, prefix) => {
      if (entry.isFile) keep(await new Promise((ok, no) => entry.file(ok, no)), prefix + entry.name);
      else if (entry.isDirectory) {
        const reader = entry.createReader();
        for (;;) {
          const batch = await new Promise((ok, no) => reader.readEntries(ok, no));
          if (!batch.length) break;
          for (const e of batch) await walk(e, prefix + entry.name + "/");
        }
      }
    };
    const entries = [...(dt.items || [])].map(i => (i.webkitGetAsEntry ? i.webkitGetAsEntry() : null)).filter(Boolean);
    if (entries.length) for (const e of entries) await walk(e, "");
    else [...(dt.files || [])].forEach(f => keep(f, f.name));
    const readable = out.filter(f => S.formats.includes("." + (f.name.split(".").pop() || "").toLowerCase()));
    return { files: readable, skipped: out.length - readable.length };
  }
  function dropZone(pick) {
    const zone = el("div", "rg-drop");
    zone.id = "rg-drop"; zone.tabIndex = 0; zone.setAttribute("role", "button"); zone.setAttribute("aria-label", T("rg.drop"));
    zone.append(el("p", "rg-line", T("rg.drop")), el("p", "rg-help", T("rg.drop.help")));
    zone.onclick = () => pick.click();
    zone.onkeydown = e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pick.click(); } };
    zone.ondragover = e => { e.preventDefault(); zone.classList.add("over"); };
    zone.ondragleave = () => zone.classList.remove("over");
    zone.ondrop = async e => {
      e.preventDefault(); zone.classList.remove("over");
      const got = await droppedFiles(e.dataTransfer);
      if (got.skipped) say(T("rg.drop.skipped", got.skipped), true);
      await addFiles(got.files);
    };
    return zone;
  }

  /* a look at this PC's folders, from the server, so that a file or a folder is chosen with its real path and can be followed */
  const browse = { path: "", entries: [], parent: null, chosen: new Set(), open: false };
  async function browseTo(path) {
    const r = await api("GET", `api/rag/browse?path=${encodeURIComponent(path)}`);
    if (!r.ok) { say(why(r), true); return false; }
    browse.path = r.data.path; browse.parent = r.data.parent; browse.entries = r.data.entries; browse.chosen = new Set();
    return true;
  }
  async function browseDialog() {
    if (!(await browseTo(browse.path))) await browseTo("");
    const dlg = el("dialog", "rg-dlg rg-browse"), body = el("div", "rg-browsebody"), bar = el("div", "rg-bar");
    dlg.setAttribute("aria-label", T("rg.browse.title"));
    const draw = () => {
      const where = textInput(browse.path, 500); where.id = "rg-br-path"; where.setAttribute("aria-label", T("rg.browse.path"));
      const go = btn(T("rg.browse.go"), "", async () => { if (await browseTo(where.value.trim())) draw(); });
      where.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
      const row = el("div", "rg-row"); row.append(where, go);
      if (browse.parent !== null && browse.path) row.append(btn(T("rg.browse.up"), "", async () => { if (await browseTo(browse.parent)) draw(); }));
      const ul = el("ul", "rg-browselist");
      browse.entries.forEach(e => {
        const li = el("li");
        if (e.dir) { const b = btn("", "rg-pick", async () => { if (await browseTo(e.path)) draw(); }); b.append(el("span", "rg-pname", "▸ " + e.name)); li.append(b); }
        else {
          const lab = el("label", "rg-pick rg-file"), c = el("input"); c.type = "checkbox"; c.checked = browse.chosen.has(e.path); c.id = "rg-br-f-" + e.name;
          c.onchange = () => { if (c.checked) browse.chosen.add(e.path); else browse.chosen.delete(e.path); add.disabled = !browse.chosen.size; };
          lab.append(c, el("span", "rg-pname", e.name), el("span", "rg-pcount", size(e.size)));
          li.append(lab);
        }
        ul.append(li);
      });
      const add = btn(T("rg.browse.addfiles"), "primary", async () => {
        const paths = [...browse.chosen]; dlg.close();
        for (const p of paths) await addPath(p);
      });
      add.disabled = !browse.chosen.size; add.id = "rg-br-add";
      const follow = btn(T("rg.browse.follow"), "", async () => {
        dlg.close();
        const r = await api("POST", `api/rag/archives/${encodeURIComponent(chosen)}/sources`, { kind: "folder", path: browse.path });
        say(r.ok ? T("rs.source.added") : why(r), !r.ok); await reloadAll();
      });
      follow.disabled = !browse.path; follow.id = "rg-br-follow";
      bar.replaceChildren(add, follow, btn(T("rg.browse.close"), "", () => dlg.close()));
      body.replaceChildren(el("p", "rg-help", T("rg.browse.help")), row, ul.children.length ? ul : el("p", "rg-help", T("rg.browse.none")));
    };
    draw();
    dlg.append(el("h4", "rg-h2", T("rg.browse.title")), body, bar);
    dlg.onclose = () => dlg.remove();
    document.body.append(dlg); dlg.showModal();
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
  /* which version of the archive "try a question" looks at: the latest (default) or one picked from its own history, kept per archive between sessions */
  function versionKey(aid) { return "rag-search-version-" + aid; }
  function loadSearchVersion(aid) {
    try { return localStorage.getItem(versionKey(aid)) || ""; } catch (e) { return ""; }
  }
  function saveSearchVersion(aid, v) {
    try { if (v) localStorage.setItem(versionKey(aid), v); else localStorage.removeItem(versionKey(aid)); } catch (e) { /* a private window, or storage blocked: the choice just does not survive a reload */ }
  }
  async function ask() {
    if (!question.trim() || asking) return;
    asking = true; results = null; paint();
    const v = chosen ? loadSearchVersion(chosen) : "";
    const body = { query: question.trim(), archives: [chosen], top_k: TOP_K };
    if (v) body.version = Number(v);
    const r = await api("POST", "api/rag/search", body);
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
  /* the projects: a list in the left column, each with an icon to rename and one to delete (the Chat's own project has neither); a new one is made from a folder of this PC. Deleting
     asks twice: first what will go, then the name of the project typed by hand. The folder and its files are never touched. */
  const ICONS = { edit: "M3 17.25V21h3.75L17.8 9.94l-3.75-3.75L3 17.25zM20.7 7.04a1 1 0 0 0 0-1.41l-2.34-2.34a1 1 0 0 0-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z",
                  trash: "M6 19a2 2 0 0 0 2 2h8a2 2 0 0 0 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z" };
  function icon(name) {
    const ns = "http://www.w3.org/2000/svg", svg = document.createElementNS(ns, "svg"), p = document.createElementNS(ns, "path");
    svg.setAttribute("viewBox", "0 0 24 24"); svg.setAttribute("width", "20"); svg.setAttribute("height", "20"); svg.setAttribute("aria-hidden", "true");
    p.setAttribute("d", ICONS[name]); p.setAttribute("fill", "currentColor"); svg.append(p);
    return svg;
  }
  function selectProject(key) {
    project = key; picked = true; chosen = ""; creating = false; detail = null; results = null; question = ""; armed = ""; say("", false);
    refresh();
  }
  function dialog(title, body, actions) {
    const dlg = el("dialog", "rg-dlg"), bar = el("div", "rg-bar");
    dlg.setAttribute("aria-label", title);
    actions.forEach(a => bar.append(a));
    dlg.append(el("h4", "rg-h2", title), ...body, bar);
    dlg.onclose = () => dlg.remove();
    document.body.append(dlg); dlg.showModal();
    return dlg;
  }
  async function changeProjects(path, body, doneText) {
    const r = await api("POST", `api/rag/projects${path}`, body);
    if (!r.ok) { say(why(r), true); paint(); return false; }
    projects = r.data.projects || projects;
    if (r.data.created) project = r.data.created;
    if (r.data.deleted_archives != null) { project = (projects.find(p => p.chat) || projects[0] || {}).key || ""; chosen = ""; detail = null; }
    say(T(doneText, r.data.deleted_archives || 0), false);
    await refresh();
    return true;
  }
  function newProjectDialog() {
    const path = textInput("", 500), name = textInput("", 80), err = el("p", "rg-note"); err.setAttribute("role", "status");
    path.inputMode = "url";
    const go = btn(T("rg.proj.create"), "primary", async () => { if (await changeProjects("", { path: path.value.trim(), name: name.value.trim() }, "rg.proj.created")) dlg.close(); else dlg.close(); });
    go.disabled = true; path.oninput = () => { go.disabled = !path.value.trim(); };
    const dlg = dialog(T("rg.proj.new"), [field("rg-pj-path", T("rg.proj.path"), T("rg.proj.path.help"), path), field("rg-pj-name", T("rg.proj.name"), "", name), err], [go, btn(T("rg.cancel"), "", () => dlg.close())]);
  }
  function renameDialog(p) {
    const name = textInput(p.name, 80), go = btn(T("rg.proj.save"), "primary", async () => { await changeProjects("/rename", { key: p.key, name: name.value }, "rg.proj.renamed"); dlg.close(); });
    const dlg = dialog(T("rg.proj.edit"), [field("rg-pj-rename", T("rg.proj.name"), "", name)], [go, btn(T("rg.cancel"), "", () => dlg.close())]);
  }
  async function deleteDialog(p) {
    const r = await api("GET", `api/rag?project=${encodeURIComponent(p.key)}`);
    const docs = r.ok ? (r.data.archives || []).reduce((n, a) => n + a.documents, 0) : 0;
    const next = btn(T("rg.proj.continue"), "rg-warn", () => {
      dlg.close();
      const typed = textInput("", 80), go = btn(T("rg.proj.delete.go"), "rg-warn", async () => { await changeProjects("/delete", { key: p.key, name: typed.value, confirm: true }, "rg.proj.deleted"); dlg2.close(); });
      go.disabled = true; typed.oninput = () => { go.disabled = typed.value.trim() !== String(p.name).trim(); };
      const dlg2 = dialog(T("rg.proj.delete.title", p.name), [el("p", "rg-help", T("rg.proj.delete.2", p.name)), field("rg-pj-typed", T("rg.proj.name"), "", typed)], [go, btn(T("rg.cancel"), "", () => dlg2.close())]);
    });
    const dlg = dialog(T("rg.proj.delete.title", p.name), [el("p", "rg-line", T("rg.proj.delete.1", p.name, p.archives, docs))], [next, btn(T("rg.cancel"), "", () => dlg.close())]);
  }
  function projectsList() {
    const side = el("nav", "rg-side rg-projects rg-box"), ul = el("ul", "rg-names");
    side.setAttribute("aria-label", T("rg.proj.title"));
    projects.forEach(p => {
      const li = el("li", "rg-prow"), on = p.key === project;
      const pick = btn("", "rg-pick" + (on ? " on" : ""), () => selectProject(p.key));
      pick.setAttribute("aria-current", String(on)); pick.title = p.path;
      pick.append(el("span", "rg-pname", p.chat ? T("rg.proj.chat") : p.name), el("span", "rg-pcount", T("rg.count", p.archives)));
      li.append(pick);
      if (!p.chat) {
        const ed = btn("", "rg-iconbtn", () => renameDialog(p)), del = btn("", "rg-iconbtn rg-warn", () => deleteDialog(p));
        ed.append(icon("edit")); ed.setAttribute("aria-label", T("rg.proj.edit") + ": " + p.name); ed.title = T("rg.proj.edit");
        del.append(icon("trash")); del.setAttribute("aria-label", T("rg.proj.delete") + ": " + p.name); del.title = T("rg.proj.delete");
        li.append(ed, del);
      }
      ul.append(li);
    });
    side.append(el("h3", "rg-h2", T("rg.proj.title")), projects.length ? ul : el("p", "rg-help", T("rg.proj.none")), btn(T("rg.proj.new"), "rg-newproj", newProjectDialog));
    return side;
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
  function sideList() {
    const side = el("nav", "rg-side rg-box"), ul = el("ul", "rg-names");
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
  /* the original file of a document: "Open" shows it in a tab when the browser can (a PDF, an image, plain text; a page is shown as text, never run), else saves it; "Save as" always
     saves a copy. The kept original is read-only on the server: what comes here is a copy. */
  const VIEW_AS = { pdf: "application/pdf", png: "image/png", jpg: "image/jpeg", jpeg: "image/jpeg", gif: "image/gif", webp: "image/webp", txt: "text/plain", md: "text/plain", markdown: "text/plain", html: "text/plain", htm: "text/plain" };
  async function getOriginal(a, d, save) {
    try {
      const r = await fetch(`api/rag/archives/${encodeURIComponent(a.id)}/documents/${encodeURIComponent(d.id)}/original`, { cache: "no-store", headers: authHeaders() });
      if (!r.ok) {
        let m = ""; try { m = ((await r.json()).error || {}).message || ""; } catch (e) { /* no body */ }
        say(m || `HTTP ${r.status}`, true); paint(); return;
      }
      const blob = await r.blob(), type = VIEW_AS[(d.name.split(".").pop() || "").toLowerCase()];
      const href = URL.createObjectURL(type && !save ? new Blob([blob], { type }) : blob);
      if (type && !save) window.open(href, "_blank", "noopener");
      else { const link = el("a"); link.href = href; link.download = d.name; document.body.append(link); link.click(); link.remove(); }
      setTimeout(() => URL.revokeObjectURL(href), 60000);
    } catch (e) { say(T("rg.e.offline"), true); paint(); }
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
    const docRow = d => {
      const li = el("div", "rg-doc"), text = el("div", "rg-doc-t");
      text.append(el("span", "rg-dname", d.name), el("span", "rg-dmeta", T("rg.doc.meta", size(d.bytes), d.chunks)),
        el("span", "rg-dmeta", T(READ_BY[d.extraction] || READ_BY.basic, d.pages || 0) + (d.flagged ? " " + T("rg.doc.flagged", d.flagged) : "")));
      if (d.figures) text.append(el("span", "rg-dmeta", T("rg.doc.figures", d.figures) + ((d.coverage && (d.coverage.figure_pages || []).length) ? " " + T("rg.doc.figpages", d.coverage.figure_pages.join(", ")) : "")));
      if (window.ragSync) text.append(...window.ragSync.docLines(d));
      if ((d.skipped_pages || []).length) { const w = el("span", "rg-dmeta rg-warnline", T("rg.doc.skipped", d.skipped_pages.join(", "))); w.setAttribute("role", "status"); text.append(w); }
      const key = "d" + d.id, sure = armed === key;
      const b = btn(sure ? T("rg.doc.remove.sure") : T("rg.doc.remove"), sure ? "rg-warn" : "", () => { if (!sure) arm(key); else removeDoc(d); });
      b.id = "rg-rm-" + d.id;
      const open = btn(T("rg.doc.open"), "", () => getOriginal(a, d, false)), save = btn(T("rg.doc.save"), "", () => getOriginal(a, d, true));
      open.id = "rg-open-" + d.id; save.id = "rg-save-" + d.id;
      [open, save].forEach(x => { x.disabled = !d.has_original; if (!d.has_original) x.title = T("rg.doc.noorig"); });
      li.append(text, open, save, b);
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
      return li;
    };
    const byId = Object.fromEntries(a.document_list.map(d => [d.id, d]));
    const treeEl = window.ragSync && window.ragSync.tree ? window.ragSync.tree(a, id => (byId[id] ? docRow(byId[id]) : null)) : null;
    a.document_list.forEach(d => { const li = el("li"); li.append(docRow(d)); ul.append(li); });
    box.append(!a.document_list.length ? el("p", "rg-help", T("rg.docs.none")) : (treeEl || ul));
    const pick = el("input"); pick.type = "file"; pick.multiple = true; pick.accept = S.formats.join(","); pick.disabled = working;
    pick.onchange = () => addFiles([...pick.files]);
    const path = textInput("", 500); path.inputMode = "url";
    const addp = btn(T("rg.add.path.btn"), "", () => addPath(path.value)); addp.disabled = working;
    const br = btn(T("rg.browse"), "", browseDialog); br.id = "rg-browse"; br.disabled = working;
    const row = el("div", "rg-row");
    row.append(path, addp, br);
    box.append(el("p", "rg-help", T("rg.add.vs")), dropZone(pick), field("rg-file", T("rg.add.file"), T("rg.add.help", S.formats.join(" "), Math.round(S.limits.document_bytes / (1 << 20))), pick), field("rg-path", T("rg.add.path"), "", row, path));
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
  function versionPicker(a) {
    const log = a.version_log || [];
    if (!log.length) return null;
    const wrap = el("div", "rg-field"), sel = el("select"), lab = el("label", "", T("rg.try.version")); sel.id = "rg-try-version"; lab.htmlFor = "rg-try-version";
    const latest = el("option", "", T("rg.try.version.latest")); latest.value = ""; sel.append(latest);
    log.slice().reverse().forEach(e => { const o = el("option", "", T("rg.try.version.opt", e.n, e.documents)); o.value = String(e.n); sel.append(o); });
    sel.value = loadSearchVersion(a.id);
    sel.onchange = () => { saveSearchVersion(a.id, sel.value); results = null; paint(); };
    wrap.append(lab, sel, el("p", "rg-help", T("rg.try.version.help")));
    return wrap;
  }
  function tryBlock(a) {
    const box = el("section", "rg-block");
    box.setAttribute("aria-labelledby", "rg-try-h");
    const h = el("h4", "rg-h2", T("rg.try.title")); h.id = "rg-try-h";
    const picker = versionPicker(a);
    const form = el("form", "rg-row"), q = textInput(question, 500);
    q.placeholder = T("rg.try.ph"); q.setAttribute("aria-label", T("rg.try.title")); q.id = "rg-q";
    q.oninput = () => { question = q.value; };
    const go = btn(T("rg.try.go"), "primary", ask); go.type = "submit"; go.disabled = asking || !a.chunks;
    form.append(q, go);
    form.onsubmit = e => { e.preventDefault(); ask(); };
    box.append(h);
    if (picker) box.append(picker);
    box.append(form);
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
        li.append(head, el("p", "rg-hit-text", hit.text));
        if ((hit.figures || []).length) {                                            // the figures that complete this passage
          const row = el("div", "rg-hitfigs");
          hit.figures.forEach(g => {
            const b = btn([g.label || T("rg.fig.one"), g.caption].filter(Boolean).join(": ").slice(0, 80), "", () => showFigure({ id: hit.archive_id }, { id: hit.doc_id }, g.fig));
            b.classList.add("rg-hitfig");
            row.append(b);
          });
          li.append(row);
        }
        ul.append(li);
      });
      box.append(ul);
    }
    return box;
  }
  function detailView() {
    const a = detail.archive, main = el("div", "rg-main");
    const box = x => { const w = el("section", "rg-box"); w.append(x); return w; };
    main.append(el("h3", "rg-h", a.name), ...[editForm(a), docsBlock(a), ...(window.ragDiscover ? [window.ragDiscover.block(a, { reload: reloadAll })] : []), ...(window.ragSync ? [window.ragSync.block(a, { reload: reloadAll, paint })] : []), indexBlock(a), tryBlock(a)].map(box));
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
    const layout = el("div", "rg-layout"), left = el("div", "rg-left"), right = el("div", "rg-right");
    const box = x => { const w = el("section", "rg-box"); w.append(x); return w; };                      // each group of functions in a container of its own
    left.append(projectsList(), sideList());
    right.append(main, ...[banner(), window.RagDatabase && project ? window.RagDatabase.block(project, paint, reloadAll) : null].filter(Boolean).map(box));
    layout.append(left, right);
    const stop = jobNow ? btn(T("rg.job.cancel"), "", cancelJob) : null;
    host.replaceChildren(el("p", "rg-intro", T("rg.intro")), status, ...(stop ? [stop] : []), layout);
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
