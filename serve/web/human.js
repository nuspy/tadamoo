"use strict";
/* What a plan asks the person (an approval, or a value), as a window over whatever tab is open. The server lists the requests of all the plans
   (/api/plans/human); the oldest comes first, and the window stays until it is answered or left for later with Back. An answer takes two steps:
   choose (Yes or No; or write a value and press OK), then confirm; Back from the confirmation returns to the choice. Everything the server says is put in the page as text. */
(function (root) {
  const SNOOZE_MS = 10 * 60 * 1000, EVERY_MS = 4000;
  const keyOf = r => `${r.plan_id}/${r.node_id}`;

  /* the request to show: the oldest one that has not been left for later */
  function pick(list, snoozed, now) {
    return (Array.isArray(list) ? list : []).find(r => !((snoozed || {})[keyOf(r)] > now)) || null;
  }
  /* what a typed answer is, or why it is not one: {value} or {error: word} */
  function readValue(request, text) {
    const t = String(text == null ? "" : text).trim();
    if (!t) return { error: "hr.need" };
    if (request.kind === "number" && !Number.isFinite(Number(t))) return { error: "hr.need.number" };
    return { value: t };
  }
  const api = { pick, readValue, keyOf, SNOOZE_MS };
  root.HumanAsk = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.createElement) return;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const button = (text, cls, onclick) => { const b = h("button", "btn hr-btn" + (cls ? " " + cls : ""), text); b.type = "button"; b.addEventListener("click", onclick); return b; };

  let list = [], timer = 0, shownKey = "", step = "ask", chosen = null, sending = false, failed = "";
  const snoozed = {};
  const dlg = h("dialog", "hr-dialog");
  dlg.id = "hr-card"; dlg.setAttribute("aria-labelledby", "hr-card-h");
  document.body.append(dlg);

  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const later = r => { snoozed[keyOf(r)] = Date.now() + SNOOZE_MS; reset(); show(); };
  function reset() { step = "ask"; chosen = null; sending = false; failed = ""; }

  async function send(r, value) {
    sending = true; failed = ""; draw(r);
    const res = await call("POST", `api/plans/${encodeURIComponent(r.plan_id)}/events`, { type: "plan:answer", node_id: r.node_id, answer: value, actor: "user" });
    if (res.ok && res.data.ok !== false) { reset(); shownKey = ""; await refresh(); return; }
    sending = false;
    failed = T("hr.err", (res.data && (res.data.error && res.data.error.message || res.data.error)) || res.status || "");
    step = "ask"; draw(r);
  }

  function shown(r) { return r.ask === "approve" ? T(chosen ? "hr.answer.yes" : "hr.answer.no") : String(chosen); }

  function draw(r) {
    const index = list.filter(x => !(snoozed[keyOf(x)] > Date.now()));
    const n = index.findIndex(x => keyOf(x) === keyOf(r)) + 1;
    const body = [
      n && index.length > 1 ? h("p", "hr-meta", T("hr.count", n, index.length)) : null,
      Object.assign(h("h2", "", T("hr.title")), { id: "hr-card-h" }),
      h("p", "hr-plan", r.plan_title), h("p", "hr-step", T("hr.step", r.title)),
      h("p", "hr-prompt", r.prompt),
    ];
    if (r.content) body.push(h("p", "hr-lab", T("hr.content")), Object.assign(h("pre", "hr-content", r.content), { tabIndex: 0 }));
    const actions = h("div", "hr-actions");
    let field = null;
    if (step === "confirm") {
      body.push(h("p", "hr-you", T("hr.confirm.you", shown(r))), h("p", "hr-q", T("hr.confirm.q")));
      actions.append(button(T("hr.confirm.send"), "primary", () => send(r, chosen)), button(T("hr.back"), "", () => { step = "ask"; failed = ""; draw(r); }));
      actions.querySelectorAll("button").forEach(b => { b.disabled = sending; });
    } else if (r.ask === "approve") {
      actions.append(button(T("hr.yes"), "primary", () => { chosen = true; step = "confirm"; draw(r); }), button(T("hr.no"), "", () => { chosen = false; step = "confirm"; draw(r); }),
        button(T("hr.back"), "hr-back", () => later(r)));
    } else {
      if (r.kind === "choice") {
        field = h("select", "hr-field"); field.id = "hr-value";
        field.append(Object.assign(h("option", "", T("hr.choice")), { value: "" }));
        r.choices.forEach(c => field.append(Object.assign(h("option", "", c), { value: c })));
      } else {
        field = h("input", "hr-field"); field.id = "hr-value"; field.type = "text"; field.autocomplete = "off"; field.spellcheck = false;
        if (r.kind === "number") field.inputMode = "decimal";
      }
      const lab = h("label", "hr-lab", T("hr.value")); lab.htmlFor = "hr-value";
      const proceed = () => {
        const got = readValue(r, field.value);
        if (got.error) { failed = T(got.error); draw(r); return; }
        chosen = got.value; step = "confirm"; failed = ""; draw(r);
      };
      field.addEventListener("keydown", e => { if (e.key === "Enter") { e.preventDefault(); proceed(); } });
      body.push(lab, field);
      actions.append(button(T("hr.ok"), "primary", proceed), button(T("hr.back"), "hr-back", () => later(r)));
      if (chosen != null) field.value = String(chosen);
    }
    if (failed) body.push(Object.assign(h("p", "hr-err", failed), { role: "alert" }));
    if (step === "ask") body.push(h("p", "help hr-help", T("hr.back.help")));
    if (sending) body.push(h("p", "help", T("hr.sending")));
    const typed = field && document.activeElement === field ? field.value : null;
    dlg.replaceChildren(h("div", "hr-form", body, actions));
    if (field) { if (typed !== null) field.value = typed; if (step === "ask") field.focus(); }
    else { const first = dlg.querySelector(".hr-btn"); if (first) first.focus(); }
  }

  function show() {
    const r = pick(list, snoozed, Date.now());
    if (!r) { shownKey = ""; if (dlg.open) dlg.close(); return; }
    if (keyOf(r) === shownKey && dlg.open) return;                  // already showing it: the answer in progress is not disturbed by a refresh
    shownKey = keyOf(r); reset(); draw(r);
    if (!dlg.open) dlg.showModal();
    const first = dlg.querySelector("#hr-value, .hr-btn");                   // the window opens on its first action, not on the text
    if (first) first.focus();
  }
  /* Esc and the browser's own close are Back: from the confirmation they go back to the choice, from the choice they leave the request for later */
  dlg.addEventListener("cancel", e => {
    e.preventDefault();
    const r = pick(list, snoozed, Date.now());
    if (!r) return;
    if (step === "confirm") { step = "ask"; failed = ""; draw(r); } else later(r);
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
  document.addEventListener("tadamoo:lang", () => { const r = pick(list, snoozed, Date.now()); if (r && dlg.open) draw(r); });
  setTimeout(refresh, 1200);
})(typeof window !== "undefined" ? window : globalThis);
