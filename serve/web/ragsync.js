"use strict";
/* Knowledge, keeping an archive up to date: where its documents come from (a file, a folder, a web address, a MediaWiki page, the company wiki), a check for what
   changed, what an update would change, one button for all, the versions kept, a verify, the coverage of every reading, and a schedule. rag.js puts the block in the
   archive's page (`ragSync.block`) and a few lines under each document (`ragSync.docLines`). Everything the server returns enters the page as text. */
(function () {
  const POLL_MS = 800, ARM_MS = 4000;
  const cache = {};                                   // archive id -> { view, at }
  let cur = null, ctx = null, working = false, status = "", jobId = "", note = "", noteBad = false, armed = "", armTimer = 0, verifyOut = null, deep = false;
  const diffs = {}, versions = {}, shown = {};
  let routines = [], wiki = null, kind = "folder";

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const when = t => (t ? new Date(t * 1000).toLocaleString() : "");
  const why = r => (r.status === 0 ? T("rg.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const base = aid => `api/rag/archives/${encodeURIComponent(aid)}`;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const say = (text, bad) => { note = text; noteBad = !!bad; repaint(); };
  const root = () => document.getElementById("rs-root");
  function repaint() { const r = root(); if (r && cur) fill(r); }
  const repaintAll = () => { repaint(); if (ctx && ctx.paint) ctx.paint(); };                 // this block, and the tree that rag.js draws in the list of documents
  function arm(key) { armed = key; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = ""; repaintAll(); }, ARM_MS); repaintAll(); }

  async function load(aid) {
    const r = await api("GET", `${base(aid)}/sync`);
    if (!r.ok) { say(why(r), true); return; }
    cache[aid] = { ...(cache[aid] || {}), view: r.data, at: Date.now() };
    const tr = await api("GET", `${base(aid)}/tree`);
    if (tr.ok) trees[aid] = tr.data;
    const rt = await api("GET", "api/routines");
    routines = rt.ok ? (rt.data.routines || []).filter(x => x.deliver_to && x.deliver_to.kind === "knowledge") : [];
    repaint();
  }

  /* a check, an update, a verify or a restore is a job on the server: follow it, show its stage, end with its result */
  async function job(path, body) {
    working = true; status = T("rs.stage.checking"); repaint();
    const r = await api("POST", path, body);
    if (!r.ok) { working = false; status = ""; say(why(r), true); return null; }
    let j = r.data.job;
    jobId = j.id;
    while (j.state === "running") {
      status = T("rs.stage." + (j.stage || "checking")) + (j.pages ? ` ${j.page}/${j.pages}` : "");
      repaint();
      await new Promise(res => setTimeout(res, POLL_MS));
      const g = await api("GET", `api/rag/jobs/${encodeURIComponent(j.id)}`);
      if (!g.ok) { working = false; jobId = ""; status = ""; say(why(g), true); return null; }
      j = g.data.job;
    }
    working = false; jobId = ""; status = "";
    if (j.state === "done") return j.result;
    say(j.state === "cancelled" ? T("rs.cancelled") : j.error, true);
    return null;
  }
  async function done(text) { say(text, false); await load(cur.id); if (ctx) ctx.reload(); }

  async function check() {
    const r = await job(`${base(cur.id)}/check`, {});
    if (r) { await done(T("rs.check.done", r.summary)); }
  }
  async function update(only, force) {
    const r = await job(`${base(cur.id)}/refresh`, { ...(only ? { only } : {}), ...(force ? { force } : {}) });
    if (r) {
      Object.keys(diffs).forEach(k => delete diffs[k]);
      await done(T("rs.update.done", r.summary) + (r.note ? " " + r.note : "") + (r.embedded ? " " + T("rs.update.embedded", r.embedded, r.reused) : ""));
    }
  }
  async function preview(item) {
    const r = await job(`${base(cur.id)}/refresh`, { only: [item.id], dry_run: true, force: item.state === "pinned" ? [item.id] : [] });
    if (r) { diffs[item.id] = r.items.find(i => i.id === item.id) || { state: "unchanged", detail: "" }; say("", false); }
  }
  async function verify() {
    const r = await job(`${base(cur.id)}/verify`, { deep });
    if (r) { verifyOut = r; await load(cur.id); say(r.ok ? T("rs.verify.ok") : T("rs.verify.bad", r.errors), !r.ok); }
  }
  async function gone(doc, action) {
    armed = "";
    const r = await api("POST", `${base(cur.id)}/documents/${encodeURIComponent(doc)}/gone`, { action });
    if (!r.ok) { say(why(r), true); return; }
    await done(action === "remove" ? T("rs.gone.removed") : T("rs.gone.kept"));
  }
  async function addSource(body) {
    const r = await api("POST", `${base(cur.id)}/sources`, body);
    if (!r.ok) { say(why(r), true); return; }
    await done(T("rs.source.added"));
  }
  async function removeSource(sid, docs) {
    armed = "";
    const r = await api("POST", `${base(cur.id)}/sources/${encodeURIComponent(sid)}/remove`, { remove_documents: !!docs });
    if (!r.ok) { say(why(r), true); return; }
    await done(T("rs.source.removed"));
  }
  async function showVersions(doc) {
    shown[doc] = !shown[doc];
    if (shown[doc]) { const r = await api("GET", `${base(cur.id)}/documents/${encodeURIComponent(doc)}/versions`); versions[doc] = r.ok ? r.data.versions : []; }
    repaint();
  }
  async function restore(doc, n) {
    armed = "";
    const r = await job(`${base(cur.id)}/documents/${encodeURIComponent(doc)}/rollback`, { version: n });
    if (r) { shown[doc] = false; await done(T("rs.version.restored", n)); }
  }
  async function saveSettings(body) {
    const r = await api("POST", "api/rag/sync/settings", body);
    say(r.ok ? T("rg.saved") : why(r), !r.ok);
    if (r.ok) await load(cur.id);
  }
  async function addRoutine(name, action, schedule) {
    const r = await api("POST", "api/routines", { name: `Knowledge: ${name} (${action})`, prompt: `${action === "check" ? "Check" : "Update"} the knowledge archive ${name}`, workspace: "", schedule,
      deliver_to: { kind: "knowledge", ref: cur.id }, message: action });
    if (!r.ok) { say((r.data && r.data.error && r.data.error.message) || why(r), true); return; }
    await done(T("rs.routine.added"));
  }
  /* ---- the tree of documents by where they come from: a switch for each node (three states), how often it is checked, read it again, remove it ---- */
  const open = {};                                                          // node id -> opened or closed by the person (kinds and sources start open)
  let editing = null;                                                       // the node whose schedule is being edited
  const DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
  async function treeCall(path, body) {
    const r = await api("POST", `${base(cur.id)}/tree/${path}`, body);
    if (!r.ok) { say(why(r), true); repaint(); if (ctx && ctx.paint) ctx.paint(); return null; }
    if (r.data.tree) trees[cur.id] = r.data;
    repaint(); if (ctx && ctx.paint) ctx.paint();
    return r.data;
  }
  function freqText(f) {
    if (!f || f.mode === "manual") return T("rs.tree.f.manual");
    if (f.mode === "parts") return T("rs.tree.f.parts");
    if (f.mode === "interval") return T("rs.tree.f.every", f.every, T("rs.tree.unit." + f.unit));
    const at = f.at || "03:00";
    if (f.type === "weekday") return T("rs.tree.f.weekday", f.days.map(d => T("rs.tree.day." + DAYS[d])).join(", "), at);
    if (f.type === "monthday") return T("rs.tree.f.monthday", f.days.map(d => (d === "last" ? T("rs.tree.last") : d)).join(", "), at);
    if (f.type === "nth_weekday") return T("rs.tree.f.nth", T("rs.tree.nth." + f.nth), T("rs.tree.day." + DAYS[f.weekday]), at);
    return T("rs.tree.f." + f.type, at);
  }
  function schedulesSomething(t) {
    let any = false;
    const go = n => { if (n.freq && (n.freq.mode === "interval" || n.freq.mode === "occurrence")) any = true; n.children.forEach(go); };
    (t.tree || []).forEach(go);
    return any;
  }
  async function ensureDueRoutine() {
    const have = routines.find(r => r.message === "due" && r.deliver_to.ref === cur.id);
    if (have || !cur) return;
    await addRoutine(cur.name, "due", "every 5m");
  }
  function freqDialog(n) {
    const dlg = el("dialog", "rg-dlg"), f = n.freq || { mode: "manual" };
    dlg.setAttribute("aria-label", T("rs.tree.f.title", n.label));
    const sel = (id, options, value) => { const s = el("select"); s.id = id; options.forEach(([v, text]) => { const o = el("option", "", text); o.value = String(v); s.append(o); }); s.value = String(value); return s; };
    const mode = sel("rs-f-mode", [["manual", T("rs.tree.m.manual")], ["interval", T("rs.tree.m.interval")], ["parts", T("rs.tree.m.parts")], ["occurrence", T("rs.tree.m.occurrence")]], f.mode);
    const every = el("input"); every.type = "number"; every.min = "1"; every.step = "1"; every.value = f.every || 1; every.id = "rs-f-every";
    const unit = sel("rs-f-unit", ["minutes", "hours", "days", "weeks", "months", "years"].map(u => [u, T("rs.tree.unit." + u)]), f.unit || "days");
    const kind = sel("rs-f-kind", [["weekday", T("rs.tree.k.weekday")], ["monthday", T("rs.tree.k.monthday")], ["nth_weekday", T("rs.tree.k.nth")], ["first_workday", T("rs.tree.k.first_workday")], ["last_workday", T("rs.tree.k.last_workday")]], f.type || "weekday");
    const at = el("input"); at.type = "time"; at.value = f.at || "03:00"; at.id = "rs-f-at";
    const days = DAYS.map((d, i) => { const c = el("input"); c.type = "checkbox"; c.value = i; c.checked = (f.days || []).includes(i); c.id = "rs-f-d" + i; return [i, c]; });
    const mdays = el("input"); mdays.type = "text"; mdays.id = "rs-f-md"; mdays.placeholder = "1, 15, last"; mdays.value = (f.type === "monthday" ? f.days : []).join(", ");
    const nth = sel("rs-f-nth", [[1, T("rs.tree.nth.1")], [2, T("rs.tree.nth.2")], [3, T("rs.tree.nth.3")], [4, T("rs.tree.nth.4")], [-1, T("rs.tree.nth.-1")]], f.nth || 1);
    const wday = sel("rs-f-wd", DAYS.map((d, i) => [i, T("rs.tree.day." + d)]), f.weekday || 0);
    const err = el("p", "rg-note"); err.setAttribute("role", "status");
    const lab = (id, key, input, cls) => { const w = el("div", "rg-field " + (cls || "")), l = el("label", "", T(key)); l.htmlFor = id; w.append(l, input); return w; };
    const interval = el("div", "rs-fgroup"), occ = el("div", "rs-fgroup"), dayRow = el("div", "rs-fgroup rs-days");
    interval.append(lab("rs-f-every", "rs.tree.f.n", every), lab("rs-f-unit", "rs.tree.f.unit", unit));
    days.forEach(([i, c]) => { const l = el("label", "rs-day"); l.append(c, el("span", "", T("rs.tree.day." + DAYS[i]))); dayRow.append(l); });
    const monthRow = lab("rs-f-md", "rs.tree.f.monthdays", mdays), nthRow = el("div", "rs-fgroup"), atRow = lab("rs-f-at", "rs.tree.f.at", at);
    nthRow.append(lab("rs-f-nth", "rs.tree.f.nthof", nth), lab("rs-f-wd", "rs.tree.f.weekday.label", wday));
    occ.append(lab("rs-f-kind", "rs.tree.f.type", kind), dayRow, monthRow, nthRow, atRow);
    const show = () => {
      interval.hidden = mode.value !== "interval"; occ.hidden = mode.value !== "occurrence";
      dayRow.hidden = kind.value !== "weekday"; monthRow.hidden = kind.value !== "monthday"; nthRow.hidden = kind.value !== "nth_weekday";
    };
    mode.onchange = show; kind.onchange = show; show();
    const save = btn(T("rg.save"), "primary", async () => {
      let freq;
      if (mode.value === "manual" || mode.value === "parts") freq = { mode: mode.value };
      else if (mode.value === "interval") freq = { mode: "interval", every: Number(every.value), unit: unit.value };
      else {
        freq = { mode: "occurrence", type: kind.value, at: at.value || "03:00" };
        if (kind.value === "weekday") freq.days = days.filter(([, c]) => c.checked).map(([i]) => i);
        if (kind.value === "monthday") freq.days = mdays.value.split(",").map(x => x.trim()).filter(Boolean).map(x => (x.toLowerCase() === "last" ? "last" : Number(x)));
        if (kind.value === "nth_weekday") { freq.nth = Number(nth.value); freq.weekday = Number(wday.value); }
      }
      const r = await treeCall("frequency", { node: n.id, frequency: freq });
      if (r) { dlg.close(); if (schedulesSomething(r)) await ensureDueRoutine(); }
    });
    const bar = el("div", "rg-bar");
    bar.append(save, btn(T("rg.cancel"), "", () => dlg.close()));
    dlg.append(el("h4", "rg-h2", T("rs.tree.f.title", n.label)), lab("rs-f-mode", "rs.tree.f.mode", mode), interval, occ, el("p", "rg-help", T("rs.tree.f.help")), err, bar);
    dlg.onclose = () => dlg.remove();
    document.body.append(dlg); dlg.showModal();
  }
  function nodeRow(n, above, renderDoc) {
    const li = el("li", "rs-node rs-n-" + n.type), row = el("div", "rs-nrow");
    li.setAttribute("role", "treeitem"); li.dataset.node = n.id;
    const kids = n.children || [], isOpen = open[n.id] !== undefined ? open[n.id] : (n.type !== "dir" || treeSize <= 80);      // directories start open while the tree is small
    if (kids.length) {
      const tg = btn("", "rs-toggle", () => { open[n.id] = !isOpen; repaint(); if (ctx && ctx.paint) ctx.paint(); });
      tg.append(el("span", "", isOpen ? "▾" : "▸")); tg.setAttribute("aria-label", T(isOpen ? "rs.tree.collapse" : "rs.tree.expand") + ": " + n.label); tg.setAttribute("aria-expanded", String(isOpen));
      row.append(tg);
    } else row.append(el("span", "rs-toggle rs-leaf"));
    if (!n.hand) {
      const box = el("input"); box.type = "checkbox"; box.id = "rs-cb-" + n.id; box.checked = n.state === "on"; box.indeterminate = n.state === "mixed";
      const blocked = n.own && n.state === "off" && !above;                      // on by itself, but off because a node above it is off
      box.disabled = working || blocked; if (blocked) box.title = T("rs.tree.blocked");
      box.setAttribute("aria-label", T("rs.tree.follow") + ": " + n.label); box.setAttribute("aria-checked", n.state === "mixed" ? "mixed" : String(n.state === "on"));
      box.onchange = () => treeCall("follow", { node: n.id, on: n.state === "off" || n.state === "mixed" });
      row.append(box);
    }
    if (n.type === "doc") {
      const d = renderDoc ? renderDoc(n.doc) : null;
      if (d) row.append(d); else row.append(el("span", "rs-nlabel", n.label));
    } else {
      const label = n.type === "kind" ? T("rs.tree.kind." + n.label) : n.label;
      row.append(el("span", "rs-nlabel", label), el("span", "rg-dmeta", T("rs.tree.count", countDocs(n))));
    }
    if (!n.hand) {
      const fb = btn(T("rs.tree.f.short", freqText(n.freq)), "rs-freq", () => freqDialog(n));
      fb.id = "rs-freq-" + n.id; fb.title = T("rs.tree.f.tip"); row.append(fb);
      const again = btn(T("rs.tree.again"), "", async () => { const r = await job(`${base(cur.id)}/tree/refresh`, { node: n.id }); if (r) await done(T("rs.tree.again.done", r.summary || "")); });
      again.id = "rs-again-" + n.id; again.title = T("rs.tree.again.tip"); again.disabled = working || n.state === "off";
      row.append(again);
    }
    const key = "n" + n.id, sure = armed === key;
    if (n.type !== "doc") {
      const rm = btn(sure ? T("rs.tree.remove.sure") : T("rs.tree.remove"), sure ? "rg-warn" : "", async () => {
        if (!sure) { arm(key); return; }
        armed = ""; const r = await treeCall("remove", { node: n.id, confirm: true }); if (r) await done(T("rs.tree.removed", r.removed_documents));
      });
      rm.id = "rs-rm-" + n.id; row.append(rm);
    }
    li.append(row);
    if (kids.length && isOpen) {
      const ul = el("ul", "rs-tree-list"); ul.setAttribute("role", "group");
      kids.forEach(c => ul.append(nodeRow(c, above && n.own, renderDoc)));
      li.append(ul);
    }
    return li;
  }
  function countDocs(n) { return n.type === "doc" ? 1 : n.children.reduce((s, c) => s + countDocs(c), 0); }
  /* what rag.js puts in place of the flat list of documents; null until the tree has come */
  function treeBlock(archive, renderDoc) {
    const c = trees[archive.id];
    if (!c) { if (!treeLoading[archive.id]) { treeLoading[archive.id] = true; api("GET", `${base(archive.id)}/tree`).then(r => { treeLoading[archive.id] = false; if (r.ok) { trees[archive.id] = r.data; if (ctx && ctx.paint) ctx.paint(); } }); } return null; }
    treeSize = c.tree.reduce((s, n) => s + countNodes(n), 0);
    const box = el("div", "rs-tree"), ul = el("ul", "rs-tree-list rs-tree-root");
    box.setAttribute("aria-label", T("rs.tree.title"));
    ul.setAttribute("role", "tree");
    c.tree.forEach(n => ul.append(nodeRow(n, true, renderDoc)));
    box.append(el("p", "rg-help", T("rs.tree.help")), ul);
    return box;
  }
  const treeLoading = {}, trees = {};                                       // archive id -> the tree the server gave
  let treeSize = 0;
  function countNodes(n) { return 1 + (n.children || []).reduce((s, c) => s + countNodes(c), 0); }

  /* ---- the page ---- */
  const STATES = { changed: "rs.state.changed", new: "rs.state.new", gone: "rs.state.gone", error: "rs.state.error", pinned: "rs.state.pinned", unchanged: "rs.state.unchanged",
    updated: "rs.state.updated", added: "rs.state.added", linked: "rs.state.linked", failed: "rs.state.failed", "would update": "rs.state.wouldupdate", "would add": "rs.state.wouldadd" };
  const stateText = s => T(STATES[s] || "rs.state.unchanged");
  const KINDS = ["folder", "file", "url", "mediawiki", "wikibridge"];

  function field(id, label, help, control) {
    const w = el("div", "rg-field"), l = el("label", "", label);
    l.htmlFor = id; control.id = id;
    w.append(l, control);
    if (help) w.append(el("p", "rg-help", help));
    return w;
  }
  const textIn = (value, max) => { const i = el("input"); i.type = "text"; i.value = value || ""; i.maxLength = max || 500; i.autocomplete = "off"; i.spellcheck = false; return i; };
  const numIn = (value, lo, hi, step) => { const i = el("input"); i.type = "number"; i.min = lo; i.max = hi; i.step = step; i.value = String(value); i.inputMode = "decimal"; return i; };

  function itemRow(it, aid) {
    const li = el("li", "rs-item"), t = el("div", "rs-item-t");
    const head = el("div", "rs-item-h");
    head.append(el("span", "rs-name", it.name), el("span", "rs-tag rs-" + it.state.replace(" ", "-"), stateText(it.state)));
    t.append(head);
    if (it.detail) t.append(el("span", "rg-dmeta", it.detail));
    const d = diffs[it.id];
    if (d && d.passages) {
      t.append(el("span", "rg-dmeta", T("rs.diff", d.passages.added, d.passages.removed, d.passages.kept)));
      const ex = d.examples || {};
      (ex.removed || []).forEach(x => t.append(el("span", "rs-ex rs-ex-old", "- " + x)));
      (ex.added || []).forEach(x => t.append(el("span", "rs-ex rs-ex-new", "+ " + x)));
    } else if (d) t.append(el("span", "rg-dmeta", d.detail || stateText(d.state)));
    const bar = el("div", "rs-item-b");
    if (it.state === "changed" || it.state === "new" || it.state === "pinned") {
      bar.append(btn(T("rs.what"), "", () => preview(it)), btn(it.state === "pinned" ? T("rs.update.latest") : T("rs.update.one"), "primary", () => update([it.id], it.state === "pinned" ? [it.id] : null)));
    }
    if (it.state === "gone" && !it.acknowledged) {
      const sure = armed === "g" + it.doc;
      bar.append(btn(T("rs.gone.keep"), "", () => gone(it.doc, "keep")), btn(sure ? T("rs.gone.remove.sure") : T("rs.gone.remove"), "rg-warn", () => { if (!sure) arm("g" + it.doc); else gone(it.doc, "remove"); }));
    }
    if (it.state === "gone" && it.acknowledged) t.append(el("span", "rg-dmeta", T("rs.gone.kept.line")));
    Array.from(bar.children).forEach(b => { b.disabled = working; });
    li.append(t, bar);
    return li;
  }

  function changesBlock(v) {
    const box = el("div", "rs-changes"), chk = v.check || {};
    const items = (chk.items || []).filter(i => i.state !== "unchanged");
    const line = chk.at ? T("rs.last", when(chk.at), chk.summary || "") : T("rs.never");
    box.append(el("p", "rg-line", line));
    (chk.errors || []).forEach(e => { const p = el("p", "rg-line rg-warnline", `${e.label}: ${e.error}`); p.setAttribute("role", "status"); box.append(p); });
    if (items.length) { const ul = el("ul", "rs-items"); items.forEach(i => ul.append(itemRow(i, v.archive))); box.append(ul); }
    return box;
  }

  function coverageBlock(v) {
    const c = v.coverage, box = el("div", "rs-cov");
    box.append(el("p", "rg-line", T("rs.cov", c.covered, c.pages, c.ok, c.check, c.incomplete)));
    box.dataset.status = c.status;
    (c.failed || []).forEach(f => box.append(el("p", "rg-line rg-warnline", T("rs.cov.failed", f.name, f.reason))));
    (c.problems || []).slice(0, 12).forEach(p => { const d = el("p", "rg-dmeta" + (p.status === "incomplete" ? " rg-warnline" : ""), `${p.name}: ${p.problems.join("; ")}`); box.append(d); });
    if (!c.problems.length && !c.failed.length && c.status === "ok") box.append(el("p", "rg-help", T("rs.cov.ok")));
    return box;
  }

  function verifyBlock() {
    const r = verifyOut;
    if (!r || r.archive !== cur.id) return null;
    const box = el("div", "rs-verify");
    box.append(el("p", "rg-line", T(r.ok ? "rs.verify.line.ok" : "rs.verify.line.bad", r.errors, r.warnings, r.documents, r.passages)));
    r.problems.slice(0, 30).forEach(p => box.append(el("p", "rg-dmeta" + (p.level === "error" ? " rg-warnline" : ""), `${T("rs.level." + p.level)}${p.name ? " · " + p.name : ""}: ${p.text}`)));
    return box;
  }

  function sourcesBlock(v) {
    const box = el("div", "rs-sources");
    if (!v.sources.length) box.append(el("p", "rg-help", cur && cur.documents ? T("rs.sources.byhand", cur.documents) : T("rs.sources.none")));
    const ul = el("ul", "rs-items");
    v.sources.forEach(s => {
      const li = el("li", "rs-item"), t = el("div", "rs-item-t"), n = v.links.filter(l => l.source === s.id).length;
      t.append(el("span", "rs-name", s.label), el("span", "rg-dmeta", T("rs.source.line", T("rs.kind." + s.kind), n)));
      const sure = armed === "s" + s.id, bar = el("div", "rs-item-b");
      bar.append(btn(sure ? T("rs.source.remove.sure") : T("rs.source.remove"), "rg-warn", () => { if (!sure) arm("s" + s.id); else removeSource(s.id, false); }));
      li.append(t, bar); ul.append(li);
    });
    box.append(ul);
    box.append(el("p", "rg-help", T("rs.source.help")));
    const form = el("form", "rg-form"), pick = el("select");
    KINDS.forEach(k => { const o = el("option", "", T("rs.kind." + k)); o.value = k; pick.append(o); });
    pick.value = kind;
    pick.onchange = () => { kind = pick.value; repaint(); };
    const fields = el("div", "rg-form"), inputs = {};
    const add = (key, label, help, value) => { inputs[key] = textIn(value, 500); fields.append(field("rs-f-" + key, label, help, inputs[key])); };
    if (kind === "folder" || kind === "file") add("path", T(kind === "folder" ? "rs.f.folder" : "rs.f.file"), T("rs.f.path.help"), "");
    if (kind === "url") {
      add("url", T("rs.f.url"), T("rs.f.url.help"), "");
      add("depth", T("rs.f.depth"), T("rs.f.depth.help"), "2");
      add("limit", T("rs.f.limit"), T("rs.f.limit.help"), "");
      inputs.depth.type = "number"; inputs.depth.min = "0"; inputs.depth.max = "10"; inputs.depth.inputMode = "numeric";
      inputs.limit.type = "number"; inputs.limit.min = "1"; inputs.limit.inputMode = "numeric";
    }
    if (kind === "mediawiki") { add("url", T("rs.f.wikipage"), T("rs.f.wikipage.help"), ""); add("category", T("rs.f.category"), T("rs.f.category.help"), ""); }
    if (kind === "wikibridge") { add("address", T("rs.f.address"), T("rs.f.address.help"), wiki ? wiki.wiki_url : ""); add("project", T("rs.f.wproject"), T("rs.f.wproject.help"), wiki ? wiki.wiki_project : ""); }
    form.append(field("rs-kind", T("rs.f.kind"), "", pick), fields);
    const go = btn(T("rs.source.add"), "primary", () => {
      const body = { kind };
      Object.entries(inputs).forEach(([k, i]) => { if (i.value.trim()) body[k] = i.value.trim(); });
      ["depth", "limit"].forEach(k => { if (body[k] !== undefined) body[k] = Number(body[k]); });          // the depth and the limit are numbers; an empty limit is no limit
      addSource(body);
    });
    go.type = "submit"; go.disabled = working; go.title = T("rs.source.add.tip");
    form.onsubmit = e => { e.preventDefault(); go.click(); };
    form.append(go);
    const det = el("details", "rg-settings"), sum = el("summary", "rg-sum", T("rs.source.new"));
    det.append(sum, form);
    if (!v.sources.length) det.open = true;
    box.append(det);
    return box;
  }

  function versionsBlock(v) {
    const withV = v.links.filter(l => l.versions > 0);
    if (!withV.length) return null;
    const box = el("div", "rs-vers"), ul = el("ul", "rs-items");
    withV.forEach(l => {
      const li = el("li", "rs-item"), t = el("div", "rs-item-t"), bar = el("div", "rs-item-b");
      t.append(el("span", "rs-name", l.name), el("span", "rg-dmeta", T("rs.versions.count", l.versions) + (l.pinned ? " " + T("rs.state.pinned") : "")));
      bar.append(btn(shown[l.doc] ? T("rs.versions.hide") : T("rs.versions.show"), "", () => showVersions(l.doc)));
      li.append(t, bar);
      if (shown[l.doc]) {
        const inner = el("ul", "rs-items rs-inner");
        (versions[l.doc] || []).forEach(x => {
          const row = el("li", "rs-item"), tt = el("div", "rs-item-t"), key = `v${l.doc}${x.n}`, sure = armed === key;
          tt.append(el("span", "rs-name", T("rs.version.n", x.n, when(x.at))), el("span", "rg-dmeta", T("rs.version.meta", x.version || "-", x.chunks, x.pages || 0)));
          const b = btn(sure ? T("rs.version.restore.sure") : T("rs.version.restore"), "", () => { if (!sure) arm(key); else restore(l.doc, x.n); });
          b.disabled = working;
          row.append(tt, b); inner.append(row);
        });
        li.append(inner);
      }
      ul.append(li);
    });
    box.append(ul);
    return box;
  }

  function settingsBlock(v) {
    const s = v.settings, det = el("details", "rg-settings"), keep = numIn(s.keep_versions, 0, 20, 1), days = numIn(s.keep_days, 0, 3650, 1), gap = numIn(s.min_interval_s, 0, 60, 0.5),
      mb = numIn(s.max_download_mb, 1, 200, 1), contact = textIn(s.contact, 200), robots = el("input");
    robots.type = "checkbox"; robots.checked = !!s.respect_robots;
    const rw = el("div", "rg-field rg-check"), lab = el("label", "");
    robots.id = "rs-robots"; lab.htmlFor = "rs-robots"; lab.append(robots, el("span", "", T("rs.set.robots")));
    rw.append(lab, el("p", "rg-help", T("rs.set.robots.help")));
    det.append(el("summary", "rg-sum", T("rs.set.title")), field("rs-keep", T("rs.set.keep"), T("rs.set.keep.help"), keep), field("rs-days", T("rs.set.days"), T("rs.set.days.help"), days),
      field("rs-gap", T("rs.set.gap"), T("rs.set.gap.help"), gap), field("rs-mb", T("rs.set.mb"), T("rs.set.mb.help"), mb), field("rs-contact", T("rs.set.contact"), T("rs.set.contact.help"), contact), rw,
      btn(T("rg.save"), "primary", () => saveSettings({ keep_versions: Number(keep.value), keep_days: Number(days.value), min_interval_s: Number(gap.value), max_download_mb: Number(mb.value),
        contact: contact.value.trim(), respect_robots: robots.checked })));
    return det;
  }

  function sub(title, id, ...children) {
    const s = el("section", "rg-block"), h = el("h4", "rg-h2", title);
    s.setAttribute("aria-labelledby", id); h.id = id;
    s.append(h, ...children.filter(Boolean));
    return s;
  }

  function fill(box) {
    const c = cache[cur.id], kids = [];
    if (!c) { box.replaceChildren(el("p", "rg-help", T("rg.loading"))); return; }
    const v = c.view, pending = (v.check.items || []).filter(i => (i.state === "changed" || i.state === "new") && !i.acknowledged).length;
    const bar = el("div", "rg-bar"), chk = btn(T("rs.check"), "primary", check), all = btn(T("rs.update.all", pending), pending ? "primary" : "", () => update(null, null));
    chk.disabled = working || !v.sources.length; all.disabled = working || !pending;
    const stop = jobId ? btn(T("rg.job.cancel"), "", async () => { await api("POST", `api/rag/jobs/${encodeURIComponent(jobId)}/cancel`, {}); }) : null;
    bar.append(chk, all, ...(stop ? [stop] : []));
    const st = el("p", "rg-note", status || note); st.setAttribute("role", "status"); st.dataset.bad = !status && noteBad ? "1" : ""; st.hidden = !(status || note);
    const vbar = el("div", "rg-bar"), dp = el("input"), dl = el("label", "rs-deep");
    dp.type = "checkbox"; dp.checked = deep; dp.id = "rs-deep"; dp.onchange = () => { deep = dp.checked; };
    dl.htmlFor = "rs-deep"; dl.append(dp, el("span", "", T("rs.verify.deep"))); dl.title = T("rs.verify.deep.help");
    const vb = btn(T("rs.verify"), "", verify); vb.disabled = working;
    vb.title = T("rs.verify.tip");
    vbar.append(vb, dl);
    const vers = versionsBlock(v);
    kids.push(el("h4", "rg-h2", T("rs.title")), el("p", "rg-help", T("rs.help")), bar, st, changesBlock(v),
      sub(T("rs.sources.title"), "rs-src-h", sourcesBlock(v)), sub(T("rs.cov.title"), "rs-cov-h", coverageBlock(v), vbar, verifyBlock()),
      vers && sub(T("rs.versions.title"), "rs-ver-h", vers), settingsBlock(v));
    const focus = document.activeElement && document.activeElement.id;
    box.replaceChildren(...kids.filter(Boolean));
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  /* what rag.js calls */
  function block(archive, context) {
    ctx = context;
    if (!cur || cur.id !== archive.id) { verifyOut = null; note = ""; armed = ""; Object.keys(diffs).forEach(k => delete diffs[k]); }
    cur = archive;
    const box = el("section", "rg-block rs-root"); box.id = "rs-root";
    box.setAttribute("aria-label", T("rs.title"));
    const c = cache[archive.id];
    fill(box);
    if (!c || Date.now() - c.at > 4000) load(archive.id);
    if (wiki === null) { wiki = false; api("GET", "api/knowledge").then(r => { wiki = r.ok ? r.data : {}; repaint(); }); }
    return box;
  }
  /* the lines under a document: where it came from and when it was updated, and what its reading missed */
  function docLines(d) {
    const out = [], o = d.origin;
    if (o) out.push(el("span", "rg-dmeta", T("rs.doc.from", T("rs.kind." + o.kind), o.version || "", when(d.updated))));
    const cov = d.coverage;
    if (cov && cov.problems && cov.problems.length) {
      const w = el("span", "rg-dmeta" + (cov.status === "incomplete" ? " rg-warnline" : ""), T("rs.doc.cov", cov.problems.join("; ")));
      w.setAttribute("role", "status"); out.push(w);
    }
    return out;
  }
  window.ragSync = { block, docLines, tree: treeBlock, invalidate: aid => { delete trees[aid]; delete cache[aid]; } };
  document.addEventListener("tadamoo:lang", () => repaint());
})();
