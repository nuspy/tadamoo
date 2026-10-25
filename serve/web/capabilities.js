"use strict";
/* Settings > Capabilities and visual analysis: the skills, plugins and MCP servers found on this PC as a tree, what the person has
   approved, and the choice for visual analysis. Nothing found is active until the person looks at what it does and presses
   Approve; the approval is tied to the hash they were shown. Also here: the settings of the search online (which sources are asked,
   the minimum score and the weights, whether the best candidate above the minimum is installed without asking) and the list of what
   Basalt installed, with Remove. Everything the server says is put in the page as text. */
(() => {
  const body = document.getElementById("st-caps-body");
  if (!body) return;
  let state = null, filter = "", note = "", noteBad = false, review = null, visionOn = null, signed = {}, waiting = 0, online = null, armed = 0, armTimer = 0;
  const opened = new Set();

  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = fn; return b; };
  const pick = o => (o && (o[LANG] || o.en)) || "";

  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => {
    const e = (r.data && r.data.error) || {};
    if (r.status === 0) return T("cp.e.offline");
    const k = e.code ? "cp.e." + e.code : "";
    return k && T(k) !== k ? T(k) : (e.message || `HTTP ${r.status}`);
  };
  const say = (t, bad) => { note = t; noteBad = !!bad; paintNote(); };
  function paintNote() { const n = body.querySelector(".cp-note"); if (n) { n.textContent = note; n.dataset.bad = noteBad ? "1" : ""; n.hidden = !note; } }

  async function loadSignins() {
    const a = await call("GET", "api/mcp/auth");
    if (a.ok) signed = Object.fromEntries((a.data.servers || []).map(s => [s.id, !!s.signed_in]));
    return signed;
  }
  /* the person signs in in their browser: look again every few seconds until the server says it worked */
  function watchSignin(id, name) {
    clearInterval(waiting);
    let left = 100;
    waiting = setInterval(async () => {
      await loadSignins();
      if (signed[id]) { clearInterval(waiting); say(T("cp.signin.done", name)); draw(); } else if (--left <= 0) { clearInterval(waiting); say(T("cp.signin.late"), true); }
    }, 3000);
  }
  async function signIn(e) {
    const r = await call("POST", "api/mcp/auth/start", { id: e.id });
    if (!r.ok) { say(why(r), true); return; }
    if (!r.data.authorization_url) { say(T(r.data.signed_in ? "cp.signin.already" : "cp.signin.none", e.name)); await loadSignins(); draw(); return; }
    const w = window.open(r.data.authorization_url, "_blank");            // noopener would make it return null
    if (w) w.opener = null;
    say(T(w ? "cp.signin.opened" : "cp.signin.blocked", e.name));
    watchSignin(e.id, e.name);
  }
  async function load() {
    await loadSignins();
    const r = await call("GET", "api/capabilities");
    const o = await call("GET", "api/capabilities/online");
    if (o.ok) online = o.data;
    if (r.ok) { state = r.data; draw(); } else if (!state) body.replaceChildren(h("p", "setnote warnnote", why(r)));
    try {
      const m = await fetch("v1/models", { cache: "no-store", headers: authHeaders() });
      if (m.ok) { const j = await m.json(); visionOn = (j.data || []).some(x => ((x.architecture || {}).input_modalities || []).includes("image")); draw(); }
    } catch (e) { /* the server may be off */ }
  }
  async function act(path, payload) {
    const r = await call("POST", path, payload);
    if (r.ok) { state = r.data; return true; }
    say(why(r), true);
    return false;
  }

  function visualBox() {
    const sel = h("select", "", state.visual_options.map(o => Object.assign(h("option", "", o.id === "builtin" ? T("cp.v.builtin") : o.id === "none" ? T("cp.v.none") : `${o.label} (${o.origin}${o.state !== "approved" ? ", " + T("cp.state." + o.state) : ""})`),
      { value: o.id, disabled: o.state !== "approved" })));
    sel.value = state.settings.visual; sel.id = "cp-visual";
    sel.onchange = async () => { if (await act("api/capabilities/settings", { visual: sel.value })) say(T("cp.saved")); draw(); };
    const vision = visionOn === null ? "" : visionOn ? T("cp.v.on") : T("cp.v.off");
    return h("div", "cp-visual", h("label", "", h("b", "", T("cp.visual")), sel), h("p", "setnote", T("cp.visual.help")), vision ? h("p", "setnote", vision) : null);
  }

  async function saveOnline(changes) {
    const r = await call("POST", "api/capabilities/online", changes);
    if (r.ok) { online = r.data; say(T("cp.saved")); } else say(why(r), true);
    draw();
  }
  function numberField(label, value, fn) {
    const input = h("input"); input.type = "number"; input.min = "0"; input.max = "100"; input.step = "1"; input.value = String(Math.round(value));
    input.setAttribute("aria-label", label);
    input.onchange = () => { const v = Number(input.value); if (input.value === "" || !(v >= 0 && v <= 100)) { draw(); return; } fn(v); };
    return h("label", "cp-num", h("span", "", label), input);
  }
  const when = iso => { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleDateString(LANG); };
  function removeButton(it) {
    const first = armed !== it.id;
    return btn(first ? T("cp.on.remove") : T("cp.on.remove2", it.name), first ? "ghost" : "primary", async () => {
      if (first) {                                                        // the first press only arms it; a second one within a few seconds deletes
        armed = it.id; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = 0; draw(); }, 6000); draw(); return;
      }
      armed = 0; clearTimeout(armTimer);
      const r = await call("POST", "api/capabilities/remove", { id: it.id });
      if (r.ok) { online = r.data; await load(); say(T("cp.on.removed", it.name)); } else { say(why(r), true); draw(); }
    });
  }
  function onlineBox() {
    const s = online.settings, sec = h("section", "cp-online");
    const sources = h("fieldset", "cp-fs", h("legend", "", T("cp.on.sources")));
    online.sources.forEach(k => {
      const box = h("input"); box.type = "checkbox"; box.checked = !!s.sources[k];
      box.onchange = () => saveOnline({ sources: { [k]: box.checked } });
      sources.append(h("label", "cp-check", box, h("span", "", T("cp.src." + k))));
    });
    const weights = h("div", "cp-weights");
    online.weights.forEach(k => weights.append(numberField(T("cp.w." + k), s.weights[k], v => saveOnline({ weights: { [k]: v } }))));
    const market = h("textarea"); market.rows = 3; market.value = s.marketplaces.join("\n"); market.setAttribute("aria-label", T("cp.on.market"));
    market.onchange = () => saveOnline({ marketplaces: market.value.split(/\s*\n\s*/).map(x => x.trim()).filter(Boolean) });
    const auto = h("input"); auto.type = "checkbox"; auto.checked = !!s.auto_install;
    auto.onchange = async () => {
      if (auto.checked && !(await riskConfirm([T("cp.on.risk")]))) { auto.checked = false; return; }
      saveOnline({ auto_install: auto.checked });
    };
    const list = h("ul", "cp-list");
    online.installed.forEach(it => {
      const li = h("li", "cp-entry", h("div", "cp-head", h("b", "", it.name), h("span", "cp-badge", T("cp.kind." + it.kind)), h("span", "cp-badge", it.source),
        it.version ? h("span", "cp-badge", T("cp.on.version", it.version)) : null, when(it.installed_at) ? h("span", "cp-badge", when(it.installed_at)) : null));
      if (it.keys.length) li.append(h("p", "setnote", T("cp.on.keys", it.keys.map(k => k.entry).join(", "))));
      li.append(h("div", "cp-actions", removeButton(it)));
      list.append(li);
    });
    sec.append(h("h3", "", T("cp.on.title")), h("p", "setnote", T("cp.on.help")), h("p", "setnote cp-scorenote", T("cp.on.score")), sources,
      !online.tools.npm ? h("p", "setnote warnnote", T("cp.on.nonpm")) : null, !online.tools.uv && !online.tools.pip ? h("p", "setnote warnnote", T("cp.on.nopy")) : null,
      numberField(T("cp.on.min"), s.min_score, v => saveOnline({ min_score: v })),
      h("fieldset", "cp-fs", h("legend", "", T("cp.on.weights")), h("p", "setnote", T("cp.on.weights.help")), weights),
      h("label", "cp-field", h("span", "", T("cp.on.market")), market),
      h("label", "cp-check", auto, h("span", "", T("cp.on.auto"))), s.auto_install ? riskLabel(T("cp.on.risk")) : null,
      h("h4", "", T("cp.on.installed")), online.installed.length ? list : h("p", "dim", T("cp.on.none")));
    return sec;
  }

  function entryRow(e) {
    const row = h("li", "cp-entry");
    row.append(h("div", "cp-head", h("b", "", e.name), h("span", "cp-badge", T("cp.kind." + e.kind)), h("span", "cp-badge", e.origin),
      h("span", "cp-badge cp-" + e.state, T("cp.state." + e.state))), h("p", "dim cp-desc", e.description));
    if (e.kind !== "builtin") {
      const acts = h("div", "cp-actions");
      if (e.state === "pending") acts.append(btn(T("cp.review"), "", async () => { const r = await call("GET", "api/capabilities/review?id=" + encodeURIComponent(e.id)); if (r.ok) { review = r.data; draw(); } else say(why(r), true); }));
      if (e.remote && e.state === "approved") {
        acts.append(h("span", "cp-badge", T(signed[e.id] ? "cp.signed" : "cp.notsigned")));
        acts.append(btn(T(signed[e.id] ? "cp.signin.again" : "cp.signin"), "", () => signIn(e)));
        if (signed[e.id]) acts.append(btn(T("cp.signout"), "ghost", async () => {
          const o = await call("POST", "api/mcp/auth/signout", { id: e.id });
          if (o.ok) { await loadSignins(); say(T("cp.signout.done", e.name)); draw(); } else say(why(o), true);
        }));
      }
      if (e.state === "approved") acts.append(btn(T("cp.revoke"), "ghost", async () => { if (await act("api/capabilities/revoke", { id: e.id })) say(T("cp.saved")); draw(); }));
      const off = state.settings.off.includes(e.id);
      acts.append(btn(off ? T("cp.turnon") : T("cp.turnoff"), "ghost", async () => {
        const next = off ? state.settings.off.filter(x => x !== e.id) : [...state.settings.off, e.id];
        if (await act("api/capabilities/settings", { off: next })) say(T("cp.saved")); draw();
      }));
      const mv = h("select", "", state.nodes.map(n => Object.assign(h("option", "", n), { value: n })));
      mv.value = e.node; mv.setAttribute("aria-label", T("cp.move", e.name));
      mv.onchange = async () => { if (await act("api/capabilities/move", { id: e.id, node: mv.value })) say(T("cp.saved")); draw(); };
      acts.append(mv);
      row.append(acts);
    }
    if (review && review.entry.id === e.id) {
      row.append(h("div", "cp-review", h("p", "setnote", T("cp.review.help")), h("pre", "cp-text", review.text), h("div", "cp-actions",
        btn(T("cp.approve"), "primary", async () => { const sha = review.sha; review = null; if (await act("api/capabilities/approve", { id: e.id, sha })) say(T("cp.approved", e.name)); draw(); }),
        btn(T("cp.cancel"), "ghost", () => { review = null; draw(); }))));
    }
    return row;
  }

  function tree() {
    const words = filter.toLowerCase().split(/\s+/).filter(Boolean);
    const match = e => !words.length || words.every(w => `${e.name} ${e.description} ${e.id} ${e.node}`.toLowerCase().includes(w));
    const sec = h("div", "cp-tree");
    Object.entries(state.roots).forEach(([root, info]) => {
      const mine = state.entries.filter(e => e.node.split("/")[0] === root && match(e));
      if (!mine.length) return;
      const det = h("details", "cp-root"); det.open = opened.has(root) || !!words.length;
      det.ontoggle = () => { if (det.open) opened.add(root); else opened.delete(root); };
      det.append(h("summary", "", `${pick(info)} (${mine.length})`), h("ul", "cp-list", mine.map(entryRow)));
      sec.append(det);
    });
    if (!sec.children.length) sec.append(h("p", "dim", T("cp.none")));
    return sec;
  }

  function draw() {
    if (!state) return;
    const search = h("input"); search.type = "search"; search.value = filter; search.placeholder = T("cp.search"); search.setAttribute("aria-label", T("cp.search"));
    search.oninput = () => { filter = search.value; const pos = search.selectionStart; draw(); const s = body.querySelector("input[type=search]"); if (s) { s.focus(); s.setSelectionRange(pos, pos); } };
    const n = h("p", "setnote cp-note", note); n.setAttribute("role", "status"); n.hidden = !note; n.dataset.bad = noteBad ? "1" : "";
    body.replaceChildren(h("p", "setnote", T("cp.help")), n, visualBox(), online ? onlineBox() : null,
      h("div", "cp-bar", search, btn(T("cp.rescan"), "", async () => { const r = await call("POST", "api/capabilities/rescan", {}); if (r.ok) { state = r.data; say(T("cp.rescanned", state.entries.length)); } else say(why(r), true); draw(); })),
      h("p", "setnote", T("cp.top", state.tree_top)), tree());
  }

  document.addEventListener("basalt:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
