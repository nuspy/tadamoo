"use strict";
/* What the plans ask the person, as a window over whatever tab is open. The server lists every request that is asking (/api/plans/human); the ones of one plan
   are ONE scrolling list, all of them at once: under a heading when the plan groups them, each question large with its explanation under it and its field.
   The field follows the answer: two possible answers (yes or no, or two choices) are two buttons with the answers in words; more than two are a menu with
   "Other, I write" that opens a text field; a free text or a number is the field. A step for the person is its description, a note and "I did it" / "I can't now"
   (which brings it back after 10 minutes). What a plan proposed, or what was answered the last time, is already in each field. At the bottom, always in sight, one
   "Send all answers" with how many are not answered yet; choosing an answer never sends anything. Later leaves the plan's questions for 10 minutes, with the
   answers kept. Everything the server says is put in the page as text. */
(function (root) {
  const SNOOZE_MS = 10 * 60 * 1000, EVERY_MS = 4000;

  /* ---- the pure parts ---- */
  const lang = () => (typeof LANG === "string" ? LANG : "en");
  /* a text in the person's language, the other one when that is missing */
  const say = (r, name, l) => ((l === "en" ? r[name + "_en"] || r[name] : r[name] || r[name + "_en"]) || "");
  const labelOf = (c, l) => (l === "en" ? c.label_en || c.label : c.label || c.label_en) || c.value;
  const twoWays = r => r.ask === "approve" || (r.ask === "value" && r.kind === "choice" && r.choices.length === 2);
  const isTask = r => r.ask === "task";

  /* the first plan that has requests asking and has not been left for later: {key, plan_id, plan_title, items}, ALL its requests in the order of the plan; a task left for later is not in it */
  function pickGroup(list, snoozed, now) {
    const held = r => (snoozed || {})[r.plan_id] > now || (snoozed || {})[`${r.plan_id}/${r.node_id}`] > now;
    const rows = (Array.isArray(list) ? list : []).filter(r => !held(r));
    if (!rows.length) return null;
    const first = rows[0];
    const items = rows.filter(r => r.plan_id === first.plan_id).sort((a, b) => (a.index || 0) - (b.index || 0));
    return { key: first.plan_id, plan_id: first.plan_id, plan_title: first.plan_title, items };
  }
  const idsOf = items => items.map(r => r.node_id).join("|");

  /* the headings of the list: each item, with the heading to put before it when the plan groups them and the group changes */
  function sections(items, l) {
    let before = null;
    return items.map(r => {
      const heading = say(r, "group", l);
      const out = { item: r, heading: heading && heading !== before ? heading : "" };
      if (heading) before = heading;
      return out;
    });
  }

  /* a menu of more than two choices that the person may answer in words: the answer is not one of the choices */
  const writing = (r, value) => r.other && value !== undefined && value !== "" && !r.choices.some(c => c.value === String(value));

  /* what is in each field at first: what the person typed before, else what the plan proposes */
  function startAnswers(items, before) {
    const out = {};
    items.forEach(r => {
      if (before && before[r.node_id] !== undefined) out[r.node_id] = before[r.node_id];
      else if (r.proposed !== null && r.proposed !== undefined) out[r.node_id] = r.proposed;
    });
    return out;
  }
  /* the answer checked for its question: {value} or {error: a word of the page}. A task is {state: "done" | "later", note}. */
  function check(r, value, otherOn) {
    if (isTask(r)) return value && (value.state === "done" || value.state === "later") ? { value } : { error: "hr.need.task" };
    if (r.ask === "approve") return typeof value === "boolean" ? { value } : { error: "hr.need.yesno" };
    const t = String(value == null ? "" : value).trim();
    if (r.kind === "choice") {
      if (r.choices.some(c => c.value === t)) return { value: t };
      if (r.other && t) return { value: t };
      return { error: r.choices.length === 2 ? "hr.need.pick2" : r.other ? (otherOn ? "hr.need" : "hr.need.pick") : "hr.need.choice" };
    }
    if (!t) return { error: "hr.need" };
    if (r.kind === "number" && !Number.isFinite(Number(t))) return { error: "hr.need.number" };
    return { value: t };
  }
  /* the form: all the questions of a plan and the answers so far */
  function newForm(items, before) {
    const answers = startAnswers(items, before);
    const other = {};
    items.forEach(r => { if (writing(r, answers[r.node_id])) other[r.node_id] = true; });
    return { items, answers, other };
  }
  const problem = (w, r) => check(r, w.answers[r.node_id], !!w.other[r.node_id]).error || "";
  /* the ids of the questions that have no good answer yet */
  const unanswered = w => w.items.filter(r => problem(w, r)).map(r => r.node_id);
  /* the open form when the questions that are asking changed under it: the answers given stay */
  const reconcile = (w, items) => newForm(items, w.answers);
  /* what is sent, together: step id -> the answer; a task is {done: true, note}, and a task that cannot be done now is left out */
  function payload(w) {
    const out = {};
    w.items.forEach(r => {
      const v = w.answers[r.node_id];
      if (isTask(r)) { if (v && v.state === "done") out[r.node_id] = { done: true, note: String(v.note || "").trim() }; } else out[r.node_id] = typeof v === "string" ? v.trim() : v;
    });
    return out;
  }
  const notNow = w => w.items.filter(r => isTask(r) && (w.answers[r.node_id] || {}).state === "later").map(r => r.node_id);

  const api = { pickGroup, idsOf, sections, startAnswers, check, newForm, problem, unanswered, reconcile, payload, notNow, say, labelOf, twoWays, writing, SNOOZE_MS };
  root.HumanAsk = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  /* ---- the window ---- */
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const button = (text, cls, onclick) => { const b = h("button", "btn hr-btn" + (cls ? " " + cls : ""), text); b.type = "button"; b.addEventListener("click", onclick); return b; };

  let list = [], timer = 0, group = null, form = null, sending = false, failed = "", scrolled = 0;
  const snoozed = {}, drafts = {}, errors = {}, sectionsOf = {};
  const dlg = h("dialog", "hr-dialog");
  dlg.id = "hr-card"; dlg.setAttribute("aria-labelledby", "hr-card-h");
  document.body.append(dlg);

  async function call(method, endpoint, body) {
    try {
      const r = await fetch(endpoint, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  function later() { snoozed[group.key] = Date.now() + SNOOZE_MS; drafts[group.key] = form; group = form = null; failed = ""; show(); }

  /* the line at the bottom: how many are not answered yet */
  function footer() {
    const n = form ? unanswered(form).length : 0;
    const left = dlg.querySelector(".hr-left"), send_ = dlg.querySelector(".hr-send");
    if (left) { left.textContent = n ? T("hr.left", n) : T("hr.left.none"); left.dataset.n = String(n); }
    if (send_) send_.disabled = sending;
  }

  async function send() {
    const open = unanswered(form);
    if (open.length) {                                              // the ones that are not answered are shown, and the first is brought into sight
      open.forEach(id => { errors[id] = T(problem(form, form.items.find(r => r.node_id === id))); refreshItem(id); });
      failed = T("hr.left", open.length); footer();
      const first = sectionsOf[open[0]];
      if (first) { first.scrollIntoView({ block: "center" }); const f = first.querySelector("input, select, textarea, button.hr-choice"); if (f) f.focus(); }
      return;
    }
    const body = payload(form), notNowIds = notNow(form), planId = group.plan_id;
    notNowIds.forEach(id => { snoozed[`${planId}/${id}`] = Date.now() + SNOOZE_MS; });      // what cannot be done now comes back in 10 minutes
    if (!Object.keys(body).length) { delete drafts[group.key]; group = form = null; show(); return; }
    sending = true; failed = ""; footer(); setNote();
    const res = await call("POST", `api/plans/${encodeURIComponent(planId)}/events`, { type: "plan:answers", answers: body, actor: "user" });
    sending = false;
    if (res.ok && res.data.ok !== false) { delete drafts[group.key]; group = form = null; await refresh(); return; }
    notNowIds.forEach(id => { delete snoozed[`${planId}/${id}`]; });
    failed = T("hr.err", (res.data && (res.data.error && res.data.error.message || res.data.error)) || res.status || "");
    footer(); setNote();
    refresh();                                                      // what is asking may have changed: the list follows it
  }
  function setNote() { const e = dlg.querySelector(".hr-note-line"); if (e) { e.textContent = sending ? T("hr.sending") : failed; e.hidden = !e.textContent; } }

  /* two buttons with the two answers in words; choosing one does not send anything */
  function twoButtons(r, l, on, set) {
    const pairs = r.ask === "approve" ? [[T("hr.yes"), true], [T("hr.no"), false]] : r.choices.map(c => [labelOf(c, l), c.value]);
    const made = pairs.map(([label, value]) => {
      const b = button(label, "hr-choice" + (on === value ? " on" : ""), () => { set(value); delete errors[r.node_id]; refreshItem(r.node_id, true); });
      b.setAttribute("aria-pressed", String(on === value));
      return b;
    });
    return h("div", "hr-yesno", made);
  }

  /* the field of a question */
  function field(r, l) {
    const value = form.answers[r.node_id], id = "hr-f-" + r.node_id;
    const keep = v => { form.answers[r.node_id] = v; delete errors[r.node_id]; const e = sectionsOf[r.node_id] && sectionsOf[r.node_id].querySelector(".hr-err"); if (e) e.hidden = true; footer(); };
    if (isTask(r)) {
      const v = value || {};
      const note = h("textarea", "hr-field hr-note"); note.id = id; note.rows = 2; note.value = v.note || ""; note.maxLength = 4000; note.setAttribute("aria-label", T("hr.note")); note.placeholder = T("hr.note");
      note.addEventListener("input", () => { form.answers[r.node_id] = { ...(form.answers[r.node_id] || {}), note: note.value }; });
      const pick = state => { form.answers[r.node_id] = { ...(form.answers[r.node_id] || {}), state, note: note.value }; delete errors[r.node_id]; refreshItem(r.node_id, true); };
      const done = button(T("hr.done"), "hr-choice" + (v.state === "done" ? " on" : ""), () => pick("done"));
      const cant = button(T("hr.cant"), "hr-choice" + (v.state === "later" ? " on" : ""), () => pick("later"));
      done.setAttribute("aria-pressed", String(v.state === "done")); cant.setAttribute("aria-pressed", String(v.state === "later"));
      return h("div", "hr-task", note, h("div", "hr-yesno", done, cant), v.state === "later" ? h("p", "hr-helpline", T("hr.task.later.help")) : null);
    }
    if (r.ask === "approve") return twoButtons(r, l, value, x => { form.answers[r.node_id] = x; footer(); });
    if (r.kind === "choice" && r.choices.length === 2) return twoButtons(r, l, value, x => { form.answers[r.node_id] = x; footer(); });
    if (r.kind === "choice") {
      const other = !!form.other[r.node_id];
      const menu = h("select", "hr-field"); menu.id = id;
      menu.append(Object.assign(h("option", "", T("hr.choice")), { value: "" }));
      r.choices.forEach(c => menu.append(Object.assign(h("option", "", labelOf(c, l)), { value: c.value })));
      if (r.other) menu.append(Object.assign(h("option", "", T("hr.other")), { value: "__other__" }));
      menu.value = other ? "__other__" : (value === undefined ? "" : String(value));
      menu.addEventListener("change", () => {
        if (menu.value === "__other__") { form.other[r.node_id] = true; form.answers[r.node_id] = ""; refreshItem(r.node_id, true, true); }
        else { delete form.other[r.node_id]; keep(menu.value); if (other) refreshItem(r.node_id); }
        footer();
      });
      if (!other) return menu;
      const typed = h("input", "hr-field hr-other"); typed.type = "text"; typed.autocomplete = "off"; typed.maxLength = 4000;
      typed.value = value === undefined ? "" : String(value); typed.setAttribute("aria-label", T("hr.other.write")); typed.placeholder = T("hr.other.write");
      typed.addEventListener("input", () => keep(typed.value));
      return h("div", "hr-menu", menu, typed);
    }
    const input = h("input", "hr-field"); input.type = "text"; input.autocomplete = "off"; input.spellcheck = false; input.maxLength = 4000; input.id = id;
    if (r.kind === "number") input.inputMode = "decimal";
    input.value = value === undefined ? "" : String(value);
    input.addEventListener("input", () => keep(input.value));
    return input;
  }

  /* one question of the list */
  function item(r, number) {
    const l = lang(), sec = h("section", "hr-item");
    sec.dataset.id = r.node_id;
    const lab = h("label", "hr-q", say(r, "prompt", l));
    if (!twoWays(r) && !isTask(r)) lab.htmlFor = "hr-f-" + r.node_id;
    const head = h("div", "hr-qrow", h("span", "hr-n", String(number)), lab);
    sec.append(head);
    if (say(r, "help", l)) sec.append(h("p", "hr-helpline", say(r, "help", l)));
    if (r.content) sec.append(h("p", "hr-lab", T(isTask(r) ? "hr.task.what" : "hr.content")), Object.assign(h("pre", "hr-content", r.content), { tabIndex: 0 }));
    sec.append(field(r, l));
    const bad = errors[r.node_id];
    sec.append(Object.assign(h("p", "hr-err", bad || ""), { role: "alert", hidden: !bad }));
    return sec;
  }
  /* a question drawn again, in its place (a choice made, an Other opened); the rest of the list and its scroll stay as they are */
  function refreshItem(id, keepFocus, focusOther) {
    const old = sectionsOf[id];
    if (!old || !form) return;
    const index = form.items.findIndex(r => r.node_id === id);
    const fresh = item(form.items[index], index + 1);
    old.replaceWith(fresh); sectionsOf[id] = fresh;
    if (keepFocus) {
      const target = focusOther ? fresh.querySelector(".hr-other") : fresh.querySelector("button.hr-choice.on, select, input, textarea");
      if (target) target.focus({ preventScroll: true });
    }
    footer();
  }

  function draw() {
    if (!group || !form) return;
    const l = lang(), n = form.items.length;
    Object.keys(sectionsOf).forEach(k => delete sectionsOf[k]);
    const body = h("div", "hr-list");
    sections(form.items, l).forEach(({ item: r, heading }, i) => {
      if (heading) body.append(h("h3", "hr-group", heading));
      const sec = item(r, i + 1);
      sectionsOf[r.node_id] = sec;
      body.append(sec);
    });
    const head = h("header", "hr-head", Object.assign(h("h2", "", T("hr.title")), { id: "hr-card-h" }), h("p", "hr-plan", group.plan_title), h("p", "hr-count", n === 1 ? T("hr.count.one") : T("hr.count", n)));
    const foot = h("footer", "hr-foot",
      h("p", "hr-left"), Object.assign(h("p", "hr-err hr-note-line"), { hidden: true, role: "alert" }),
      h("div", "hr-nav", button(T("hr.later"), "hr-later", later), button(T("hr.send"), "primary hr-send", send)),
      h("p", "hr-helpline", T("hr.later.help")));
    dlg.replaceChildren(h("div", "hr-form", head, body, foot));
    body.scrollTop = scrolled; scrolled = 0;
    footer(); setNote();
  }

  function show() {
    const g = pickGroup(list, snoozed, Date.now());
    if (!g) { group = form = null; if (dlg.open) dlg.close(); return; }
    if (group && g.key === group.key) {                             // the same plan: the list goes on, following what is asking
      group = g;
      if (idsOf(g.items) !== idsOf(form.items)) { const body = dlg.querySelector(".hr-list"); scrolled = body ? body.scrollTop : 0; form = reconcile(form, g.items); draw(); }
      return;
    }
    group = g;
    const kept = drafts[g.key];
    form = newForm(g.items, kept ? kept.answers : null);
    failed = ""; Object.keys(errors).forEach(k => delete errors[k]);
    if (!dlg.open) dlg.showModal();
    draw();
    const first = dlg.querySelector(".hr-item .hr-field, .hr-item button.hr-choice");
    if (first) first.focus({ preventScroll: true });
  }

  /* Esc leaves the questions for later; Enter in a text field goes to the next field */
  dlg.addEventListener("cancel", e => { e.preventDefault(); if (group) later(); });
  dlg.addEventListener("keydown", e => {
    if (!form || sending || e.altKey || e.ctrlKey || e.metaKey || e.key !== "Enter") return;
    const t = e.target;
    if (!t || t.tagName !== "INPUT") return;
    e.preventDefault();
    const fields = [...dlg.querySelectorAll(".hr-item input, .hr-item select, .hr-item textarea")], at = fields.indexOf(t);
    const nextField = fields[at + 1];
    if (nextField) nextField.focus();
  });

  async function refresh() {
    clearTimeout(timer);
    if (document.visibilityState === "visible") {
      const res = await call("GET", "api/plans/human");
      if (res.ok && Array.isArray(res.data.requests)) { list = res.data.requests; show(); }
    }
    timer = setTimeout(refresh, EVERY_MS);
  }
  api.refresh = refresh;
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
  document.addEventListener("tadamoo:lang", () => { if (group) { const body = dlg.querySelector(".hr-list"); scrolled = body ? body.scrollTop : 0; draw(); } });
  setTimeout(refresh, 1200);
})(typeof window !== "undefined" ? window : globalThis);
