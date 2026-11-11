"use strict";
/* Heavy jobs: (1) Settings > General: the level (ask, allow for images and video, never) and the provider the conversations go to while the local model
   sleeps for a heavy job; (2) the question a job asks before that happens, on a dialog that opens on any page. Everything the server says is put in the
   page as text. */
(() => {
  const root = document.getElementById("hv-root");
  if (!root) return;
  let state = null, providers = [], note = "", bad = false, timer = 0, shown = "";

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const MODES = ["ask", "allow", "never"];
  const lang = () => (document.documentElement.lang || "en").slice(0, 2);

  async function load() {
    const [s, l] = await Promise.all([call("GET", "api/heavy"), call("GET", "api/llm")]);
    if (s.ok) { state = s.data; note = ""; } else { note = T("hv.e.offline"); bad = true; }
    providers = l.ok ? (l.data.providers || []).filter(p => !p.is_local && !p.judge_only && !p.agent_only) : [];
    draw();
    question();
  }
  async function save(change) {
    const r = await call("POST", "api/heavy/settings", change);
    if (r.ok) { state = { ...state, ...r.data }; note = T("hv.saved"); bad = false; } else { note = (r.data.error && r.data.error.message) || T("hv.e.offline"); bad = true; }
    draw();
  }

  function draw() {
    root.textContent = "";
    if (!state) { root.append(h("p", "setnote", note || T("hv.loading"))); return; }
    const group = h("fieldset", "hv-levels");
    group.append(h("legend", "", T("hv.level")));
    MODES.forEach(m => {
      const input = h("input"); input.type = "radio"; input.name = "hv-mode"; input.value = m; input.id = "hv-mode-" + m; input.checked = state.mode === m;
      input.onchange = () => save({ mode: m });
      const label = h("label", "hv-level", input, h("span", "", h("b", "", T("hv.mode." + m)), h("span", "help", T("hv.mode." + m + ".help"))));
      group.append(label);
    });
    const select = h("select"); select.id = "hv-provider"; select.setAttribute("aria-describedby", "hv-provider-h");
    select.append(Object.assign(h("option", "", T("hv.none")), { value: "" }));
    providers.forEach(p => select.append(Object.assign(h("option", "", p.name || p.id), { value: p.id })));
    select.value = state.provider || "";
    select.onchange = () => save({ provider: select.value });
    const field = h("div", "field", h("label", "", T("hv.provider")), select, h("p", "help", T("hv.provider.help")));
    field.querySelector("p").id = "hv-provider-h";
    field.querySelector("label").htmlFor = "hv-provider";
    const msg = h("p", "setstatus", note); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");
    if (bad) msg.dataset.bad = "1";
    root.append(h("p", "help", T("hv.intro")), group, field, state.active ? h("p", "setnote", T("hv.active", state.active.kind, state.active.provider)) : null, msg);
  }

  // The question: a dialog on whatever page is open, with the three answers.
  function question() {
    const card = state && state.pending && state.pending[0];
    const dlg = document.getElementById("hv-card");
    if (!card) { shown = ""; if (dlg && dlg.open) dlg.close(); return; }
    if (card.id === shown && dlg && dlg.open) return;
    shown = card.id;
    const it = lang() === "it";
    const answer = async (allow, always) => { await call("POST", "api/heavy/answer", { id: card.id, allow, always }); shown = ""; dlg.close(); load(); };
    const yes = h("button", "btn primary", T("hv.card.yes")), always = h("button", "btn", T("hv.card.always")), no = h("button", "btn", T("hv.card.no"));
    [yes, always, no].forEach(b => { b.type = "button"; });
    yes.onclick = () => answer(true, false); always.onclick = () => answer(true, true); no.onclick = () => answer(false, false);
    const cost = card.cost || {};
    dlg.textContent = "";
    dlg.append(h("form", "hv-form",
      Object.assign(h("h2", "", T("hv.card.title", card.kind)), { id: "hv-card-h" }),
      h("p", "", T("hv.card.what", card.purpose || card.kind, card.minutes)),
      h("p", "", T("hv.card.where", card.provider)),
      h("p", "", h("b", "", T("hv.card.leaves")), " ", it ? card.leaves_it : card.leaves),
      h("p", "", h("b", "", T("hv.card.cost")), " ", it ? cost.note_it : cost.note, cost.daily_cap ? " " + T("hv.card.cap", cost.tokens_used_today, cost.daily_cap) : ""),
      h("p", "help", T("hv.card.after")),
      h("div", "hv-actions", yes, always, no)));
    if (!dlg.open) dlg.showModal();
    yes.focus();
  }

  async function poll() {
    clearTimeout(timer);
    if (document.visibilityState === "visible") {
      const s = await call("GET", "api/heavy");
      if (s.ok) { state = s.data; question(); if (document.body.dataset.view === "settings" && !root.contains(document.activeElement)) draw(); }
    }
    timer = setTimeout(poll, 3000);
  }
  const dlg = h("dialog", "hv-dialog"); dlg.id = "hv-card"; dlg.setAttribute("aria-labelledby", "hv-card-h");
  dlg.addEventListener("cancel", e => e.preventDefault());                   // a question is answered with a button, not dismissed
  document.body.append(dlg);
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") load(); });
  document.addEventListener("tadamoo:lang", () => { if (state) { draw(); shown = ""; question(); } });
  load().then(poll);
})();
