"use strict";
/* Knowledge > Database: the database that holds a project's archives (a SQLite file of its own by default, or PostgreSQL, MariaDB or MySQL, or another one). The kind and its
   fields, "Connect and prepare" (three steps, each with its result), the choice to make when PostgreSQL has no vector extension, the import of archives in the old file format,
   and the Python packages the databases need, each installed only by its own button. The password is sent once and goes to the vault; the page never gets it back.
   Everything the server says is put in the page as text. */
(() => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rd.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  const KINDS = ["sqlite", "postgres", "mysql", "other"];
  let st = null, host = null, repaint = () => {}, changed = () => {};
  const fresh = project => ({ project, view: null, cfg: { kind: "sqlite" }, password: "", superuser: "", superpassword: "", steps: [], stage: "", running: false, planB: "blob", vecFile: "", open: false, note: "", bad: false,
    comps: [], log: [], compBusy: "", importing: false, importResult: null, loaded: false });

  const say = (text, bad) => { st.note = text; st.bad = !!bad; repaint(); };

  async function load() {
    const p = st.project;
    const [v, c] = await Promise.all([api("GET", `api/rag/db?project=${encodeURIComponent(p)}`), api("GET", "api/rag/db/components")]);
    if (!st || st.project !== p) return;
    if (v.ok) { st.view = v.data; if (!st.loaded) st.cfg = { ...v.data.config }; st.loaded = true; } else say(why(v), true);
    if (c.ok) st.comps = c.data.components || [];
    repaint();
  }

  /* a job on the server: ask how it goes until it ends, handing each look to `tick` */
  async function follow(job, tick) {
    for (;;) {
      tick(job);
      repaint();
      if (job.state !== "running") return job;
      await new Promise(r => setTimeout(r, 700));
      const r = await api("GET", `api/rag/jobs/${encodeURIComponent(job.id)}`);
      if (!r.ok) return { ...job, state: "failed", error: why(r) };
      job = r.data.job;
    }
  }

  async function prepare(extra) {
    if (st.running) return;
    st.running = true; st.steps = []; st.note = ""; repaint();
    const body = { project: st.project, config: cleanConfig(), ...(st.password ? { password: st.password } : {}),
      ...(st.cfg.kind === "postgres" && st.superuser.trim() ? { superuser: st.superuser.trim(), superpassword: st.superpassword } : {}), ...(extra || {}) };
    const r = await api("POST", "api/rag/db/prepare", body);
    if (!r.ok) { st.running = false; say(why(r), true); return; }
    const done = await follow(r.data.job, j => { st.stage = j.stage; if (j.data && j.data.steps) st.steps = j.data.steps; });
    st.running = false; st.superpassword = "";
    if (done.state === "done" && done.result) {
      st.steps = done.result.steps;
      if (done.result.ok) { st.password = ""; st.note = T("rd.prepare.ok"); st.bad = false; await load(); changed(); }
      else say(T("rd.prepare.stopped", T("rd.step." + done.result.stopped_at)), true);
    } else say(done.error || T("rd.prepare.failed"), true);
    repaint();
  }

  /* the Test button: is the database installed and set up? The server checks, in order, the package, the connection, the tables, the archives and a small write that is removed again;
     it stops at the first step that fails and says why. It never keeps the password the form holds. */
  async function runTest() {
    if (st.testing || st.running) return;
    st.testing = true; st.test = null; repaint();
    const r = await api("POST", "api/rag/db/test", { project: st.project, config: cleanConfig(), ...(st.password ? { password: st.password } : {}) });
    st.testing = false;
    if (r.ok) st.test = r.data; else say(why(r), true);
    repaint();
  }
  function testBlock() {
    if (!st.test) return null;
    const ol = el("ol", "rd-steps rd-test");
    ol.setAttribute("aria-label", T("rd.test.title"));
    st.test.steps.forEach(s => {
      const li = el("li", "rd-step " + (s.ok ? "ok" : "bad"));
      li.append(el("b", "", T("rd.test.step." + s.step)), " ", el("span", "rd-mark", s.ok ? T("rd.ok") : T("rd.failed")), el("p", "rg-help", s.message));
      ol.append(li);
    });
    const box = el("div", "rd-testbox");
    box.setAttribute("role", "status");
    box.append(el("p", "rg-line", T(st.test.ok ? "rd.test.ok" : "rd.test.bad")), ol);
    return box;
  }

  function cleanConfig() {
    const c = { kind: st.cfg.kind };
    ["path", "host", "port", "dbname", "user", "sslmode", "prefix", "driver", "dsn", "dialect", "vault"].forEach(k => { if (st.cfg[k] !== undefined && String(st.cfg[k]).trim() !== "") c[k] = typeof st.cfg[k] === "number" ? st.cfg[k] : String(st.cfg[k]).trim(); });
    if (c.port !== undefined) c.port = Number(c.port) || undefined;
    return c;
  }

  async function reset() {
    const r = await api("POST", "api/rag/db/config", { project: st.project, reset: true });
    if (r.ok) { st.loaded = false; st.steps = []; st.view = r.data; st.cfg = { ...r.data.config }; st.loaded = true; say(T("rd.reset.done"), false); changed(); } else say(why(r), true);
  }

  async function importAll() {
    const ids = (st.view.waiting_here || []);
    if (!ids.length || st.importing) return;
    st.importing = true; st.importResult = null; repaint();
    const r = await api("POST", "api/rag/db/import", { archives: ids });
    if (!r.ok) { st.importing = false; say(why(r), true); return; }
    const done = await follow(r.data.job, j => { st.stage = j.stage; st.progress = j.pages ? T("rd.import.progress", j.page, j.pages) : ""; });
    st.importing = false;
    if (done.state === "done") { st.importResult = done.result; st.note = T("rd.import.finished", done.result.imported.length); st.bad = done.result.failed.length > 0; await load(); changed(); } else say(done.error || T("rd.import.failed"), true);
    repaint();
  }

  async function installComponent(c) {
    if (st.compBusy) return;
    st.compBusy = c.id; st.log = []; repaint();
    const r = await api("POST", "api/rag/db/components/install", { id: c.id });
    if (!r.ok) { st.compBusy = ""; say(why(r), true); return; }
    const done = await follow(r.data.job, j => { if (j.data && j.data.log) st.log = j.data.log; });
    st.compBusy = "";
    if (done.state === "done") { say(T("rd.comp.done", c.id, (done.result || {}).version || ""), false); await load(); } else say(T("rd.comp.failed", c.id, done.error || ""), true);
  }

  /* ---- the page ---- */
  function field(id, label, help, control) {
    const wrap = el("div", "rg-field"), lab = el("label", "", label);
    lab.htmlFor = id; control.id = id;
    wrap.append(lab, control);
    if (help) wrap.append(el("p", "rg-help", help));
    return wrap;
  }
  function input(key, type, max) {
    const i = el("input"); i.type = type || "text"; i.value = st.cfg[key] === undefined ? "" : String(st.cfg[key]); i.maxLength = max || 500; i.autocomplete = "off"; i.spellcheck = false;
    i.oninput = () => { st.cfg[key] = i.value; };
    return i;
  }
  function kindFields(box) {
    const k = st.cfg.kind;
    if (k === "sqlite") {
      const p = input("path"); p.placeholder = (st.view && st.view.default_path) || "";
      box.append(field("rd-path", T("rd.f.path"), T("rd.f.path.help"), p));
    } else if (k === "postgres" || k === "mysql") {
      const port = input("port", "text", 6); port.inputMode = "numeric"; port.placeholder = k === "postgres" ? "5432" : "3306";
      const pw = el("input"); pw.type = "password"; pw.autocomplete = "new-password"; pw.value = st.password; pw.oninput = () => { st.password = pw.value; };
      const ssl = el("select"); ["", "disable", "prefer", "require", "verify-full"].forEach(m => { const o = el("option", "", m || T("rd.f.ssl.default")); o.value = m; ssl.append(o); });
      ssl.value = st.cfg.sslmode || ""; ssl.onchange = () => { st.cfg.sslmode = ssl.value; };
      box.append(field("rd-host", T("rd.f.host"), "", Object.assign(input("host"), { placeholder: "localhost" })), field("rd-port", T("rd.f.port"), "", port),
        field("rd-db", T("rd.f.dbname"), T("rd.f.dbname.help"), input("dbname", "text", 63)), field("rd-user", T("rd.f.user"), "", input("user", "text", 63)),
        field("rd-pw", T("rd.f.password"), st.view && st.view.has_password ? T("rd.f.password.kept", "●".repeat(Math.min(st.view.password_chars || 0, 24)), st.view.password_chars || 0) : T("rd.f.password.help"), pw));
      if (k === "postgres") {
        const su = el("input"); su.type = "text"; su.autocomplete = "off"; su.spellcheck = false; su.maxLength = 63; su.value = st.superuser; su.oninput = () => { st.superuser = su.value; };
        const sp = el("input"); sp.type = "password"; sp.autocomplete = "off"; sp.value = st.superpassword; sp.oninput = () => { st.superpassword = sp.value; };
        box.append(field("rd-ssl", T("rd.f.ssl"), "", ssl), el("p", "rg-help", T("rd.super.help")),
          field("rd-su", T("rd.f.superuser"), "", su), field("rd-sp", T("rd.f.superpassword"), T("rd.f.superpassword.help"), sp));
      }
    } else {
      const dia = el("select"); ["postgres", "mysql", "sqlite"].forEach(d => { const o = el("option", "", d); o.value = d; dia.append(o); });
      dia.value = st.cfg.dialect || "postgres"; dia.onchange = () => { st.cfg.dialect = dia.value; };
      const pw = el("input"); pw.type = "password"; pw.autocomplete = "new-password"; pw.value = st.password; pw.oninput = () => { st.password = pw.value; };
      box.append(field("rd-driver", T("rd.f.driver"), T("rd.f.driver.help"), input("driver")), field("rd-dsn", T("rd.f.dsn"), T("rd.f.dsn.help"), input("dsn")), field("rd-dialect", T("rd.f.dialect"), "", dia),
        field("rd-pw", T("rd.f.password"), st.view && st.view.has_password ? T("rd.f.password.kept", "●".repeat(Math.min(st.view.password_chars || 0, 24)), st.view.password_chars || 0) : T("rd.f.password.help"), pw));
    }
    if (k !== "sqlite") box.append(field("rd-prefix", T("rd.f.prefix"), T("rd.f.prefix.help"), Object.assign(input("prefix", "text", 31), { placeholder: "rag_" })));
  }

  function nowLine() {
    const v = st.view;
    if (!v) return el("p", "rg-help", T("rd.loading"));
    if (v.error) return el("p", "rg-note", T("rd.now.error", v.error));
    if (!v.exists) return el("p", "rg-help", T("rd.now.none", T("rd.kind." + v.config.kind)));
    const i = v.info || {};
    return el("p", "rg-help", T("rd.now", T("rd.kind." + v.config.kind), T("rd.mode." + (i.vectors || "app")), T("rd.mode." + (i.words || "app")), v.archives));
  }

  function stepsBlock() {
    if (!st.steps.length && !st.running) return null;
    const ol = el("ol", "rd-steps");
    ol.setAttribute("aria-label", T("rd.steps"));
    ["connect", "extension", "configure"].forEach(name => {
      const s = st.steps.find(x => x.step === name);
      const li = el("li", "rd-step" + (s ? (s.ok ? " ok" : " bad") : ""));
      const head = el("b", "", T("rd.step." + name));
      const mark = el("span", "rd-mark", s ? (s.ok ? T("rd.ok") : T("rd.failed")) : (st.running && st.stage.split(":")[0] === name ? T("rd.running") : T("rd.waiting")));
      li.append(head, " ", mark);
      if (s) {
        li.append(el("p", "rg-help", s.message));
        if (s.needs_superuser && !st.superuser.trim()) li.append(el("p", "rg-note", T("rd.super.ask")));
        if (s.sql) li.append(el("pre", "rd-code", s.sql));
        if (s.steps) { const ul = el("ul", "rd-howto"); s.steps.forEach(x => ul.append(el("li", "", x))); li.append(ul); }
        if (s.component) li.append(btn(T("rd.comp.go", s.component), "", () => { const c = st.comps.find(x => x.id === s.component); if (c) installComponent(c); }));
        if (s.state === "not_available") li.append(planB(s));
      }
      ol.append(li);
    });
    return ol;
  }

  function planB(s) {
    const box = el("div", "rd-planb");
    box.append(el("p", "", T("rd.planb.title")));
    [["blob", T("rd.planb.blob")], ["file", T("rd.planb.file")]].forEach(([v, text]) => {
      const lab = el("label", "rd-radio"), r = el("input"); r.type = "radio"; r.name = "rd-planb"; r.value = v; r.checked = st.planB === v;
      r.onchange = () => { st.planB = v; repaint(); };
      lab.append(r, el("span", "", text)); box.append(lab);
    });
    if (st.planB === "file") {
      const f = el("input"); f.type = "text"; f.value = st.vecFile; f.placeholder = T("rd.planb.file.ph"); f.autocomplete = "off"; f.oninput = () => { st.vecFile = f.value; };
      box.append(field("rd-vecfile", T("rd.planb.path"), "", f));
    }
    const row = el("div", "rd-actions");
    row.append(btn(T("rd.planb.go"), "primary", () => prepare({ plan_b: st.planB, vec_file: st.planB === "file" ? st.vecFile.trim() : undefined })),
      btn(T("rd.again"), "", () => prepare({})));
    if (s.can_install) row.append(btn(T("rd.install.local"), "", () => prepare({ install_local: true })));
    box.append(row);
    return box;
  }

  function importBlock() {
    const v = st.view;
    if (!v) return null;
    const n = (v.waiting_here || []).length, box = el("div", "rd-import");
    box.append(el("h4", "rg-h2", T("rd.import.title")));
    box.append(el("p", "rg-help", n ? T("rd.import.waiting", n) : T("rd.import.none")), el("p", "rg-help", T("rd.import.help")));
    if (n) box.append(btn(st.importing ? T("rd.running") : T("rd.import.go"), "primary", importAll));
    if (st.importing && st.progress) box.append(el("p", "rg-help", st.progress));
    if (st.importResult) {
      const ul = el("ul", "rd-howto");
      st.importResult.imported.forEach(r => ul.append(el("li", "", T("rd.import.done", r.name || r.archive, r.chunks, r.vectors, r.blocks + r.images))));
      st.importResult.failed.forEach(f => ul.append(el("li", "rd-bad", T("rd.import.fail", f.archive, f.error))));
      box.append(ul, el("p", "rg-help", T("rd.import.kept")));
    }
    return box;
  }

  const NEEDS = { sqlite: ["sqlite-vec"], postgres: ["psycopg"], mysql: ["pymysql"], other: [] };      // the package each kind of database needs
  function componentsBlock() {
    const box = el("div", "rd-comps"), needed = NEEDS[st.cfg.kind] || [], shown = st.comps.filter(c => needed.includes(c.id));
    box.append(el("h4", "rg-h2", T("rd.comp.title")), el("p", "rg-help", T(shown.length ? "rd.comp.help" : "rd.comp.none")));
    const ul = el("ul", "rd-list");
    shown.forEach(c => {
      const li = el("li", "rd-comp");
      li.append(el("b", "", c.id), " ", el("span", "rd-mark", c.installed ? T("rd.comp.have", c.version || "") : T("rd.comp.missing")), el("p", "rg-help", c.what[LANG] || c.what.en),
        el("p", "rg-help rd-mono", `${c.requirement} - ${T("rd.comp.size", c.size_mb)}`));
      if (!c.installed) li.append(btn(st.compBusy === c.id ? T("rd.running") : T("rd.comp.install", c.size_mb), "", () => installComponent(c)));
      if (st.compBusy === c.id && st.log.length) li.append(el("pre", "rd-code", st.log.join("\n")));
      ul.append(li);
    });
    box.append(ul);
    return box;
  }

  function build() {
    const box = el("details", "rg-settings rd-box"), sum = el("summary", "rg-sum", T("rd.title"));
    box.open = st.open;
    box.ontoggle = () => { st.open = box.open; };
    box.append(sum, el("p", "rg-help", T("rd.intro")), nowLine());
    const kind = el("select");
    KINDS.forEach(k => { const o = el("option", "", T("rd.kind." + k)); o.value = k; kind.append(o); });
    kind.value = st.cfg.kind; kind.onchange = () => { st.cfg = { kind: kind.value }; st.steps = []; repaint(); };
    box.append(field("rd-kind", T("rd.kind"), T("rd.kind.help"), kind));
    kindFields(box);
    const row = el("div", "rd-actions");
    const go = btn(st.running ? T("rd.running") : T("rd.prepare"), "primary", () => prepare({}));
    go.disabled = st.running || st.testing;
    const test = btn(st.testing ? T("rd.running") : T("rd.test"), "", runTest);
    test.disabled = st.running || st.testing; test.title = T("rd.test.tip");
    row.append(go, test);
    if (st.view && st.view.config.kind !== "sqlite" || (st.view && st.view.config.path !== st.view.default_path)) row.append(btn(T("rd.reset"), "ghost", reset));
    box.append(row);
    const status = el("p", "rg-note", st.note); status.setAttribute("role", "status"); status.dataset.bad = st.bad ? "1" : ""; status.hidden = !st.note;
    box.append(status);
    const tested = testBlock();
    if (tested) box.append(tested);
    const steps = stepsBlock();
    if (steps) box.append(steps);
    box.append(importBlock() || "", componentsBlock());
    return box;
  }

  /* what the Knowledge tab puts in its settings: the block of this project, made again at every paint from what was typed so far */
  window.RagDatabase = {
    block(project, again, onChange) {
      repaint = again; changed = onChange || (() => {});
      if (!st || st.project !== project) { st = fresh(project); load(); }
      return build();
    },
  };
})();
