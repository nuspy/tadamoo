"use strict";
/* Settings > Memories > Lessons: what went wrong or worked in earlier tasks, found by the harness and kept in readable files (the project's own
   and the global ones). Each lesson can be corrected, pinned, deleted or promoted into the project's AGENTS.md (a preview first, then a
   confirmation). Everything shown enters the page as text. */
(function () {
  const host = document.getElementById("ls-root");
  if (!host) return;

  const ARM_MS = 4000;
  const RESULTS = ["done", "stalled", "abandoned", "stopped", "failed"];
  let S = null, folder = "", note = "", info = "", editing = "", armed = "", promo = null, armTimer = 0, stepsOn = true;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  async function refresh() {
    const r = await api("GET", "api/lessons?" + new URLSearchParams(folder ? { folder } : {}));
    if (!r.ok) { note = errText(r); paint(); return; }
    S = r.data; folder = S.folder;
    const n = await api("GET", "api/next-steps");
    if (n.ok) stepsOn = !!n.data.on;
    paint();
  }
  async function setSteps(on) {
    const r = await api("POST", "api/next-steps", { on });
    note = r.ok ? "" : errText(r);
    if (r.ok) stepsOn = !!r.data.on;
    paint();
  }
  async function act(action, body) {
    const r = await api("POST", "api/lessons/" + action, { folder, ...body });
    note = r.ok ? "" : errText(r);
    if (r.ok) info = "";
    await refresh();
    return r;
  }
  function arm(key) {
    armed = key; clearTimeout(armTimer);
    armTimer = setTimeout(() => { armed = ""; paint(); }, ARM_MS);
    paint();
  }

  /* ---- the folder and the summary ---- */
  function folderBar() {
    const bar = el("div", "ls-bar"), sel = el("select"), lab = el("label", "", T("ls.folder"));
    sel.id = "ls-folder"; lab.htmlFor = "ls-folder";
    S.folders.forEach(f => { const o = el("option", "", f.name); o.value = f.path; o.selected = f.path === S.folder; o.title = f.path; sel.append(o); });
    sel.onchange = () => { folder = sel.value; editing = ""; promo = null; refresh(); };
    bar.append(lab, sel, btn(T("ls.refresh"), "ghost", () => refresh()));
    return bar;
  }
  function summary() {
    const box = el("div", "ls-block"), o = S.outcomes;
    box.append(el("h3", "", T("ls.out.title")));
    if (!o.count) { box.append(el("p", "dim", T("ls.out.none"))); return box; }
    const counts = RESULTS.filter(k => o.by_result[k]).map(k => `${T("ls.result." + k)} ${o.by_result[k]}`).join(" · ");
    const lines = [T("ls.out.line", o.count, counts), T("ls.out.steps", o.avg_steps)];
    if (o.top_error_tools.length) lines.push(T("ls.out.errors", o.top_error_tools.map(([t, n]) => `${t} (${n})`).join(", ")));
    if (o.corrected) lines.push(T("ls.out.corrected", o.corrected));
    const list = el("ul", "ls-sum");
    lines.forEach(l => list.append(el("li", "", l)));
    box.append(list);
    return box;
  }

  /* ---- the card after a task, and the work that repeats ---- */
  function stepsSwitch() {
    const box = el("div", "ls-block"), row = el("label", "ls-check"), cb = el("input");
    cb.type = "checkbox"; cb.checked = stepsOn; cb.onchange = () => setSteps(cb.checked);
    const text = el("span", "");
    text.append(el("b", "", T("ls.steps.title")), el("span", "help", T("ls.steps.help")));
    row.append(cb, text);
    box.append(row);
    return box;
  }
  function repeatsBlock() {
    if (!S.repeats.length) return null;
    const box = el("div", "ls-block"), list = el("ul", "ls-sum");
    box.append(el("h3", "", T("ls.repeat.title")), el("p", "help", T("ls.repeat.help")));
    S.repeats.forEach(g => list.append(el("li", "", T("ls.repeat.line", g.request, g.count, g.steps_avg))));
    box.append(list);
    return box;
  }

  /* ---- one lesson ---- */
  function field(label, id, value, rows) {
    const wrap = el("div", "ls-field"), lab = el("label", "", label), area = el("textarea");
    area.id = id; lab.htmlFor = id; area.value = value; area.rows = rows; area.maxLength = 500;
    wrap.append(lab, area);
    return [wrap, area];
  }
  function editForm(l) {
    const form = el("form", "ls-edit");
    const [w1, a1] = field(T("ls.situation"), "ls-es-" + l.id, l.situation, 2);
    const [w2, a2] = field(T("ls.went"), "ls-ew-" + l.id, l.went_wrong, 3);
    const [w3, a3] = field(T("ls.fix"), "ls-ef-" + l.id, l.fix, 3);
    form.onsubmit = async e => {
      e.preventDefault();
      const r = await act("edit", { id: l.id, situation: a1.value, went_wrong: a2.value, fix: a3.value });
      if (r.ok) editing = "";
      paint();
    };
    const bar = el("div", "ls-bar2");
    bar.append(btn(T("ls.save"), "primary", () => form.requestSubmit()), btn(T("ls.cancel"), "ghost", () => { editing = ""; paint(); }));
    form.append(w1, w2, w3, bar);
    return form;
  }
  function promotion(l) {
    const box = el("div", "ls-promo");
    box.append(el("p", "help", T("ls.promote.help", promo.file)), el("pre", "ls-promo-text", promo.text));
    const bar = el("div", "ls-bar2");
    bar.append(btn(T("ls.promote.go"), "primary", async () => {
      const r = await api("POST", "api/lessons/append_agents", { folder, id: l.id, confirm: true });
      note = r.ok ? "" : errText(r);
      if (r.ok) info = T(r.data.already ? "ls.promote.already" : r.data.created ? "ls.promote.created" : "ls.promote.done", r.data.file);
      promo = null;
      await refresh();
    }), btn(T("ls.cancel"), "ghost", () => { promo = null; paint(); }));
    box.append(bar);
    return box;
  }
  function card(l) {
    const li = el("li", "ls-card" + (l.pinned ? " pinned" : ""));
    if (editing === l.id) { li.append(editForm(l)); return li; }
    const head = el("div", "ls-head");
    if (l.pinned) head.append(el("span", "ls-tag", T("ls.pinned")));
    head.append(el("span", "ls-meta", T("ls.confirmed", l.confirmed)), el("span", "ls-meta", l.date || ""));
    li.append(head, el("p", "ls-situation", l.situation));
    if (l.went_wrong) li.append(labelled(T("ls.went"), l.went_wrong));
    if (l.fix) li.append(labelled(T("ls.fix"), l.fix));
    const bar = el("div", "ls-bar2");
    bar.append(btn(T("ls.edit"), "ghost", () => { editing = l.id; promo = null; paint(); }));
    const pin = btn(l.pinned ? T("ls.unpin") : T("ls.pin"), "ghost", () => act("pin", { id: l.id, pinned: !l.pinned }));
    pin.setAttribute("aria-pressed", String(l.pinned));
    bar.append(pin);
    if (l.scope === "project") {
      bar.append(btn(T("ls.promote"), "ghost", async () => {
        const r = await api("POST", "api/lessons/promote", { folder, id: l.id });
        note = r.ok ? "" : errText(r);
        promo = r.ok ? { id: l.id, text: r.data.text, file: r.data.file } : null;
        paint();
      }));
    }
    const sure = armed === "d" + l.id;
    bar.append(btn(sure ? T("ls.delete.sure") : T("ls.delete"), sure ? "ls-warn" : "ghost", async () => {
      if (!sure) { arm("d" + l.id); return; }
      armed = ""; await act("delete", { id: l.id });
    }));
    li.append(bar);
    if (promo && promo.id === l.id) li.append(promotion(l));
    return li;
  }
  function labelled(label, text) {
    const p = el("p", "ls-line");
    p.append(el("b", "", label + ": "), document.createTextNode(text));
    return p;
  }

  /* ---- the two lists and the form to write one by hand ---- */
  function group(title, items, emptyKey) {
    const box = el("div", "ls-block");
    box.append(el("h3", "", title));
    if (!items.length) { box.append(el("p", "dim", T(emptyKey))); return box; }
    const list = el("ul", "ls-list");
    items.forEach(l => list.append(card(l)));
    box.append(list);
    return box;
  }
  function addForm() {
    const form = el("form", "ls-add"), scope = el("select"), sl = el("label", "", T("ls.add.scope"));
    scope.id = "ls-as"; sl.htmlFor = "ls-as";
    [["project", T("ls.add.project")], ["global", T("ls.add.global")]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; scope.append(o); });
    const [w1, a1] = field(T("ls.situation"), "ls-as-s", "", 2);
    const [w2, a2] = field(T("ls.went"), "ls-as-w", "", 2);
    const [w3, a3] = field(T("ls.fix"), "ls-as-f", "", 2);
    form.onsubmit = async e => {
      e.preventDefault();
      if (!a1.value.trim() || !(a2.value.trim() || a3.value.trim())) { note = T("ls.add.need"); paint(); return; }
      await act("add", { scope: scope.value, situation: a1.value, went_wrong: a2.value, fix: a3.value });
    };
    const row = el("div", "ls-field");
    row.append(sl, scope);
    form.append(row, w1, w2, w3, btn(T("ls.add.go"), "primary", () => form.requestSubmit()));
    const box = el("div", "ls-block");
    box.append(el("h3", "", T("ls.add.title")), form);
    return box;
  }

  function paint() {
    if (!S) { host.replaceChildren(el("p", "dim", note || T("ls.loading"))); return; }
    const focus = document.activeElement && document.activeElement.id, repeated = repeatsBlock();
    host.replaceChildren(el("p", "ls-intro", T("ls.intro")), ...(note ? [el("p", "ls-bad", note)] : []), ...(info ? [el("p", "ls-ok", info)] : []),
      stepsSwitch(), folderBar(), summary(), ...(repeated ? [repeated] : []), group(T("ls.project.title", S.project.length), S.project, "ls.empty"), group(T("ls.global.title", S.global.length), S.global, "ls.empty.global"), addForm());
    const again = focus && document.getElementById(focus);
    if (again && again !== document.activeElement) again.focus();
  }

  document.addEventListener("tadamoo:sub", e => { if (e.detail === "memories") refresh(); });
  document.addEventListener("tadamoo:lang", () => { if (S) paint(); });
})();
