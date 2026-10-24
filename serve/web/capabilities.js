"use strict";
/* Settings > Capabilities and visual analysis: the skills, plugins and MCP servers found on this PC as a tree, what the person has
   approved, and the choice for visual analysis. Nothing found is active until the person looks at what it does and presses
   Approve; the approval is tied to the hash they were shown. Everything the server says is put in the page as text. */
(() => {
  const body = document.getElementById("st-caps-body");
  if (!body) return;
  let state = null, filter = "", note = "", noteBad = false, review = null, visionOn = null;
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

  async function load() {
    const r = await call("GET", "api/capabilities");
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

  function entryRow(e) {
    const row = h("li", "cp-entry");
    row.append(h("div", "cp-head", h("b", "", e.name), h("span", "cp-badge", T("cp.kind." + e.kind)), h("span", "cp-badge", e.origin),
      h("span", "cp-badge cp-" + e.state, T("cp.state." + e.state))), h("p", "dim cp-desc", e.description));
    if (e.kind !== "builtin") {
      const acts = h("div", "cp-actions");
      if (e.state === "pending") acts.append(btn(T("cp.review"), "", async () => { const r = await call("GET", "api/capabilities/review?id=" + encodeURIComponent(e.id)); if (r.ok) { review = r.data; draw(); } else say(why(r), true); }));
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
    body.replaceChildren(h("p", "setnote", T("cp.help")), n, visualBox(),
      h("div", "cp-bar", search, btn(T("cp.rescan"), "", async () => { const r = await call("POST", "api/capabilities/rescan", {}); if (r.ok) { state = r.data; say(T("cp.rescanned", state.entries.length)); } else say(why(r), true); draw(); })),
      h("p", "setnote", T("cp.top", state.tree_top)), tree());
  }

  document.addEventListener("basalt:view", e => { if (e.detail === "settings") load(); });
  if (document.body.dataset.view === "settings") load();
})();
