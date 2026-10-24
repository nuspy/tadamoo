"use strict";
/* Models > Providers: which provider each internal function of Basalt uses (the Chat, the agent's loop, the planner, the judges),
   and the providers themselves (a model in this PC, a cloud service, a command-line agent, Jev that judges).
   What other programs see of this server does not change: it is the loaded model. Keys are write-only: they go to the server,
   which keeps them in the Windows credential manager; this page is only told that there is one. Everything the server says is
   put in the page as text. */
(() => {
  const SUB_KEY = "basalt.models.sub";
  const view = document.getElementById("view-loader");
  const panel = document.getElementById("ld-providers"), qwen = document.getElementById("ld-qwen");
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

  let state = null, busy = false, editing = "", note = "", noteBad = false;

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
    const prov = which === "providers";
    panel.hidden = !prov; qwen.hidden = prov;
    document.getElementById("sub-providers").setAttribute("aria-selected", String(prov));
    document.getElementById("sub-qwen").setAttribute("aria-selected", String(!prov));
    keep(SUB_KEY, which);
    if (prov) refresh();
  }
  document.getElementById("sub-providers").onclick = () => sub("providers");
  document.getElementById("sub-qwen").onclick = () => sub("qwen");

  /* ---- reading ---- */
  async function refresh() {
    const r = await call("GET", "api/llm");
    if (r.ok) { state = r.data; draw(); } else if (!state) { panel.replaceChildren(h("p", "setnote warnnote", explain(r))); }
  }

  /* ---- the strip: what is loaded and who uses it ---- */
  async function strip() {
    const box = h("section", "sec pv-strip");
    let st = null;
    try { const r = await fetch("v1/status", { cache: "no-store", headers: authHeaders() }); if (r.ok) st = await r.json(); } catch (e) { /* offline */ }
    const users = state.functions.filter(f => !f.provider).map(f => f.label);
    box.append(h("b", "", T("pv.loaded")), " ", st && st.loaded ? st.model : T("pv.noneLoaded"),
      h("span", "dim", " · " + T("pv.usedBy", users.length ? users.join(", ") : "—")));
    return box;
  }

  /* ---- the table of functions ---- */
  function providerOptions(f) {
    const opts = [Object.assign(h("option", "", T("pv.local")), { value: "" })];
    state.providers.forEach(p => {
      if (f.kind === "chat" && p.judge_only) return;
      if (f.kind === "chat" && f.tools && p.agent_only) return;
      opts.push(Object.assign(h("option", "", `${p.name} (${p.is_local ? T("pv.onpc") : T("pv.outside")})`), { value: p.id }));
    });
    return opts;
  }
  function functionsTable() {
    const sec = h("section", "sec pv-functions");
    sec.append(h("div", "sh", h("b", "", T("pv.functions"))), h("p", "setnote", T("pv.functions.help")));
    const table = h("table", "pv-table");
    table.append(h("thead", "", h("tr", "", ["pv.fn", "pv.data", "pv.provider", "pv.consent", "pv.status"].map(k => h("th", "", T(k))))));
    const body = h("tbody");
    state.functions.forEach(f => {
      const sel = h("select", "", providerOptions(f));
      sel.value = f.provider; sel.setAttribute("aria-label", f.label);
      sel.onchange = async () => {
        const r = await call("POST", "api/llm/assign", { function: f.id, provider: sel.value });
        if (r.ok) { state = r.data; say(T("pv.saved")); } else { say(explain(r), true); sel.value = f.provider; }
        draw();
      };
      const outside = f.provider && !(state.providers.find(p => p.id === f.provider) || {}).is_local;
      const consent = h("input"); consent.type = "checkbox"; consent.checked = f.consent; consent.disabled = !f.needs_consent;
      consent.setAttribute("aria-label", T("pv.consent.for", f.label));
      consent.onchange = async () => { const r = await call("POST", "api/llm/consent", { function: f.id, allowed: consent.checked }); if (r.ok) state = r.data; else say(explain(r), true); draw(); };
      body.append(h("tr", "", h("td", "", f.label), h("td", "", T("pv.data." + f.data)), h("td", "", sel),
        h("td", "", f.needs_consent ? consent : h("span", "dim", "—"), outside && !f.consent ? h("span", "dim", " " + T("pv.consent.need")) : null),
        h("td", f.ok ? "" : "pv-bad", f.provider ? (f.ok ? T("pv.ok") : f.why) : T("pv.local.short"))));
    });
    table.append(body);
    sec.append(h("div", "pv-scroll", table));
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
    const name = h("input"), url = h("input"), model = h("input"), key = h("input"), cap = h("input"), level = h("select", "", state.reasoning_levels.map(l => Object.assign(h("option", "", l.label || l.value), { value: l.value })));
    key.type = "password"; key.autocomplete = "off"; key.spellcheck = false;
    cap.type = "number"; cap.min = "0"; cap.step = "1000"; cap.inputMode = "numeric";
    [name, url, model].forEach(i => { i.type = "text"; i.autocomplete = "off"; i.spellcheck = false; });
    if (existing) { type.value = existing.type; type.disabled = true; name.value = existing.name || ""; url.value = existing.base_url || ""; model.value = existing.model || ""; cap.value = existing.daily_token_cap || ""; level.value = existing.reasoning_level || "inherit"; }
    const hint = () => { const t = state.types.find(x => x.type === type.value) || {}; url.placeholder = t.base_url || ""; key.placeholder = existing && existing.has_key ? T("pv.key.keep") : ""; };
    type.onchange = hint; hint();
    f.append(field("pv-type", T("pv.f.type"), type), field("pv-name", T("pv.f.name"), name), field("pv-url", T("pv.f.url"), url, T("pv.f.url.help")),
      field("pv-model", T("pv.f.model"), model), field("pv-key", T("pv.f.key"), key, T("pv.f.key.help")),
      field("pv-level", T("pv.f.level"), level), field("pv-cap", T("pv.f.cap"), cap, T("pv.f.cap.help")));
    f.append(h("div", "pv-actions", btn(existing ? T("pv.save") : T("pv.add"), "primary", async () => {
      const body = { type: type.value, name: name.value, base_url: url.value, model: model.value, api_key: key.value, reasoning_level: level.value, daily_token_cap: cap.value === "" ? 0 : cap.value };
      const r = existing ? await call("POST", `api/llm/providers/${existing.id}`, body) : await call("POST", "api/llm/providers", body);
      if (r.ok) { editing = ""; key.value = ""; say(T("pv.saved")); await refresh(); } else say(explain(r), true);
    }), existing ? btn(T("pv.cancel"), "ghost", () => { editing = ""; draw(); }) : null));
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
    panel.replaceChildren(noteEl, top, functionsTable(), providersSection());
  }

  document.addEventListener("basalt:view", e => { if (e.detail === "loader" && !panel.hidden) refresh(); });
  applyT(view.querySelector(".subtabs"));
  sub(load(SUB_KEY, "qwen") === "providers" ? "providers" : "qwen");
})();
