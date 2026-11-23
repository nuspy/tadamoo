"use strict";
/* Tasks: the work the assistant is carrying out now and the map of the project's development, for each project (the register of tadamoo/taskboard.py).
   Three views of the same register: the tree of the open tasks (grouped), the spine (the nodes in order, open and closed, with their notes in a panel) and the history
   of what was closed. A task that is done is closed (it goes to the history); deleting removes it for good; hiding takes a node out of these views and "Show hidden
   nodes" brings it back. "This conversation" shows only the tasks the open conversation carries. The person can change anything here and the assistant reads the
   change; a task a plan commands is one line whose state and progress come from the plan. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("tk-root");
  if (!root) return;
  const POLL_MS = 5000;
  const FILTER_STATES = ["waiting", "running", "review", "decision", "queued", "blocked"];
  const EDIT_STATES = ["queued", "running", "waiting", "blocked", "review", "decision"];
  const NOTES = ["solved", "decisions", "risks"];
  const KEEP = "tadamoo.tasks";
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("tk.e.offline") : ((r.data.error || {}).message || `HTTP ${r.status}`));
  const when = t => { const d = new Date(t * 1000); return t ? d.toLocaleString(LANG, { dateStyle: "short", timeStyle: "short" }) : ""; };
  const lines = text => String(text || "").split("\n").map(x => x.replace(/^\s*[-*•]\s+/, "").trim()).filter(Boolean);
  const ICONS = { waiting: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v6M12 16.5v.5", running: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM10 8.5l6 3.5-6 3.5z",
    queued: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", blocked: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM6 6l12 12",
    review: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12.5l3 3 5-6", decision: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1 .9-1 1.7M12 17v.5",
    done: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM8 12.5l3 3 5-6" };
  function icon(name) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "tk-ico"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    p.setAttribute("d", ICONS[name] || ICONS.queued); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.append(p);
    return s;
  }
  const stored = (() => { try { return JSON.parse(localStorage.getItem(KEEP) || "{}") || {}; } catch (e) { return {}; } })();
  const S = { data: null, hist: null, project: "all", state: "", view: ["tree", "spine", "history"].includes(stored.view) ? stored.view : "tree", hidden: stored.hidden === true, session: "",
    msg: "", bad: false, editing: "", adding: "", confirm: "", node: "", loaded: false, groupEdit: "", groupAdd: "" };
  let timer = 0;
  const remember = () => { try { localStorage.setItem(KEEP, JSON.stringify({ view: S.view, hidden: S.hidden })); } catch (e) { /* no storage */ } };
  const say = (t, bad) => { S.msg = t || ""; S.bad = !!bad; };
  const active = () => document.body.dataset.view === "tasks";
  const typing = () => !!(S.editing || S.adding || S.groupEdit || S.groupAdd);
  const stateLabel = (s, closed) => T(closed ? "tk.closed" : "tk.state." + s);

  async function refresh(quiet) {
    const q = `api/register?project=${encodeURIComponent(S.project)}${S.state ? "&state=" + S.state : ""}${S.hidden ? "&hidden=1" : ""}${S.session ? "&session=" + encodeURIComponent(S.session) : ""}`;
    const r = await call("GET", q);
    if (!r.ok) { say(why(r), true); S.loaded = true; if (!quiet) paint(); return; }
    S.data = r.data; S.loaded = true;
    if (S.project === "all" && !S.session) S.projectsAll = r.data.projects.map(p => ({ name: p.name, path: p.path }));
    if (S.view !== "tree") {
      const hr = await call("GET", `api/register/history?project=${encodeURIComponent(S.project)}${S.session ? "&session=" + encodeURIComponent(S.session) : ""}`);
      S.hist = hr.ok ? Object.fromEntries(hr.data.projects.map(p => [p.path, p.history])) : {};
    }
    if (!quiet || !typing()) paint();
  }
  async function act(path, payload, okText) {
    const r = await call("POST", "api/register/" + path, payload);
    if (!r.ok) { say(why(r), true); paint(); return null; }
    say(okText || ""); S.editing = S.adding = S.confirm = S.groupEdit = S.groupAdd = "";
    await refresh();
    return r.data;
  }
  async function resume(project, t) {
    const r = await call("POST", "api/register/resume", { project, id: t.id });
    if (!r.ok) { say(why(r), true); paint(); return; }
    if (window.agentChat && window.agentChat.startWith) { if (await window.agentChat.startWith(project, r.data.text)) return; }
    say(T("tk.e.chat"), true); paint();
  }

  /* ---- the parts ---- */
  const field = (label, el, id) => { el.id = id; return h("label", "tk-field", h("span", "", label), el); };
  const input = (value, id, ph) => { const i = h("input"); i.type = "text"; i.value = value || ""; i.autocomplete = "off"; if (ph) i.placeholder = ph; i.id = id; return i; };
  const area = (value, id) => { const a = h("textarea"); a.rows = 3; a.value = value || ""; a.id = id; return a; };
  const groupSelect = (p, value, id) => {
    const s = h("select"); s.id = id;
    s.append(Object.assign(h("option", "", T("tk.group.none")), { value: "" }), ...(p.groups || []).map(g => Object.assign(h("option", "", g.title), { value: g.id })));
    s.value = (p.groups || []).some(g => g.id === value) ? value : "";
    return s;
  };

  function addForm(p, parent) {
    const project = p.path, key = parent || "top-" + project;
    const title = input("", "tk-new-title", T("tk.add.ph")), desc = area("", "tk-new-desc"), group = groupSelect(p, "", "tk-new-g-" + key);
    const form = h("form", "tk-form", field(T("tk.f.title"), title, "tk-new-t-" + key), field(T("tk.f.desc"), desc, "tk-new-d-" + key),
      parent || !(p.groups || []).length ? null : field(T("tk.f.group"), group, "tk-new-gf-" + key),
      h("div", "tk-acts", btn(T("tk.add.go"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.adding = ""; paint(); })));
    form.querySelector(".primary").type = "submit";
    form.onsubmit = e => { e.preventDefault(); if (!title.value.trim()) { say(T("tk.e.title"), true); paint(); return; } act("add", { project, parent, title: title.value, description: desc.value, ...(group.value ? { group: group.value } : {}) }, T("tk.added")); };
    return form;
  }

  function editForm(p, t) {
    const project = p.path;
    const title = input(t.title, "tk-e-title-" + t.id), desc = area(t.description, "tk-e-desc-" + t.id), done = area(t.done, "tk-e-done-" + t.id), missing = area(t.missing, "tk-e-miss-" + t.id);
    const notes = Object.fromEntries(NOTES.map(k => [k, area((t.notes || {})[k], `tk-e-${k}-${t.id}`)]));
    const state = h("select"); state.id = "tk-e-state-" + t.id;
    EDIT_STATES.forEach(s => state.append(Object.assign(h("option", "", T("tk.state." + s)), { value: s })));
    state.value = t.state;
    const group = groupSelect(p, t.group, "tk-e-group-" + t.id);
    const planned = !!t.plan;
    const form = h("form", "tk-form", field(T("tk.f.title"), title, "tk-e-t-" + t.id), field(T("tk.f.desc"), desc, "tk-e-d-" + t.id),
      planned ? null : field(T("tk.f.state"), state, "tk-e-s-" + t.id), (p.groups || []).length && !t.parent ? field(T("tk.f.group"), group, "tk-e-g-" + t.id) : null,
      planned ? null : field(T("tk.f.done"), done, "tk-e-dn-" + t.id), planned ? null : field(T("tk.f.missing"), missing, "tk-e-m-" + t.id),
      ...NOTES.map(k => field(T("tk.notes." + k), notes[k], `tk-e-n-${k}-${t.id}`)),
      h("div", "tk-acts", btn(T("tk.save"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.editing = ""; paint(); })));
    form.querySelector(".primary").type = "submit";
    form.onsubmit = e => {
      e.preventDefault();
      act("update", { project, id: t.id, title: title.value, description: desc.value, ...(planned ? {} : { state: state.value, done: done.value, missing: missing.value }),
        ...((p.groups || []).length && !t.parent ? { group: group.value } : {}), ...Object.fromEntries(NOTES.map(k => [k, notes[k].value])) }, T("tk.saved"));
    };
    return form;
  }

  const noteBlocks = t => NOTES.filter(k => lines((t.notes || {})[k]).length).map(k => h("div", "tk-notes", h("b", "", T("tk.notes." + k)), h("ul", "", lines(t.notes[k]).map(x => h("li", "", x)))));

  function confirmBox(p, t, name) {
    return h("div", "tk-ask", h("p", "", t.children ? T("tk.delete.ask.n", name, t.children) : T("tk.delete.ask", name)),
      h("div", "tk-acts", btn(T("tk.delete.yes"), "tk-danger", () => act("delete", { project: p.path, id: t.id }, T("tk.deleted"))), btn(T("tk.cancel"), "ghost", () => { S.confirm = ""; paint(); })));
  }

  /* one open task in the tree */
  function item(p, t, sibs) {
    const li = h("li", "tk-item"); li.dataset.id = t.id; li.dataset.state = t.state; li.style.setProperty("--d", String(Math.min(t.depth, 6)));
    if (t.hidden) li.dataset.hidden = "1";
    const name = t.plan ? T("tk.plan.title", t.plan.title) : t.title;
    li.append(h("div", "tk-head", t.num ? h("span", "tk-num", "#" + t.num) : null, h("b", "tk-name", name), h("span", "tk-badge tk-s-" + t.state, icon(t.state), h("span", "", T("tk.state." + t.state))),
      t.plan ? h("span", "tk-badge", T("tk.plan")) : null, t.hidden ? h("span", "tk-badge tk-hiddenbadge", T(t.hidden_by_parent ? "tk.hidden.parent" : "tk.hidden.badge")) : null));
    if (t.state === "waiting") li.append(h("p", "tk-wait", T("tk.waits")));
    if (t.meta && t.meta.assigned_by) li.append(h("p", "setnote tk-meta", T("tk.assigned", t.meta.assigned_by.name)));
    if (t.description && !t.plan) li.append(h("p", "tk-text", t.description));
    if (t.done) li.append(h("p", "tk-note", h("b", "", T("tk.f.done") + ": "), t.done));
    if (t.missing) li.append(h("p", "tk-note", h("b", "", T("tk.f.missing") + ": "), t.missing));
    li.append(...noteBlocks(t));
    li.append(h("p", "setnote tk-meta", T(t.by === "user" ? "tk.by.user" : "tk.by.agent", when(t.updated)), (t.links.files || []).length ? " " + T("tk.files", t.links.files.join(", ")) : ""));
    const idx = sibs.findIndex(x => x.id === t.id);
    const acts = h("div", "tk-acts", btn(T(t.plan ? "tk.resume.plan" : "tk.resume"), "primary", () => resume(p.path, t)),
      t.plan ? null : btn(T("tk.done"), "tk-donebtn", () => act("close", { project: p.path, id: t.id }, T("tk.closed.ok"))),
      btn(T("tk.edit"), "", () => { S.editing = S.editing === t.id ? "" : t.id; S.confirm = ""; paint(); }));
    const more = h("details", "tk-more", h("summary", "", T("tk.more")), h("div", "tk-acts",
      t.plan ? null : btn(T("tk.sub.add"), "", () => { S.adding = S.adding === t.id ? "" : t.id; paint(); }),
      t.hidden_by_parent ? null : btn(T(t.hidden ? "tk.show" : "tk.hide"), "tk-hidebtn", () => act(t.hidden ? "show" : "hide", { project: p.path, id: t.id }, T(t.hidden ? "tk.shown.ok" : "tk.hidden.ok"))),
      btn(T("tk.up"), "", () => idx > 0 && act("move", { project: p.path, id: t.id, before: sibs[idx - 1].id })),
      btn(T("tk.down"), "", () => idx < sibs.length - 1 && act("move", { project: p.path, id: t.id, before: sibs[idx + 2] ? sibs[idx + 2].id : "", ...(sibs[idx + 2] ? {} : { parent: t.parent }) })),
      btn(T("tk.delete"), "ghost", () => { S.confirm = S.confirm === t.id ? "" : t.id; paint(); })));
    li.append(acts, more);
    if (S.editing === t.id) li.append(editForm(p, t));
    if (S.adding === t.id) li.append(addForm(p, t.id));
    if (S.confirm === t.id) li.append(confirmBox(p, t, name));
    return li;
  }

  /* ---- the tree view: the open tasks, by group ---- */
  function groupHead(p, g) {
    const head = h("div", "tk-ghead", h("h4", "tk-gname", g.title), h("span", "tk-gcount", T("tk.group.count", g.open, g.closed)));
    if (g.id) {
      head.append(h("details", "tk-more tk-gmore", h("summary", "", T("tk.more")), h("div", "tk-acts",
        btn(T("tk.group.rename"), "", () => { S.groupEdit = S.groupEdit === g.id ? "" : g.id; paint(); }),
        btn(T("tk.group.delete"), "ghost", () => act("group-delete", { project: p.path, id: g.id }, T("tk.group.deleted"))))));
    }
    const out = [head];
    if (g.id && S.groupEdit === g.id) {
      const name = input(g.title, "tk-g-name-" + g.id);
      const f = h("form", "tk-form", field(T("tk.f.title"), name, "tk-g-n-" + g.id), h("div", "tk-acts", btn(T("tk.save"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.groupEdit = ""; paint(); })));
      f.querySelector(".primary").type = "submit";
      f.onsubmit = e => { e.preventDefault(); act("group-rename", { project: p.path, id: g.id, title: name.value }, T("tk.saved")); };
      out.push(f);
    }
    return out;
  }

  function treeBody(p) {
    const groups = p.groups || [];
    const blocks = [];
    p.tasks.forEach(t => { if (t.depth === 0) blocks.push({ top: t, rows: [t] }); else if (blocks.length) blocks[blocks.length - 1].rows.push(t); });
    const sections = [{ g: { id: "", title: T("tk.group.none"), open: 0, closed: "" }, blocks: blocks.filter(b => !groups.some(g => g.id === b.top.group)), loose: true },
      ...groups.map(g => ({ g, blocks: blocks.filter(b => b.top.group === g.id) }))];
    const out = [];
    sections.forEach(sec => {
      if (sec.loose && !sec.blocks.length) return;
      if (groups.length) out.push(...groupHead(p, sec.loose ? { ...sec.g, open: sec.blocks.length } : sec.g));
      if (!sec.blocks.length) { out.push(h("p", "dim tk-gempty", T("tk.group.empty"))); return; }
      const ul = h("ul", "tk-list");
      sec.blocks.forEach(b => b.rows.forEach(t => ul.append(item(p, t, p.tasks.filter(x => x.parent === t.parent)))));
      out.push(ul);
    });
    return out;
  }

  /* ---- the spine view: the nodes in order, open and closed, with the notes of the one chosen ---- */
  function nodeRows(p) {
    const closed = ((S.hist || {})[p.path] || []).filter(r => !r.parent).map(r => ({ ...r, closedRow: true }));
    const open = p.tasks.filter(t => t.depth === 0);
    const order = g => { const i = (p.groups || []).findIndex(x => x.id === g); return i < 0 ? 999 : i; };
    return [...open, ...closed].sort((a, b) => order(a.group) - order(b.group) || (a.num || 1e6) - (b.num || 1e6) || (a.closed || 0) - (b.closed || 0));
  }

  function nodePanel(p, n) {
    const subs = n.closedRow ? ((S.hist || {})[p.path] || []).filter(r => r.parent === n.id) : p.tasks.filter(x => x.parent === n.id);
    const panel = h("aside", "tk-panel"); panel.dataset.id = n.id;
    panel.append(h("div", "tk-head", n.num ? h("span", "tk-num", "#" + n.num) : null, h("b", "tk-name", n.title),
      h("span", "tk-badge tk-s-" + (n.closedRow ? "done" : n.state), icon(n.closedRow ? "done" : n.state), h("span", "", stateLabel(n.state, n.closedRow)))));
    if (n.description) panel.append(h("p", "tk-text", n.description));
    if (n.done) panel.append(h("p", "tk-note", h("b", "", T("tk.f.done") + ": "), n.done));
    if (n.missing) panel.append(h("p", "tk-note", h("b", "", T("tk.f.missing") + ": "), n.missing));
    const blocks = noteBlocks(n);
    panel.append(...(blocks.length ? blocks : [h("p", "dim", T("tk.notes.none"))]));
    if (subs.length) panel.append(h("div", "tk-notes", h("b", "", T("tk.subtasks")), h("ul", "", subs.map(s => h("li", "", s.title + " (" + stateLabel(s.state, n.closedRow) + ")")))));
    if (n.closedRow) panel.append(h("p", "setnote tk-meta", T("tk.closed.on", when(n.closed))));
    const acts = h("div", "tk-acts");
    if (n.closedRow) acts.append(btn(T("tk.reopen"), "primary", () => act("reopen", { project: p.path, id: n.id }, T("tk.reopened"))));
    else {
      acts.append(btn(T("tk.done"), "primary", () => act("close", { project: p.path, id: n.id }, T("tk.closed.ok"))),
        btn(T("tk.edit"), "", () => { S.view = "tree"; remember(); S.editing = n.id; refresh(); }),
        n.hidden_by_parent ? null : btn(T(n.hidden ? "tk.show" : "tk.hide"), "tk-hidebtn", () => act(n.hidden ? "show" : "hide", { project: p.path, id: n.id }, T(n.hidden ? "tk.shown.ok" : "tk.hidden.ok"))));
    }
    acts.append(btn(T("tk.delete"), "ghost", () => { S.confirm = S.confirm === n.id ? "" : n.id; paint(); }));
    panel.append(acts);
    if (S.confirm === n.id) panel.append(confirmBox(p, { ...n, children: subs.length }, n.title));
    return panel;
  }

  function spineBody(p) {
    const rows = nodeRows(p);
    if (!rows.length) return [h("p", "dim", T("tk.none"))];
    const spine = h("div", "tk-spine"); spine.setAttribute("role", "list");
    let last = null;
    rows.forEach(n => {
      if ((p.groups || []).length && n.group !== last) {
        const g = (p.groups || []).find(x => x.id === n.group);
        spine.append(h("div", "tk-sep", h("span", "", g ? g.title : T("tk.group.none"))));
        last = n.group;
      }
      const st = n.closedRow ? "done" : n.state;
      const b = h("button", "tk-node" + (S.node === n.id ? " sel" : ""), h("span", "tk-nnum", n.num ? String(n.num) : "·"),
        h("span", "tk-nbody", h("span", "tk-ntitle", n.title), h("span", "tk-nsum", n.description || "")), h("span", "tk-badge tk-s-" + st, icon(st), h("span", "", stateLabel(n.state, n.closedRow))),
        n.hidden ? h("span", "tk-badge tk-hiddenbadge", T("tk.hidden.badge")) : null);
      b.type = "button"; b.dataset.id = n.id; b.dataset.state = st; b.setAttribute("role", "listitem"); b.setAttribute("aria-pressed", S.node === n.id ? "true" : "false");
      b.onclick = () => { S.node = S.node === n.id ? "" : n.id; S.confirm = ""; paint(); };
      spine.append(b);
    });
    const chosen = rows.find(n => n.id === S.node);
    return [h("div", "tk-spinewrap", spine, chosen ? nodePanel(p, chosen) : h("aside", "tk-panel tk-panel-empty", h("p", "dim", T("tk.spine.pick"))))];
  }

  /* ---- the history view: what was closed ---- */
  function historyBody(p) {
    const rows = (S.hist || {})[p.path] || [];
    if (!rows.length) return [h("p", "dim", T("tk.history.none"))];
    const ul = h("ul", "tk-list");
    rows.forEach(r => {
      const li = h("li", "tk-item tk-closedrow"); li.dataset.id = r.id; li.style.setProperty("--d", r.parent ? "1" : "0");
      li.append(h("div", "tk-head", r.num ? h("span", "tk-num", "#" + r.num) : null, h("b", "tk-name", r.title), h("span", "tk-badge tk-s-done", icon("done"), h("span", "", T("tk.closed")))),
        h("p", "setnote tk-meta", T("tk.closed.on", when(r.closed))));
      if (r.description) li.append(h("p", "tk-text", r.description));
      li.append(...noteBlocks(r));
      li.append(h("div", "tk-acts", btn(T("tk.reopen"), "primary", () => act("reopen", { project: p.path, id: r.id }, T("tk.reopened"))),
        btn(T("tk.delete"), "ghost", () => { S.confirm = S.confirm === r.id ? "" : r.id; paint(); })));
      if (S.confirm === r.id) li.append(confirmBox(p, { ...r, children: 0 }, r.title));
      ul.append(li);
    });
    return [ul];
  }

  function projectBlock(p) {
    const sec = h("section", "tk-sec");
    sec.dataset.project = p.path;
    const count = S.view === "history" ? ((S.hist || {})[p.path] || []).length : p.tasks.length;
    const roadmap = h("details", "tk-more tk-roadmap", h("summary", "", T("tk.roadmap")), h("div", "tk-acts",
      btn(T("tk.roadmap.write"), "tk-rm-write", () => act("roadmap-write", { project: p.path }, T("tk.roadmap.written"))),
      btn(T("tk.roadmap.load"), "tk-rm-load", () => { S.confirm = S.confirm === "load-" + p.path ? "" : "load-" + p.path; paint(); })));
    sec.append(h("div", "tk-sechead", h("h3", "", `${p.name} (${count})`), h("div", "tk-acts", S.view === "history" ? null : btn(T("tk.add"), "", () => { S.adding = S.adding === "top-" + p.path ? "" : "top-" + p.path; paint(); }),
      S.view === "tree" ? btn(T("tk.group.add"), "tk-gadd", () => { S.groupAdd = S.groupAdd === p.path ? "" : p.path; paint(); }) : null)), roadmap);
    if (S.confirm === "load-" + p.path) {
      sec.append(h("div", "tk-ask", h("p", "", T("tk.roadmap.load.ask")), h("div", "tk-acts", btn(T("tk.roadmap.load.yes"), "primary", () => act("roadmap-load", { project: p.path }, T("tk.roadmap.loaded"))), btn(T("tk.cancel"), "ghost", () => { S.confirm = ""; paint(); }))));
    }
    if (S.groupAdd === p.path) {
      const name = input("", "tk-g-new");
      const f = h("form", "tk-form", field(T("tk.f.title"), name, "tk-g-new-f"), h("div", "tk-acts", btn(T("tk.add.go"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.groupAdd = ""; paint(); })));
      f.querySelector(".primary").type = "submit";
      f.onsubmit = e => { e.preventDefault(); if (!name.value.trim()) { say(T("tk.e.title"), true); paint(); return; } act("group-add", { project: p.path, title: name.value }, T("tk.group.added")); };
      sec.append(f);
    }
    if (S.adding === "top-" + p.path) sec.append(addForm(p, ""));
    if (S.view === "history") sec.append(...historyBody(p));
    else if (!p.tasks.length && S.view === "tree") sec.append(h("p", "dim", S.session ? T("tk.session.none") : T("tk.none")));
    else sec.append(...(S.view === "spine" ? spineBody(p) : treeBody(p)));
    return sec;
  }

  function paint() {
    if (!S.loaded) { root.replaceChildren(h("p", "dim", T("tk.loading"))); return; }
    const d = S.data || { projects: [], counts: {}, waiting: 0, hidden: 0 };
    const msg = h("p", "setnote tk-msg", S.msg); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite"); msg.dataset.bad = S.bad ? "1" : ""; msg.hidden = !S.msg;
    const proj = h("select"); proj.id = "tk-project"; proj.append(Object.assign(h("option", "", T("tk.all")), { value: "all" }));
    (S.projectsAll || d.projects).forEach(p => proj.append(Object.assign(h("option", "", p.name), { value: p.path })));
    proj.value = S.project; proj.onchange = () => { S.project = proj.value; refresh(); };
    const st = h("select"); st.id = "tk-state"; st.append(Object.assign(h("option", "", T("tk.any")), { value: "" }));
    FILTER_STATES.forEach(s => st.append(Object.assign(h("option", "", `${T("tk.state." + s)} (${(d.counts || {})[s] || 0})`), { value: s })));
    st.value = S.state; st.onchange = () => { S.state = st.value; refresh(); };
    const banner = d.waiting ? h("p", "tk-banner", icon("waiting"), h("span", "", T("tk.banner", d.waiting))) : null;
    const views = h("div", "tk-views"); views.setAttribute("role", "group"); views.setAttribute("aria-label", T("tk.view"));
    ["tree", "spine", "history"].forEach(v => {
      const b = btn(T("tk.view." + v), "tk-view" + (S.view === v ? " on" : ""), () => { S.view = v; S.editing = S.adding = S.confirm = ""; remember(); refresh(); });
      b.id = "tk-view-" + v; b.setAttribute("aria-pressed", S.view === v ? "true" : "false");
      views.append(b);
    });
    const hid = h("input"); hid.type = "checkbox"; hid.id = "tk-hidden"; hid.checked = S.hidden; hid.onchange = () => { S.hidden = hid.checked; remember(); refresh(); };
    const hiddenBox = h("label", "tk-check", hid, h("span", "", T("tk.hidden.show", d.hidden || 0)));
    const chip = S.session ? h("p", "tk-chip", h("span", "", T("tk.session.chip")), btn(T("tk.session.clear"), "ghost tk-chip-x", () => { S.session = ""; refresh(); })) : null;
    if (chip) chip.id = "tk-session";
    root.replaceChildren(...[h("p", "setnote tk-intro", T("tk.intro")), banner, views,
      h("div", "tk-filters", field(T("tk.f.project"), proj, "tk-project"), field(T("tk.f.state"), st, "tk-state"), hiddenBox), chip, msg,
      ...(d.projects.length ? d.projects.map(projectBlock) : [h("p", "dim", T("tk.noprojects"))])].filter(Boolean));
  }

  /* the Chat's button: only the tasks of the open conversation */
  function showSession(sid) {
    S.session = sid || ""; S.project = "all"; S.state = ""; S.view = "tree"; S.editing = S.adding = S.confirm = "";
    const tab = document.getElementById("tab-tasks");
    if (tab) tab.click();
    S.loaded = false; paint(); refresh();
  }

  function poll() { clearTimeout(timer); timer = setTimeout(async () => { if (active() && !document.hidden) await refresh(true); poll(); }, POLL_MS); }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "tasks") { S.loaded = false; paint(); refresh(); } });
  document.addEventListener("tadamoo:lang", () => { if (S.loaded) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) refresh(true); });
  poll();
  if (active()) refresh();
  window.tasksUi = { refresh, showSession };
})();
