"use strict";
/* What the plans ask the person, as a window over whatever tab is open. The server lists every request that is asking (/api/plans/human); the ones of one plan
   are one path of steps: "Question 3 of 22", the question in the person's language with a line of explanation, its field (a menu, a text, a number, yes or no),
   Back and Next (and the left and right arrow keys), then a summary of all the answers where any can be changed with Edit, and one Send for all of them together.
   What a plan proposed, or what was answered the last time, is already in each field. Later leaves the plan's questions for 10 minutes, with the answers kept.
   Everything the server says is put in the page as text. */
(function (root) {
  const SNOOZE_MS = 10 * 60 * 1000, EVERY_MS = 4000;

  /* ---- the pure parts ---- */
  const lang = () => (typeof LANG === "string" ? LANG : "en");
  /* a text in the person's language, the other one when that is missing */
  const say = (r, name, l) => ((l === "en" ? r[name + "_en"] || r[name] : r[name] || r[name + "_en"]) || "");
  const labelOf = (c, l) => (l === "en" ? c.label_en || c.label : c.label || c.label_en) || c.value;

  /* the first plan that has requests asking and has not been left for later: {key, plan_id, plan_title, items}, the items in the order of the plan */
  function pickGroup(list, snoozed, now) {
    const rows = Array.isArray(list) ? list : [];
    for (const first of rows) {
      if ((snoozed || {})[first.plan_id] > now) continue;
      const items = rows.filter(r => r.plan_id === first.plan_id).sort((a, b) => (a.index || 0) - (b.index || 0));
      return { key: first.plan_id, plan_id: first.plan_id, plan_title: first.plan_title, items };
    }
    return null;
  }
  const idsOf = items => items.map(r => r.node_id).join("|");

  /* what is in each field at first: what the person typed before, else what the plan proposes */
  function startAnswers(items, before) {
    const out = {};
    items.forEach(r => {
      if (before && before[r.node_id] !== undefined) out[r.node_id] = before[r.node_id];
      else if (r.proposed !== null && r.proposed !== undefined) out[r.node_id] = r.proposed;
    });
    return out;
  }
  /* the answer checked for its question: {value} or {error: a word of the page} */
  function check(r, value) {
    if (r.ask === "approve") return typeof value === "boolean" ? { value } : { error: "hr.need.yesno" };
    const t = String(value == null ? "" : value).trim();
    if (r.kind === "choice") return r.choices.some(c => c.value === t) ? { value: t } : { error: "hr.need.choice" };
    if (!t) return { error: "hr.need" };
    if (r.kind === "number" && !Number.isFinite(Number(t))) return { error: "hr.need.number" };
    return { value: t };
  }
  /* an answer as the person reads it: a choice by its label, never by its value */
  function shown(r, value, l, words) {
    if (r.ask === "approve") return value === true ? words.yes : value === false ? words.no : "";
    if (r.kind === "choice") { const c = r.choices.find(x => x.value === value); return c ? labelOf(c, l) : String(value == null ? "" : value); }
    return String(value == null ? "" : value);
  }
  /* the path: step i of n is a question, step n is the summary */
  function newPath(items, before) { return { items, answers: startAnswers(items, before), step: 0, back: false }; }
  function next(w) {
    if (w.step >= w.items.length) return {};
    const r = w.items[w.step], got = check(r, w.answers[r.node_id]);
    if (got.error) return got;
    w.answers[r.node_id] = got.value;
    w.step = w.back ? w.items.length : w.step + 1;                  // after a change made from the summary it goes straight back there
    w.back = false;
    return {};
  }
  function prev(w) { if (w.step > 0) w.step -= 1; w.back = false; }
  function edit(w, i) { w.step = i; w.back = true; }
  /* the open path when the questions that are asking changed under it: the answers given stay, the step stays where it can */
  function reconcile(w, items) {
    const fresh = newPath(items, w.answers);
    fresh.step = Math.min(w.step, items.length);
    return fresh;
  }
  /* the answers as the server takes them: step id -> value */
  const payload = w => Object.fromEntries(w.items.map(r => [r.node_id, w.answers[r.node_id]]));
  const missing = w => w.items.findIndex(r => check(r, w.answers[r.node_id]).error);

  const api = { pickGroup, idsOf, startAnswers, check, shown, newPath, next, prev, edit, reconcile, payload, missing, say, labelOf, SNOOZE_MS };
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

  let list = [], timer = 0, group = null, path = null, sending = false, failed = "", focusing = true;
  const snoozed = {}, drafts = {};
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

  function later() { snoozed[group.key] = Date.now() + SNOOZE_MS; drafts[group.key] = path; group = path = null; failed = ""; show(); }
  const forward = () => { const got = next(path); failed = got.error ? T(got.error) : ""; focusing = true; draw(); };
  const backward = () => { prev(path); failed = ""; focusing = true; draw(); };

  async function send() {
    const bad = missing(path);
    if (bad >= 0) { path.step = bad; path.back = true; failed = T(check(path.items[bad], path.answers[path.items[bad].node_id]).error); focusing = true; draw(); return; }
    sending = true; failed = ""; draw();
    const res = await call("POST", `api/plans/${encodeURIComponent(group.plan_id)}/events`, { type: "plan:answers", answers: payload(path), actor: "user" });
    sending = false;
    if (res.ok && res.data.ok !== false) { delete drafts[group.key]; group = path = null; await refresh(); return; }
    failed = T("hr.err", (res.data && (res.data.error && res.data.error.message || res.data.error)) || res.status || "");
    draw();
    refresh();                                                      // what is asking may have changed: the path follows it
  }

  /* the field of a question */
  function field(r, l) {
    const value = path.answers[r.node_id];
    if (r.ask === "approve") {
      const pick = yes => { path.answers[r.node_id] = yes; failed = ""; focusing = true; draw(); };
      const yes = button(T("hr.yes"), "hr-choice" + (value === true ? " on" : ""), () => pick(true));
      const no = button(T("hr.no"), "hr-choice" + (value === false ? " on" : ""), () => pick(false));
      yes.setAttribute("aria-pressed", String(value === true)); no.setAttribute("aria-pressed", String(value === false));
      return { el: h("div", "hr-yesno", yes, no), focus: value === false ? no : yes };
    }
    let input;
    if (r.kind === "choice") {
      input = h("select", "hr-field");
      input.append(Object.assign(h("option", "", T("hr.choice")), { value: "" }));
      r.choices.forEach(c => input.append(Object.assign(h("option", "", labelOf(c, l)), { value: c.value })));
      input.value = value === undefined ? "" : String(value);
    } else {
      input = h("input", "hr-field"); input.type = "text"; input.autocomplete = "off"; input.spellcheck = false; input.maxLength = 4000;
      if (r.kind === "number") input.inputMode = "decimal";
      input.value = value === undefined ? "" : String(value);
    }
    input.id = "hr-value";
    const keep = () => { path.answers[r.node_id] = input.value; };
    input.addEventListener("input", keep); input.addEventListener("change", keep);
    return { el: input, focus: input };
  }

  function draw() {
    if (!group || !path) return;
    const l = lang(), n = path.items.length, summary = path.step >= n;
    const words = { yes: T("hr.answer.yes"), no: T("hr.answer.no") };
    const body = [h("p", "hr-plan", group.plan_title)];
    let focus = null;
    if (!summary) {
      const r = path.items[path.step], f = field(r, l);
      focus = f.focus;
      const lab = h("label", "hr-q", say(r, "prompt", l)); lab.id = "hr-card-h";
      if (r.ask !== "approve") lab.htmlFor = "hr-value";
      body.push(h("div", "hr-progress", h("span", "hr-count", T("hr.q", path.step + 1, n)), h("span", "hr-bar", h("span", "hr-fill"))), lab);
      if (say(r, "help", l)) body.push(h("p", "hr-helpline", say(r, "help", l)));
      if (r.content) body.push(h("p", "hr-lab", T("hr.content")), Object.assign(h("pre", "hr-content", r.content), { tabIndex: 0 }));
      body.push(f.el);
      if (failed) body.push(Object.assign(h("p", "hr-err", failed), { role: "alert" }));
      body.push(h("div", "hr-nav", Object.assign(button(T("hr.prev"), "", backward), { disabled: path.step === 0 }), button(T("hr.next"), "primary", forward)));
      body.push(h("p", "hr-keys", T("hr.keys")));
    } else {
      body.push(Object.assign(h("h2", "hr-q", T("hr.review")), { id: "hr-card-h" }), h("p", "hr-helpline", T("hr.review.help")));
      const rows = h("ul", "hr-rows");
      path.items.forEach((r, i) => {
        const bad = check(r, path.answers[r.node_id]).error;
        const change = button(T("hr.edit"), "hr-edit", () => { edit(path, i); failed = ""; focusing = true; draw(); });
        change.setAttribute("aria-label", T("hr.edit.q", say(r, "prompt", l)));
        rows.append(h("li", "hr-row" + (bad ? " bad" : ""), h("div", "hr-rowtext", h("span", "hr-rowq", say(r, "prompt", l)), h("b", "hr-rowa", bad ? T(bad) : shown(r, path.answers[r.node_id], l, words))), change));
      });
      body.push(rows);
      if (failed) body.push(Object.assign(h("p", "hr-err", failed), { role: "alert" }));
      if (sending) body.push(h("p", "hr-helpline", T("hr.sending")));
      const sendAll = button(T("hr.send"), "primary hr-send", send); sendAll.disabled = sending;
      body.push(h("div", "hr-nav", Object.assign(button(T("hr.prev"), "", backward), { disabled: sending }), sendAll));
    }
    body.push(button(T("hr.later"), "hr-later", later), h("p", "hr-helpline", T("hr.later.help")));
    dlg.replaceChildren(h("div", "hr-form", body));
    const fill = dlg.querySelector(".hr-fill");
    if (fill) fill.style.width = `${Math.round(100 * (path.step + 1) / n)}%`;
    if (focusing) {
      const target = focus || dlg.querySelector(".hr-q");           // the summary opens on its title, so that a second press of Enter cannot send
      if (target) { if (!focus) target.tabIndex = -1; target.focus(); }
    }
    focusing = false;
  }

  function show() {
    const g = pickGroup(list, snoozed, Date.now());
    if (!g) { group = path = null; if (dlg.open) dlg.close(); return; }
    if (group && g.key === group.key) {                             // the same plan: the path goes on, following what is asking
      group = g;
      if (idsOf(g.items) !== idsOf(path.items)) { path = reconcile(path, g.items); draw(); }
      return;
    }
    group = g;
    const kept = drafts[g.key];
    path = newPath(g.items, kept ? kept.answers : null);
    if (kept) path.step = Math.min(kept.step, g.items.length);
    failed = ""; focusing = true;
    if (!dlg.open) dlg.showModal();
    draw();
  }

  /* Esc leaves the questions for later; the arrow keys move along the path (not inside a text field, where they move the cursor); Enter goes on from a field */
  dlg.addEventListener("cancel", e => { e.preventDefault(); if (group) later(); });
  dlg.addEventListener("keydown", e => {
    if (!path || sending || e.altKey || e.ctrlKey || e.metaKey) return;
    const t = e.target, typing = t && (t.tagName === "TEXTAREA" || (t.tagName === "INPUT" && t.type === "text"));
    const asking = path.step < path.items.length;
    if (e.key === "ArrowRight" && !typing && asking) { e.preventDefault(); forward(); }
    else if (e.key === "ArrowLeft" && !typing && path.step > 0) { e.preventDefault(); backward(); }
    else if (e.key === "Enter" && t && t.tagName !== "BUTTON" && asking) { e.preventDefault(); forward(); }
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
  document.addEventListener("tadamoo:lang", () => { if (group) draw(); });
  setTimeout(refresh, 1200);
})(typeof window !== "undefined" ? window : globalThis);
