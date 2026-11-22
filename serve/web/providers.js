"use strict";
/* Models > Providers: which provider each internal function of TaDaMoo uses (the Chat, the agent's loop, the planner, the judges),
   and the providers themselves (a model in this PC, a cloud service, a command-line agent, Jev that judges).
   What other programs see of this server does not change: it is the loaded model. Keys are write-only: they go to the server,
   which keeps them in the Windows credential manager; this page is only told that there is one. Everything the server says is
   put in the page as text. */
(() => {
  const SUB_KEY = "basalt.models.sub";
  const view = document.getElementById("view-loader");
  const panel = document.getElementById("ld-providers"), qwen = document.getElementById("ld-qwen"), conv = document.getElementById("ld-converter");
  if (!view || !panel) return;
  const load = (k, d) => { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* this page only */ } };
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, onclick) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  const PRIMARY = "@primary";                                                    // a function that follows the Primary model
  let state = null, busy = false, editing = "", note = "", noteBad = false, loadProfiles = [];

  async function call(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  function explain(res) {
    const err = (res.data && res.data.error) || {};
    if (res.status === 0) return T("pv.e.offline");
    if (err.code === "not_allowed" && err.message) return err.message;           // the reason is specific: which function, which provider
    const key = err.code ? "pv.e." + err.code : "";
    return key && T(key) !== key ? T(key) : (err.message || `HTTP ${res.status}`);
  }
  const say = (t, bad) => { note = t; noteBad = !!bad; drawNote(); };
  function drawNote() { const n = panel.querySelector(".pv-note"); if (n) { n.textContent = note; n.dataset.bad = noteBad ? "1" : ""; n.hidden = !note; } }

  /* ---- the two sub-tabs ---- */
  function sub(which) {
    const prov = which === "providers", cv = which === "converter", lc = which === "llamacpp", vc = which === "voicesvc";
    panel.hidden = !prov; qwen.hidden = prov || cv || lc || vc;
    const vcPanel = document.getElementById("ld-voicesvc");
    if (vcPanel) vcPanel.hidden = !vc;
    const vcTab = document.getElementById("sub-voicesvc");
    if (vcTab) vcTab.setAttribute("aria-selected", String(vc));
    if (conv) conv.hidden = !cv;
    const lcPanel = document.getElementById("ld-llamacpp");
    if (lcPanel) lcPanel.hidden = !lc;
    document.getElementById("sub-providers").setAttribute("aria-selected", String(prov));
    document.getElementById("sub-qwen").setAttribute("aria-selected", String(!prov && !cv && !lc && !vc));
    const tab = document.getElementById("sub-converter");
    if (tab) tab.setAttribute("aria-selected", String(cv));
    const lcTab = document.getElementById("sub-llamacpp");
    if (lcTab) lcTab.setAttribute("aria-selected", String(lc));
    keep(SUB_KEY, which);
    if (prov) refresh();
    document.dispatchEvent(new CustomEvent("basalt:models-sub", { detail: which }));
  }
  document.getElementById("sub-providers").onclick = () => sub("providers");
  document.getElementById("sub-qwen").onclick = () => sub("qwen");
  const convTab = document.getElementById("sub-converter");
  if (convTab) convTab.onclick = () => sub("converter");
  const lcTabBtn = document.getElementById("sub-llamacpp");
  if (lcTabBtn) lcTabBtn.onclick = () => sub("llamacpp");
  const vcTabBtn = document.getElementById("sub-voicesvc");
  if (vcTabBtn) vcTabBtn.onclick = () => sub("voicesvc");

  /* ---- reading ---- */
  async function refresh() {
    const r = await call("GET", "api/llm");
    if (r.ok) {
      state = r.data;
      const pr = await call("GET", "api/profiles");                              // the Loader's load profiles, for a route to this server
      loadProfiles = pr.ok ? (pr.data.load || []).map(p => p.name) : [];
      const pe = await call("GET", "api/personas");                              // the personalities a council role can take its job from
      personas = pe.ok ? (pe.data.personas || []).map(x => ({ slug: x.slug, name: x.name })) : [];
      draw();
    } else if (!state) { panel.replaceChildren(h("p", "setnote warnnote", explain(r))); }
  }

  /* ---- the strip: what is loaded and who uses it ---- */
  async function strip() {
    const box = h("section", "sec pv-strip");
    let st = null;
    try { const r = await fetch("v1/status", { cache: "no-store", headers: authHeaders() }); if (r.ok) st = await r.json(); } catch (e) { /* offline */ }
    const users = state.functions.filter(f => !f.now.provider).map(f => f.label);
    box.append(h("b", "", T("pv.loaded")), " ", st && st.loaded ? st.model : T("pv.noneLoaded"),
      h("span", "dim", " · " + T("pv.usedBy", users.length ? users.join(", ") : "—")));
    return box;
  }

  /* ---- the Primary model: one choice that every function set to it follows, at once ---- */
  function primarySection() {
    const pr = state.primary, sec = h("section", "sec pv-primary");
    const sel = h("select", "", [Object.assign(h("option", "", T("pv.primary.local")), { value: "" }), ...pr.options.map(o => Object.assign(h("option", "", o.label), { value: o.id }))]);
    sel.value = pr.id; sel.id = "pv-primary-sel";
    sel.onchange = async () => {
      const r = await call("POST", "api/llm/primary", { primary: sel.value });
      if (r.ok) { state = r.data; say(T("pv.saved")); document.dispatchEvent(new CustomEvent("basalt:llm-changed")); } else { say(explain(r), true); sel.value = pr.id; }
      draw();
    };
    const fresh = h("input"); fresh.type = "checkbox"; fresh.checked = !!pr.default_for_new;
    fresh.onchange = async () => {
      const r = await call("POST", "api/llm/primary", { default_for_new: fresh.checked });
      if (r.ok) state = r.data; else say(explain(r), true);
      draw();
    };
    sec.append(h("div", "sh", h("b", "", T("pv.primary"))), h("p", "setnote", T("pv.primary.help")),
      h("div", "pv-primary-row", field("pv-primary", T("pv.primary"), sel), h("label", "pv-consent", fresh, h("span", "", T("pv.primary.new")))),
      h("p", "setnote pv-primary-now", T("pv.primary.now", pr.label)));
    return sec;
  }
  function fallbackNotice() {
    if (!state.fallbacks.length) return null;
    const by = new Map();
    state.fallbacks.forEach(n => by.set(n.to, [...(by.get(n.to) || []), n.label]));
    const p = h("p", "setnote warnnote pv-fallback", [...by].map(([to, labels]) => T("pv.fallback.notice", labels.join(", "), to)).join(" "));
    p.setAttribute("role", "status");
    return p;
  }

  /* ---- the table of functions ---- */
  function providerOptions(f) {
    const opts = [Object.assign(h("option", "", T("pv.local")), { value: "" }), Object.assign(h("option", "", T("pv.primary.option")), { value: PRIMARY })];
    state.providers.forEach(p => {
      if (f.kind === "chat" && p.judge_only) return;
      if (f.kind === "chat" && f.tools && p.agent_only) return;
      opts.push(Object.assign(h("option", "", `${p.name} (${p.is_local ? T("pv.onpc") : T("pv.outside")})`), { value: p.id }));
    });
    return opts;
  }
  function functionsTable() {
    const sec = h("section", "sec pv-functions");
    sec.append(h("div", "sh", h("b", "", T("pv.functions"))), h("p", "setnote", T("pv.functions.help")), h("p", "setnote", T("pv.local.help")));
    const table = h("table", "pv-table");
    table.append(h("thead", "", h("tr", "", ["pv.fn", "pv.data", "pv.provider", "pv.consent", "pv.status"].map(k => h("th", "", T(k))))));
    const body = h("tbody");
    state.functions.forEach(f => {
      const sel = h("select", "", providerOptions(f));
      sel.value = f.provider; sel.setAttribute("aria-label", f.label);
      sel.onchange = async () => {
        const r = await call("POST", "api/llm/assign", { function: f.id, provider: sel.value });
        if (r.ok) { state = r.data; say(T("pv.saved")); document.dispatchEvent(new CustomEvent("basalt:llm-changed")); } else { say(explain(r), true); sel.value = f.provider; }
        draw();
      };
      const target = f.provider === PRIMARY ? state.primary.id : f.provider;
      const outside = target && !(state.providers.find(p => p.id === target) || {}).is_local;
      const consent = h("input"); consent.type = "checkbox"; consent.checked = f.consent; consent.disabled = !f.needs_consent;
      consent.setAttribute("aria-label", T("pv.consent.for", f.label));
      consent.onchange = async () => {
        if (consent.checked && !(await riskConfirm([T("pv.risk.consent", f.label)]))) { draw(); return; }
        const r = await call("POST", "api/llm/consent", { function: f.id, allowed: consent.checked }); if (r.ok) state = r.data; else say(explain(r), true); draw();
      };
      const cell = (key, cls, ...kids) => { const td = h("td", cls, ...kids); td.dataset.label = T(key); return td; };          // the label shows on a narrow screen, where a row is a card
      body.append(h("tr", "", cell("pv.fn", "", f.label), cell("pv.data", "", T("pv.data." + f.data)), cell("pv.provider", "", sel),
        cell("pv.consent", "", f.needs_consent ? consent : h("span", "dim", "—"), outside && !f.consent ? h("span", "dim", " " + T("pv.consent.need")) : null),
        cell("pv.status", f.ok ? "" : "pv-bad", f.ok ? T("pv.ok") : f.why, h("div", "dim pv-now", f.now.via === "fallback" ? T("pv.via.fallback", f.now.label) : f.now.via === "refused" ? T("pv.via.refused") : T("pv.f.now", f.now.label)))));
    });
    table.append(body);
    sec.append(h("div", "pv-scroll", table));
    return sec;
  }

  /* ---- other programs calling this server with the OpenAI or the Anthropic interface: always the loaded local model, never a provider chosen above ---- */
  function externalRequests() {
    const sec = h("section", "sec pv-external");
    sec.append(h("div", "sh", h("b", "", T("pv.external"))), h("p", "pv-external-who", T("pv.external.who")), h("p", "setnote", T("pv.external.help")));
    return sec;
  }

  /* ---- the council: the roles with their route, rounds, the cap on calls and when the model may convene it ---- */
  let draft = null, dirty = false, personas = [];
  const touch = () => { dirty = true; };
  const clone = o => JSON.parse(JSON.stringify(o));
  const outsideRoute = pid => { const to = pid === PRIMARY ? state.primary.id : pid; return !!to && !((state.providers.find(p => p.id === to) || {}).is_local); };
  const option = (value, text) => Object.assign(h("option", "", text), { value });
  const routeOptions = () => [option("", T("pv.local")), option(PRIMARY, T("pv.primary.option")), ...state.providers.filter(p => !p.judge_only).map(p => option(p.id, `${p.name} (${p.is_local ? T("pv.onpc") : T("pv.outside")})`))];
  const personaOptions = role => [option("", T("pv.council.persona.none")), ...personas.map(x => option(x.slug, x.name)),
    ...(role.persona && !personas.some(x => x.slug === role.persona) ? [option(role.persona, T("pe.gone", role.persona))] : [])];
  const nameOfRoute = pid => pid === PRIMARY ? T("pv.primary.option") : (state.providers.find(p => p.id === pid) || {}).name || T("pv.local.short");

  /* one role, closed to a line (enabled box, name, route) and opened to its fields */
  function roleBox(role, moderator, lim) {
    const box = h("details", "pv-crole"), sum = h("summary", "pv-crole-sum");
    const on = h("input"); on.type = "checkbox"; on.checked = role.enabled !== false; on.setAttribute("aria-label", T("pv.council.enabled", role.name));
    const tick = h("label", "pv-tick", on);
    on.onclick = e => e.stopPropagation(); tick.onclick = e => e.stopPropagation();
    on.onchange = () => { role.enabled = on.checked; touch(); box.dataset.off = role.enabled ? "" : "1"; };
    box.dataset.off = role.enabled === false ? "1" : "";
    const title = h("span", "pv-crole-name", role.name || "…"), where = h("span", "pv-badge", nameOfRoute(role.route));
    sum.append(...(moderator ? [] : [tick]), title, where, role.ok === false && role.route ? h("span", "pv-bad", "!") : null);
    const name = h("input"), job = h("textarea"), persona = h("select", "", personaOptions(role)), route = h("select", "", routeOptions());
    name.type = "text"; name.maxLength = lim.name; name.value = role.name; name.autocomplete = "off";
    job.rows = 2; job.maxLength = lim.job; job.value = role.job; persona.value = role.persona || ""; route.value = role.route || "";
    name.oninput = () => { role.name = name.value; title.textContent = name.value || "…"; touch(); };
    job.oninput = () => { role.job = job.value; touch(); };
    persona.onchange = () => { role.persona = persona.value; touch(); };
    const consent = h("input"); consent.type = "checkbox"; consent.checked = !!role.consent;
    const consentRow = h("label", "pv-consent", consent, h("span", "", T("pv.council.consent")));
    const showConsent = () => { consentRow.hidden = !outsideRoute(role.route); where.textContent = nameOfRoute(role.route); };
    route.onchange = () => { role.route = route.value; touch(); showConsent(); };
    consent.onchange = async () => {
      if (consent.checked && !(await riskConfirm([T("pv.risk.consent", role.name)]))) { consent.checked = false; return; }
      role.consent = consent.checked; touch();
    };
    showConsent();
    const body = h("div", "pv-crole-body", field("", T("pv.council.name"), name), field("", T("pv.council.persona"), persona, T("pv.council.persona.help")),
      field("", T("pv.council.job"), job), field("", T("pv.council.route"), route), consentRow,
      role.ok === false && role.route && role.why ? h("p", "setnote pv-bad", role.why) : null);
    box.append(sum, body);
    return { box, body };
  }

  function councilSection() {
    const c = state.council, lim = c.limits, sec = h("section", "sec pv-council");
    if (!draft || !dirty) { draft = clone(c.settings); dirty = false; }
    const cur = draft;
    sec.append(h("div", "sh", h("b", "", T("pv.council"))), h("p", "setnote", T("pv.council.help")));
    const num = (value, min, max, set) => { const i = h("input"); i.type = "number"; i.min = String(min); i.max = String(max); i.value = value; i.inputMode = "numeric"; i.oninput = () => { set(i.value === "" ? NaN : Number(i.value)); touch(); }; return i; };
    const auto = h("select", "", ["off", "ask", "auto"].map(m => option(m, T("pv.council.auto." + m))));
    auto.value = cur.council_auto; auto.onchange = () => { cur.council_auto = auto.value; touch(); };
    sec.append(h("div", "pv-cgrid", field("pv-c-rounds", T("pv.council.rounds"), num(cur.rounds, 1, lim.rounds, v => { cur.rounds = v; }), T("pv.council.rounds.help", lim.rounds)),
      field("pv-c-cap", T("pv.council.cap"), num(cur.max_calls, 0, 100000, v => { cur.max_calls = v; }), T("pv.council.cap.help", lim.calls_min)),
      field("pv-c-auto", T("pv.council.auto"), auto, T("pv.council.auto.help"))));
    const list = h("div", "pv-clist");
    cur.roles.forEach((r, i) => {
      const { box, body } = roleBox(r, false, lim);
      body.append(btn(T("pv.council.remove"), "ghost", () => { cur.roles.splice(i, 1); touch(); draw(); }));
      list.append(box);
    });
    sec.append(h("div", "pv-chead", h("b", "", T("pv.council.advisers", cur.roles.filter(r => r.enabled !== false).length, cur.roles.length))), list);
    sec.append(btn(T("pv.council.add"), "", () => { if (cur.roles.length < lim.roles) { cur.roles.push({ id: "", name: "", job: "", enabled: true, persona: "", route: state.primary.default_for_new ? PRIMARY : "", consent: false }); touch(); draw(); } }));
    sec.append(h("div", "pv-chead", h("b", "", T("pv.council.moderator"))), roleBox(cur.moderator, true, lim).box);
    const e = c.estimate;
    const cost = h("p", "pv-cost", e.ready ? T("pv.council.cost", e.calls_min, e.calls_max, e.read_tokens_min, e.read_tokens_max, e.written_tokens_max) : T("pv.council.few"));
    cost.setAttribute("role", "status");
    sec.append(cost);
    (e.notes || []).forEach(n => sec.append(h("p", "setnote warnnote", n)));
    if (e.outside_calls_max) sec.append(h("p", "setnote warnnote", T("pv.council.outside", e.outside_calls_max)));
    if (dirty) sec.append(h("p", "setnote", T("pv.council.unsaved")));
    const send = () => ({ rounds: cur.rounds, max_calls: cur.max_calls || 0, council_auto: cur.council_auto,
      roles: cur.roles.map(r => ({ id: r.id, name: r.name, job: r.job, enabled: r.enabled !== false, persona: r.persona || "", route: r.route || "", consent: !!r.consent })),
      moderator: { name: cur.moderator.name, job: cur.moderator.job, persona: cur.moderator.persona || "", route: cur.moderator.route || "", consent: !!cur.moderator.consent } });
    const save = async body => { const r = await call("POST", "api/llm/council", body); if (r.ok) { state = r.data; dirty = false; draft = null; say(T("pv.saved")); draw(); } else say(explain(r), true); };
    sec.append(h("div", "pv-actions", btn(T("pv.save"), "primary", () => save(send())), btn(T("pv.council.reset"), "ghost", () => save(c.defaults))));
    return sec;
  }

  /* ---- the providers ---- */
  function providerCard(p) {
    const card = h("li", "pv-card");
    const head = h("div", "pv-head", h("b", "", p.name), h("span", "pv-badge", p.is_local ? T("pv.onpc") : T("pv.outside")),
      p.judge_only ? h("span", "pv-badge", T("pv.judgeOnly")) : null, p.agent_only ? h("span", "pv-badge", T("pv.agentOnly")) : null);
    const meta = h("p", "dim pv-meta", `${p.label || p.type}${p.model ? " · " + p.model : ""}${p.base_url ? " · " + p.base_url : ""} · ${p.has_key ? T("pv.key.yes") : T("pv.key.no")}`);
    const cap = p.daily_token_cap ? h("p", "dim pv-meta", T("pv.cap", nfmt(p.used_today), nfmt(p.daily_token_cap))) : null;
    const row = h("div", "pv-actions");
    const test = btn(T("pv.test"), "", async () => { test.disabled = true; const r = await call("POST", `api/llm/providers/${p.id}/test`, {}); test.disabled = false; say(r.ok && r.data.ok ? T("pv.test.ok", p.name) : T("pv.test.bad", p.name, (r.data && r.data.error) || (r.data && r.data.note) || explain(r)), !(r.ok && r.data.ok)); });
    row.append(test, btn(T("pv.edit"), "ghost", () => { editing = p.id; draw(); }), removeButton(p));
    card.append(head, meta);
    if (cap) card.append(cap);
    card.append(row);
    if (editing === p.id) card.append(form(p));
    return card;
  }
  function removeButton(p) {
    let armed = false, timer = 0;
    const b = btn(T("pv.remove"), "ghost", async () => {                      // two presses: it is not given back
      if (!armed) { armed = true; b.textContent = T("pv.remove.sure"); timer = setTimeout(() => { armed = false; b.textContent = T("pv.remove"); }, 4000); return; }
      clearTimeout(timer);
      const r = await call("POST", `api/llm/providers/${p.id}/remove`, {});
      if (r.ok) { say(T("pv.removed", p.name)); await refresh(); } else say(explain(r), true);
    });
    return b;
  }

  function field(id, label, input, help) {
    const l = h("label", "pv-field", h("span", "", label), input);
    if (help) l.append(h("small", "dim", help));
    return l;
  }
  function form(existing) {
    const f = h("form", "pv-form"); f.noValidate = true;
    const type = h("select", "", state.types.map(t => Object.assign(h("option", "", t.label), { value: t.type })));
    const profile = h("select", "", [Object.assign(h("option", "", T("pv.f.profile.default")), { value: "" }), ...loadProfiles.map(n => Object.assign(h("option", "", n), { value: n }))]);
    const name = h("input"), url = h("input"), model = h("input"), key = h("input"), cap = h("input"), level = h("select", "", state.reasoning_levels.map(l => Object.assign(h("option", "", l.label || l.value), { value: l.value })));
    key.type = "password"; key.autocomplete = "off"; key.spellcheck = false;
    cap.type = "number"; cap.min = "0"; cap.step = "1000"; cap.inputMode = "numeric";
    const ctx = h("input"), out = h("input");
    [ctx, out].forEach(i => { i.type = "number"; i.min = "0"; i.step = "1000"; i.inputMode = "numeric"; });
    [name, url, model].forEach(i => { i.type = "text"; i.autocomplete = "off"; i.spellcheck = false; });
    if (existing) { ctx.value = existing.context_window || ""; out.value = existing.max_output || ""; }
    if (existing) { profile.value = existing.profile || ""; type.value = existing.type; type.disabled = true; name.value = existing.name || ""; url.value = existing.base_url || ""; model.value = existing.model || ""; cap.value = existing.daily_token_cap || ""; level.value = existing.reasoning_level || "inherit"; }
    const hint = () => { const t = state.types.find(x => x.type === type.value) || {}; url.placeholder = t.base_url || ""; key.placeholder = existing && existing.has_key ? T("pv.key.keep") : ""; };
    const profileField = field("pv-profile", T("pv.f.profile"), profile, T("pv.f.profile.help"));
    const here = () => type.value === "basalt";
    const limitFields = [field("pv-ctx", T("pv.f.ctx"), ctx, T("pv.f.ctx.help")), field("pv-out", T("pv.f.out"), out, T("pv.f.out.help"))];
    type.onchange = () => { hint(); profileField.hidden = !here(); limitFields.forEach(x => { x.hidden = here(); }); }; hint(); profileField.hidden = !here(); limitFields.forEach(x => { x.hidden = here(); });
    /* the models of the provider: loaded with the address and key as they are in the form (or the saved key), picked from a list, or typed */
    const pick = h("select", "pv-pick"); pick.hidden = true; pick.setAttribute("aria-label", T("pv.f.model"));
    pick.onchange = () => { if (pick.value) model.value = pick.value; };
    const loadNote = h("small", "dim pv-loadnote"); loadNote.setAttribute("role", "status"); loadNote.setAttribute("aria-live", "polite");
    const loadBtn = btn(T("pv.f.load"), "ghost pv-load", async () => {
      if (!canLoad()) { loadNote.textContent = T("pv.f.load.hint"); return; }              // greyed, but still reachable: tap or focus says why
      loadBtn.setAttribute("aria-busy", "true"); loadNote.textContent = T("pv.f.load.busy");
      const r = await call("POST", "api/llm/models", { type: type.value, base_url: url.value, api_key: key.value, id: existing ? existing.id : "" });
      loadBtn.removeAttribute("aria-busy");
      if (!r.ok) { loadNote.textContent = T("pv.f.load.bad", (r.data.error && r.data.error.message) || explain(r)); return; }
      const list = r.data.models || [];
      pick.replaceChildren(Object.assign(h("option", "", "—"), { value: "" }), ...list.map(m => Object.assign(h("option", "", m.label && m.label !== m.id ? `${m.label} (${m.id})` : m.id), { value: m.id })));
      pick.value = list.some(m => m.id === model.value) ? model.value : ""; pick.hidden = !list.length;
      loadNote.textContent = T("pv.f.load.ok", list.length);
    });
    loadBtn.id = "pv-load-btn"; loadBtn.setAttribute("aria-describedby", "pv-load-hint-" + (existing ? existing.id : "new")); loadNote.id = "pv-load-hint-" + (existing ? existing.id : "new");
    const canLoad = () => {                                                              // the address (typed, or the kind's own) and the key (typed, saved, or none needed)
      const t = state.types.find(x => x.type === type.value) || {};
      if (/_cli$/.test(type.value)) return true;
      return !!((url.value.trim() || t.base_url) && (key.value.trim() || (existing && existing.has_key) || t.auth === "none"));
    };
    const syncLoad = () => { const off = !canLoad(); loadBtn.setAttribute("aria-disabled", String(off)); loadBtn.classList.toggle("is-off", off); loadBtn.title = off ? T("pv.f.load.hint") : ""; if (!off && loadNote.textContent === T("pv.f.load.hint")) loadNote.textContent = ""; };
    [url, key].forEach(i => i.addEventListener("input", syncLoad)); type.addEventListener("change", syncLoad);
    loadBtn.addEventListener("focus", () => { if (!canLoad()) loadNote.textContent = T("pv.f.load.hint"); });
    const modelRow = h("div", "pv-modelrow", model, loadBtn);
    f.append(field("pv-type", T("pv.f.type"), type), field("pv-name", T("pv.f.name"), name), field("pv-url", T("pv.f.url"), url, T("pv.f.url.help")),
      field("pv-model", T("pv.f.model"), modelRow, loadNote), h("div", "pv-pickrow", pick), field("pv-key", T("pv.f.key"), key, T("pv.f.key.help")),
      profileField, field("pv-level", T("pv.f.level"), level), field("pv-cap", T("pv.f.cap"), cap, T("pv.f.cap.help")), ...limitFields);
    syncLoad();
    const testNote = h("p", "setnote pv-testnote"); testNote.setAttribute("role", "status"); testNote.setAttribute("aria-live", "polite");
    const testBtn = btn(T("pv.f.test"), "", async () => {                                // one small request with the form's values; nothing is saved
      testBtn.disabled = true; testNote.dataset.bad = ""; testNote.textContent = T("pv.f.test.busy");
      const r = await call("POST", "api/llm/try", { type: type.value, base_url: url.value, api_key: key.value, model: model.value, id: existing ? existing.id : "" });
      testBtn.disabled = false;
      const ok = r.ok && r.data.ok;
      testNote.dataset.bad = ok ? "" : "1";
      testNote.textContent = ok ? T("pv.f.test.ok", r.data.ms) : T("pv.f.test.bad", (r.data && r.data.error && r.data.error.message) || (r.data && r.data.error) || explain(r));
    });
    f.append(h("div", "pv-actions", btn(existing ? T("pv.save") : T("pv.add"), "primary", async () => {
      const body = { type: type.value, name: name.value, base_url: url.value, model: model.value, api_key: key.value, reasoning_level: level.value, daily_token_cap: cap.value === "" ? 0 : cap.value, profile: here() ? profile.value : "", context_window: here() ? 0 : ctx.value, max_output: here() ? 0 : out.value };
      const r = existing ? await call("POST", `api/llm/providers/${existing.id}`, body) : await call("POST", "api/llm/providers", body);
      if (r.ok) { editing = ""; key.value = ""; say(T("pv.saved")); await refresh(); } else say(explain(r), true);
    }), testBtn, existing ? btn(T("pv.cancel"), "ghost", () => { editing = ""; draw(); }) : null), testNote);
    return f;
  }
  function providersSection() {
    const sec = h("section", "sec pv-providers");
    sec.append(h("div", "sh", h("b", "", T("pv.providers"))), h("p", "setnote", T("pv.providers.help")));
    const list = h("ul", "pv-list");
    if (!state.providers.length) list.append(h("li", "dim", T("pv.none")));
    state.providers.forEach(p => list.append(providerCard(p)));
    sec.append(list);
    const add = h("details", "pv-add"); add.append(h("summary", "", T("pv.new")), form(null));
    sec.append(add);
    if (state.shared.length) {
      sec.append(h("div", "pv-import", h("p", "setnote", T("pv.import.help")), ...state.shared.map(s => {
        const done = state.imported.includes(s.path);
        return btn(done ? T("pv.import.done", s.path) : T("pv.import.copy", s.providers, s.path), "ghost", async () => {
          const r = await call("POST", "api/llm/import", { path: s.path });
          if (r.ok) { state = r.data; say(r.data.already ? T("pv.import.already") : T("pv.import.ok", r.data.imported)); draw(); } else say(explain(r), true);
        });
      })));
    }
    return sec;
  }

  async function draw() {
    if (!state) return;
    const top = await strip();
    const noteEl = h("p", "setnote pv-note", note); noteEl.setAttribute("role", "status"); noteEl.hidden = !note; noteEl.dataset.bad = noteBad ? "1" : "";
    panel.replaceChildren(noteEl, top, ...[fallbackNotice()].filter(Boolean), primarySection(), functionsTable(), externalRequests(), councilSection(), providersSection());
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "loader" && !panel.hidden) refresh(); });
  applyT(view.querySelector(".subtabs"));
  const saved = load(SUB_KEY, "qwen");
  sub(["providers", "converter", "llamacpp", "voicesvc"].includes(saved) ? saved : "qwen");
  if (saved.startsWith("engine:")) keep(SUB_KEY, saved);                       // engines.js restores its own tab
})();
