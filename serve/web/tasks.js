"use strict";
/* Tasks: the work the assistant is carrying out now, for each project (the register of tadamoo/taskboard.py). It is not a history: a task that is done is deleted.
   The person can change anything here and the assistant reads the change; a task a plan commands is one line whose state and progress come from the plan.
   Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("tk-root");
  if (!root) return;
  const POLL_MS = 5000;
  const STATES = ["waiting", "running", "queued", "blocked"];
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
  const ICONS = { waiting: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v6M12 16.5v.5", running: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM10 8.5l6 3.5-6 3.5z",
    queued: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM12 7v5l3 2", blocked: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM6 6l12 12" };
  function icon(name) {
    const NS = "http://www.w3.org/2000/svg", s = document.createElementNS(NS, "svg"), p = document.createElementNS(NS, "path");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("class", "tk-ico"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false");
    p.setAttribute("d", ICONS[name]); p.setAttribute("fill", "none"); p.setAttribute("stroke", "currentColor"); p.setAttribute("stroke-width", "2");
    p.setAttribute("stroke-linecap", "round"); p.setAttribute("stroke-linejoin", "round");
    s.append(p);
    return s;
  }
  const S = { data: null, project: "all", state: "", msg: "", bad: false, editing: "", adding: "", confirm: "", loaded: false };
  let timer = 0;
  const say = (t, bad) => { S.msg = t || ""; S.bad = !!bad; };
  const active = () => document.body.dataset.view === "tasks";
  const typing = () => !!(S.editing || S.adding);

  async function refresh(quiet) {
    const q = `api/register?project=${encodeURIComponent(S.project)}${S.state ? "&state=" + S.state : ""}`;
    const r = await call("GET", q);
    if (!r.ok) { say(why(r), true); S.loaded = true; if (!quiet) paint(); return; }
    S.data = r.data; S.loaded = true;
    if (S.project === "all") S.projectsAll = r.data.projects.map(p => ({ name: p.name, path: p.path }));
    if (!quiet || !typing()) paint();
  }
  async function act(path, payload, okText) {
    const r = await call("POST", "api/register/" + path, payload);
    if (!r.ok) { say(why(r), true); paint(); return null; }
    say(okText || ""); S.editing = S.adding = S.confirm = "";
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

  function addForm(project, parent, label) {
    const key = parent || "top-" + project;
    const title = input("", "tk-new-title", T("tk.add.ph")), desc = area("", "tk-new-desc");
    const form = h("form", "tk-form", field(T("tk.f.title"), title, "tk-new-t-" + key), field(T("tk.f.desc"), desc, "tk-new-d-" + key),
      h("div", "tk-acts", btn(T("tk.add.go"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.adding = ""; paint(); })));
    form.querySelector(".primary").type = "submit";
    form.onsubmit = e => { e.preventDefault(); if (!title.value.trim()) { say(T("tk.e.title"), true); paint(); return; } act("add", { project, parent, title: title.value, description: desc.value }, T("tk.added")); };
    return form;
  }

  function editForm(project, t) {
    const title = input(t.title, "tk-e-title-" + t.id), desc = area(t.description, "tk-e-desc-" + t.id), done = area(t.done, "tk-e-done-" + t.id), missing = area(t.missing, "tk-e-miss-" + t.id);
    const state = h("select"); state.id = "tk-e-state-" + t.id;
    ["queued", "running", "waiting", "blocked"].forEach(s => state.append(Object.assign(h("option", "", T("tk.state." + s)), { value: s })));
    state.value = t.state;
    const planned = !!t.plan;
    const form = h("form", "tk-form", field(T("tk.f.title"), title, "tk-e-t-" + t.id), field(T("tk.f.desc"), desc, "tk-e-d-" + t.id),
      planned ? null : field(T("tk.f.state"), state, "tk-e-s-" + t.id), planned ? null : field(T("tk.f.done"), done, "tk-e-dn-" + t.id), planned ? null : field(T("tk.f.missing"), missing, "tk-e-m-" + t.id),
      h("div", "tk-acts", btn(T("tk.save"), "primary", () => {}), btn(T("tk.cancel"), "ghost", () => { S.editing = ""; paint(); })));
    form.querySelector(".primary").type = "submit";
    form.onsubmit = e => { e.preventDefault(); act("update", { project, id: t.id, title: title.value, description: desc.value, ...(planned ? {} : { state: state.value, done: done.value, missing: missing.value }) }, T("tk.saved")); };
    return form;
  }

  function item(p, t, sibs) {
    const li = h("li", "tk-item"); li.dataset.id = t.id; li.dataset.state = t.state; li.style.setProperty("--d", String(Math.min(t.depth, 6)));
    const name = t.plan ? T("tk.plan.title", t.plan.title) : t.title;
    li.append(h("div", "tk-head", h("b", "tk-name", name), h("span", "tk-badge tk-s-" + t.state, icon(t.state), h("span", "", T("tk.state." + t.state))),
      t.plan ? h("span", "tk-badge", T("tk.plan")) : null));
    if (t.state === "waiting") li.append(h("p", "tk-wait", T("tk.waits")));
    if (t.meta && t.meta.assigned_by) li.append(h("p", "setnote tk-meta", T("tk.assigned", t.meta.assigned_by.name)));
    if (t.description && !t.plan) li.append(h("p", "tk-text", t.description));
    if (t.done) li.append(h("p", "tk-note", h("b", "", T("tk.f.done") + ": "), t.done));
    if (t.missing) li.append(h("p", "tk-note", h("b", "", T("tk.f.missing") + ": "), t.missing));
    li.append(h("p", "setnote tk-meta", T(t.by === "user" ? "tk.by.user" : "tk.by.agent", when(t.updated)), (t.links.files || []).length ? " " + T("tk.files", t.links.files.join(", ")) : ""));
    const idx = sibs.findIndex(x => x.id === t.id);
    const acts = h("div", "tk-acts", btn(T(t.plan ? "tk.resume.plan" : "tk.resume"), "primary", () => resume(p.path, t)), btn(T("tk.edit"), "", () => { S.editing = S.editing === t.id ? "" : t.id; S.confirm = ""; paint(); }));
    const more = h("details", "tk-more", h("summary", "", T("tk.more")), h("div", "tk-acts",
      t.plan ? null : btn(T("tk.sub.add"), "", () => { S.adding = S.adding === t.id ? "" : t.id; paint(); }),
      btn(T("tk.up"), "", () => idx > 0 && act("move", { project: p.path, id: t.id, before: sibs[idx - 1].id })),
      btn(T("tk.down"), "", () => idx < sibs.length - 1 && act("move", { project: p.path, id: t.id, before: sibs[idx + 2] ? sibs[idx + 2].id : "", ...(sibs[idx + 2] ? {} : { parent: t.parent }) })),
      btn(T("tk.delete"), "ghost", () => { S.confirm = S.confirm === t.id ? "" : t.id; paint(); })));
    li.append(acts, more);
    if (S.editing === t.id) li.append(editForm(p.path, t));
    if (S.adding === t.id) li.append(addForm(p.path, t.id));
    if (S.confirm === t.id) {
      li.append(h("div", "tk-ask", h("p", "", t.children ? T("tk.delete.ask.n", name, t.children) : T("tk.delete.ask", name)),
        h("div", "tk-acts", btn(T("tk.delete.yes"), "tk-danger", () => act("delete", { project: p.path, id: t.id }, T("tk.deleted"))), btn(T("tk.cancel"), "ghost", () => { S.confirm = ""; paint(); }))));
    }
    return li;
  }

  function projectBlock(p) {
    const sec = h("section", "tk-sec");
    sec.dataset.project = p.path;
    sec.append(h("div", "tk-sechead", h("h3", "", `${p.name} (${p.tasks.length})`), btn(T("tk.add"), "", () => { S.adding = S.adding === "top-" + p.path ? "" : "top-" + p.path; paint(); })));
    if (S.adding === "top-" + p.path) sec.append(addForm(p.path, "", ""));
    if (!p.tasks.length) { sec.append(h("p", "dim", T("tk.none"))); return sec; }
    const ul = h("ul", "tk-list");
    p.tasks.forEach(t => ul.append(item(p, t, p.tasks.filter(x => x.parent === t.parent))));
    sec.append(ul);
    return sec;
  }

  function paint() {
    if (!S.loaded) { root.replaceChildren(h("p", "dim", T("tk.loading"))); return; }
    const d = S.data || { projects: [], counts: {}, waiting: 0 };
    const msg = h("p", "setnote tk-msg", S.msg); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite"); msg.dataset.bad = S.bad ? "1" : ""; msg.hidden = !S.msg;
    const proj = h("select"); proj.id = "tk-project"; proj.append(Object.assign(h("option", "", T("tk.all")), { value: "all" }));
    (S.projectsAll || d.projects).forEach(p => proj.append(Object.assign(h("option", "", p.name), { value: p.path })));
    proj.value = S.project; proj.onchange = () => { S.project = proj.value; refresh(); };
    const st = h("select"); st.id = "tk-state"; st.append(Object.assign(h("option", "", T("tk.any")), { value: "" }));
    STATES.forEach(s => st.append(Object.assign(h("option", "", `${T("tk.state." + s)} (${(d.counts || {})[s] || 0})`), { value: s })));
    st.value = S.state; st.onchange = () => { S.state = st.value; refresh(); };
    const banner = d.waiting ? h("p", "tk-banner", icon("waiting"), h("span", "", T("tk.banner", d.waiting))) : null;
    root.replaceChildren(...[h("p", "setnote tk-intro", T("tk.intro")), banner,
      h("div", "tk-filters", field(T("tk.f.project"), proj, "tk-project"), field(T("tk.f.state"), st, "tk-state")), msg,
      ...(d.projects.length ? d.projects.map(projectBlock) : [h("p", "dim", T("tk.noprojects"))])].filter(Boolean));
  }

  function poll() { clearTimeout(timer); timer = setTimeout(async () => { if (active() && !document.hidden) await refresh(true); poll(); }, POLL_MS); }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "tasks") { S.loaded = false; paint(); refresh(); } });
  document.addEventListener("tadamoo:lang", () => { if (S.loaded) paint(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && active()) refresh(true); });
  poll();
  if (active()) refresh();
  window.tasksUi = { refresh };
})();
