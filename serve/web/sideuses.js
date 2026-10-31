"use strict";
/* Settings > Inference > Technical session, and the dialog that lists every use of it (GET/POST api/side-uses): for each use the switch,
   its numbers, its own instructions for the model (never the conversation's), the work counted so far, and the way back to the defaults.
   While the dialog is open the counters and the jobs running are refreshed; the list itself is drawn again only after a change, so what
   is being typed is not lost. Everything the server says is put in the page as text. */
(() => {
  const dlg = document.getElementById("su-dlg");
  if (!dlg) return;
  const open = new Set();
  let state = null, timer = 0;

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const num = n => new Intl.NumberFormat(LANG === "it" ? "it-IT" : "en-US").format(n);
  const say = (text, bad) => { const n = document.getElementById("su-note"); n.textContent = text || ""; n.dataset.bad = bad ? "1" : ""; };

  async function llm(path, payload) {                            // the providers page's own routes: one function, `technical`
    const r = await fetch(path, payload === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(payload) });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  function paintRoute() {
    const route = state.route, sel = document.getElementById("su-route");
    sel.replaceChildren(h("option", "", T("su.route.local")), ...route.providers.map(p => h("option", "", p.name + (p.model ? " · " + p.model : "") + (p.is_local ? "" : " · " + T("su.route.cloud")))));
    [...sel.options].forEach((o, i) => { o.value = i ? route.providers[i - 1].id : ""; });
    sel.value = route.provider;
    const consent = document.getElementById("su-route-consent");
    consent.checked = route.consent;
    document.getElementById("su-route-consent-row").hidden = !route.provider || !route.needs_consent;
    document.getElementById("su-external").checked = state.external.on;
    document.getElementById("su-route-state").textContent = route.provider ? (route.ok ? T("su.route.ok") : T("su.route.not_ok", route.why)) : T("su.route.here");
  }

  async function call(payload) {
    const opts = payload === undefined ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(payload) };
    const r = await fetch("api/side-uses", opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error((j.error && j.error.message) || `HTTP ${r.status}`);
    return j;
  }

  const statsText = u => {
    const s = u.stats || {};
    return s.calls ? T("su.stats", num(s.calls), num(s.main), num(s.cut), (s.seconds / s.calls).toFixed(1)) : T("su.stats.none");
  };

  function paintState() {
    if (!state) return;
    const on = state.uses.filter(u => u.on).length, text = state.session.available
      ? T("su.state.on", num(state.session.tokens), on, state.uses.length) : T("su.state.off");
    document.getElementById("su-state").textContent = text;
    document.getElementById("su-dlg-state").textContent = text;
    dlg.dataset.off = state.session.available ? "" : "1";
    paintRoute();
  }

  function paintRunning() {
    const box = document.getElementById("su-running"), list = document.getElementById("su-running-list");
    box.hidden = !state.running.length;
    list.replaceChildren(...state.running.map(r => h("li", "", r.total ? T("su.running.row", T("su.u." + r.use), r.done, r.total) : T("su.u." + r.use))));
  }

  async function post(payload, saved) {
    try {
      state = await call(payload);
      say(saved ? T("su.saved") : "");
      draw();
    } catch (e) { say(e.message, true); }
  }

  function numberField(u, p, off) {
    const id = `su-${u.id}-${p.key}`;
    const input = h("input"); input.type = "number"; input.id = id; input.value = String(p.value); input.min = String(p.min); input.max = String(p.max);
    input.step = p.integer ? "1" : "0.5"; input.inputMode = "decimal"; input.disabled = off; input.dataset.key = p.key;
    return h("div", "sud-num", Object.assign(h("label", "", T("su.p." + p.key)), { htmlFor: id }), input,
      h("small", "", T("su.p.range", num(p.min), num(p.max)) + " · " + T("su.p.default", num(p.default))));
  }

  function row(u) {
    const off = !state.session.available;
    const det = h("details", "sud-use"); det.dataset.id = u.id; det.dataset.off = u.on ? "" : "1"; det.open = open.has(u.id);
    det.addEventListener("toggle", () => { if (det.open) open.add(u.id); else open.delete(u.id); });
    const sw = h("input"); sw.type = "checkbox"; sw.checked = u.on; sw.disabled = off; sw.id = "su-on-" + u.id;
    sw.addEventListener("change", () => post({ use: u.id, on: sw.checked }));
    const sl = h("label", "sud-switch", sw, h("span", "", u.on ? T("su.on") : T("su.off")));
    sl.addEventListener("click", e => e.stopPropagation());                 // the switch does not open or close the row
    det.append(h("summary", "", h("span", "sud-name", T("su.u." + u.id)),
      u.needs_session ? h("span", "sud-tag", T("su.needs.tag")) : null, u.default_on === false ? h("span", "sud-tag", T("su.default_off")) : null, u.changed ? h("span", "sud-tag", T("su.changed")) : null, sl));
    const more = h("div", "sud-more", h("p", "", T("su.u." + u.id + ".d")));
    if (!u.on) more.append(h("p", "sud-dim", u.needs_session ? T("su.needs") : T("su.fallback")));
    more.append(Object.assign(h("p", "sud-dim", statsText(u)), { id: "su-stats-" + u.id }));
    if (u.params.length) more.append(h("div", "sud-nums", u.params.map(p => numberField(u, p, off))));
    let area = null;
    if (u.prompt || u.editable) {
      area = h("textarea", "sud-prompt"); area.id = "su-prompt-" + u.id; area.value = u.prompt; area.readOnly = !u.editable || off; area.spellcheck = false;
      more.append(Object.assign(h("label", "", T("su.prompt")), { htmlFor: area.id }), area,
        h("p", "sud-dim", u.editable ? T("su.prompt.edit") : T("su.prompt.fixed")));
    } else more.append(h("p", "sud-dim", T("su.prompt.built")));
    const save = h("button", "btn primary", T("su.save")); save.type = "button"; save.disabled = off || (!u.params.length && !u.editable);
    save.onclick = () => {
      const params = {};
      more.querySelectorAll("input[data-key]").forEach(i => { params[i.dataset.key] = Number(i.value); });
      post({ use: u.id, params, ...(u.editable && area ? { system: area.value === u.prompt_default ? "" : area.value } : {}) }, true);
    };
    const restore = h("button", "btn ghost", T("su.restore")); restore.type = "button"; restore.disabled = off || !u.changed;
    restore.onclick = () => post({ reset: u.id }, true);
    more.append(h("div", "sud-actions", save, restore));
    det.append(more);
    return det;
  }

  function draw() {
    if (!state) return;
    paintState();
    paintRunning();
    document.getElementById("su-list").replaceChildren(...state.uses.map(row));
    document.getElementById("su-defaults").disabled = !state.session.available || !state.uses.some(u => u.changed);
  }

  async function refresh() {
    try {
      const fresh = await call();
      if (!state) { state = fresh; draw(); return; }
      state.session = fresh.session; state.running = fresh.running;
      fresh.uses.forEach((u, i) => { if (state.uses[i]) state.uses[i].stats = u.stats; });
      paintState(); paintRunning();
      state.uses.forEach(u => { const el = document.getElementById("su-stats-" + u.id); if (el) el.textContent = statsText(u); });
    } catch (e) { say(T("su.failed", e.message), true); }
  }

  async function load() {
    try { state = await call(); draw(); } catch (e) { document.getElementById("su-state").textContent = T("su.failed", e.message); }
  }

  document.getElementById("su-open").addEventListener("click", async () => {
    await load();
    say("");
    dlg.showModal();
    clearInterval(timer);
    timer = setInterval(() => { if (!dlg.open) clearInterval(timer); else refresh(); }, 3000);
  });
  document.getElementById("su-close").addEventListener("click", () => dlg.close());
  document.getElementById("su-route").addEventListener("change", async e => {
    try { await llm("api/llm/assign", { function: "technical", provider: e.target.value || null }); state = await call(); say(T("su.saved")); draw(); } catch (err) { say(err.message, true); state = await call().catch(() => state); draw(); }
  });
  document.getElementById("su-route-consent").addEventListener("change", async e => {
    try { await llm("api/llm/consent", { function: "technical", allowed: e.target.checked }); state = await call(); say(T("su.saved")); draw(); } catch (err) { say(err.message, true); }
  });
  document.getElementById("su-external").addEventListener("change", e => post({ external: e.target.checked }, true));
  dlg.addEventListener("close", () => { clearInterval(timer); load(); });
  document.getElementById("su-defaults").addEventListener("click", () => post({ reset: "all" }, true));
  document.getElementById("su-stop").addEventListener("click", async () => {
    try { state = await call({ stop: true }); say(T("su.stopped")); draw(); } catch (e) { say(e.message, true); }
  });
  document.addEventListener("basalt:side", () => { if (document.body.dataset.view === "settings") load(); });
  document.addEventListener("basalt:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
  applyT(dlg);
})();
