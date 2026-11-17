"use strict";
/* Connectors: skills, MCP servers, MCP apps and plugins on this PC, and the personal skills, in one list. One filter, a switch for personal / external / all, a
   sub-tab for each kind, and two bands (installed above, installable below). Text from skills and servers enters the page as text only. */
(function () {
  const root = document.getElementById("cx-root");
  if (!root) return;

  const SUBTABS = ["skill", "mcp_server", "mcp_app", "plugin", "providers"];
  const S = { sub: "skill", scope: "all", query: "", state: "", data: null, detail: "", tree: null, busy: false, off: null };
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const errText = r => (r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`;
  const statusEl = document.getElementById("cx-status");
  const say = (text, bad) => { statusEl.textContent = text; statusEl.classList.toggle("bad", !!bad); };

  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* the providers: where the connectors that are not installed yet are listed from. Rows the person can switch off, remove or add to; the list can be reset. */
  async function loadProviders() {
    const r = await api("GET", "api/connectors/providers");
    if (!r.ok) { say(errText(r), true); return; }
    S.providers = r.data; say(""); draw();
  }
  async function changeProviders(body) {
    const r = await api("POST", "api/connectors/providers", body);
    if (!r.ok) { say(errText(r), true); return; }
    S.providers = r.data; say(""); draw();
  }
  function providersView() {
    const host = document.createDocumentFragment(), d = S.providers;
    if (!d) return host;
    host.append(el("p", "help", T("cx.prov.help")));
    const ul = el("ul", "cx-list");
    d.providers.forEach(p => {
      const li = el("li", "cx-item cx-" + (p.enabled ? "active" : "disabled")), head = el("div", "cx-open");
      head.append(el("span", "cx-name", p.ref), el("span", "cx-badge", T("cx.prov.kind." + p.kind)), el("span", "cx-badge", T("cx.prov.trust." + p.trust)), el("span", "cx-badge", T(p.enabled ? "cx.state.active" : "cx.state.disabled")));
      const acts = el("div", "cx-acts");
      acts.append(btn(T(p.enabled ? "cx.disable" : "cx.enable"), "", () => changeProviders({ action: p.enabled ? "disable" : "enable", id: p.id })), btn(T("cx.prov.remove"), "", () => changeProviders({ action: "remove", id: p.id })));
      li.append(head, acts); ul.append(li);
    });
    host.append(ul);
    const form = el("form", "cx-provform"), kind = el("select"), ref = el("input"), trust = el("select");
    kind.id = "cx-pk"; ref.id = "cx-pr"; trust.id = "cx-pt"; ref.type = "text"; ref.maxLength = 500; ref.autocomplete = "off"; ref.spellcheck = false;
    d.kinds.forEach(k => { const o = el("option", "", T("cx.prov.kind." + k)); o.value = k; kind.append(o); });
    d.trust.forEach(k => { const o = el("option", "", T("cx.prov.trust." + k)); o.value = k; trust.append(o); });
    trust.value = "unknown";
    const lab = (id, key, input) => { const w = el("div", "cx-pf"), l = el("label", "", T(key)); l.htmlFor = id; w.append(l, input); return w; };
    const add = btn(T("cx.prov.add"), "primary", () => {}); add.type = "submit";
    form.append(lab("cx-pk", "cx.prov.f.kind", kind), lab("cx-pr", "cx.prov.f.ref", ref), lab("cx-pt", "cx.prov.f.trust", trust), add);
    form.onsubmit = e => { e.preventDefault(); changeProviders({ action: "add", kind: kind.value, ref: ref.value, trust: trust.value }); };
    host.append(form, btn(T("cx.prov.reset"), "", () => changeProviders({ action: "reset" })));
    return host;
  }

  async function load() {
    if (S.sub === "providers") { await loadProviders(); return; }
    const q = new URLSearchParams({ scope: S.scope, kind: S.sub, q: S.query });
    if (S.state) q.set("state", S.state);
    const [view, caps] = await Promise.all([api("GET", "api/connectors?" + q), api("GET", "api/capabilities")]);
    if (!view.ok) { say(errText(view), true); return; }
    S.data = view.data;
    S.off = caps.ok ? ((caps.data.settings || {}).off || []) : null;
    say("");
    draw();
  }

  async function setEnabled(item, enabled) {
    if (S.off === null) { say(T("cx.cannot"), true); return; }
    const off = new Set(S.off);
    if (enabled) off.delete(item.id); else off.add(item.id);
    const r = await api("POST", "api/capabilities/settings", { off: [...off] });
    if (!r.ok) { say(errText(r), true); return; }
    await load();
    if (S.detail === item.id) showDetail(item.id);
  }

  async function showDetail(id) {
    S.detail = id;
    const r = await api("GET", "api/connectors/item?id=" + encodeURIComponent(id));
    S.tree = r.ok ? r.data : null;
    if (!r.ok) say(errText(r), true);
    draw();
  }

  const stateText = s => T("cx.state." + s);

  function row(item) {
    const li = el("li", "cx-item cx-" + item.state + (S.detail === item.id ? " sel" : ""));
    const open = btn("", "cx-open", () => showDetail(S.detail === item.id ? "" : item.id));
    open.append(el("span", "cx-name", item.name), el("span", "cx-badge", stateText(item.state)), el("span", "cx-src", item.source));
    if (item.kind === "plugin") open.append(el("span", "cx-tri", T("cx.tri." + item.tri)));
    open.setAttribute("aria-expanded", String(S.detail === item.id));
    li.append(open);
    const acts = el("div", "cx-acts");
    const switchable = S.off !== null && item.scope === "external" && item.kind !== "plugin";
    if (switchable) {
      const on = item.state !== "disabled";
      acts.append(btn(on ? T("cx.disable") : T("cx.enable"), "", () => setEnabled(item, !on), on ? T("cx.disable.tip") : T("cx.enable.tip")));
    }
    if (acts.childNodes.length) li.append(acts);
    if (S.detail === item.id && S.tree) li.append(detail(S.tree));
    return li;
  }

  function detail(d) {
    const box = el("div", "cx-detail");
    box.append(el("p", "cx-desc", d.description || T("cx.nodesc")));
    if (d.keywords.length) box.append(el("p", "help", T("cx.keywords", d.keywords.join(", "))));
    if (d.why) box.append(el("p", "cx-why", d.why));
    if (d.used_by.length) box.append(el("p", "help", T("cx.usedby", d.used_by.join(", "))));
    if (d.tree && d.tree.length) {
      box.append(el("p", "cx-treeh", T("cx.tree")));
      const ul = el("ul", "cx-tree");
      d.tree.forEach(c => {
        const li = el("li", "cx-leaf cx-" + c.state);
        li.append(el("span", "cx-name", c.name), el("span", "cx-badge", stateText(c.state)));
        if (S.off !== null && c.scope === "external" && c.kind !== "plugin") {
          const on = c.state !== "disabled";
          li.append(btn(on ? T("cx.disable") : T("cx.enable"), "", () => setEnabled(c, !on)));
        }
        ul.append(li);
      });
      box.append(ul);
      box.append(el("p", "help", T("cx.removal", d.removal.removed.length, d.removal.kept.length)));
    }
    return box;
  }

  function band(title, items, emptyText) {
    const sec = el("section", "cx-band");
    sec.append(el("h3", "cx-bh", `${title} (${items.length})`));
    if (!items.length) { sec.append(el("p", "help", emptyText)); return sec; }
    const ul = el("ul", "cx-list");
    items.forEach(i => ul.append(row(i)));
    sec.append(ul);
    return sec;
  }

  function draw() {
    const d = S.data;
    const bar = document.getElementById("cx-subtabs");
    bar.replaceChildren();
    SUBTABS.forEach(k => {
      const b = btn(T("cx.sub." + k), "cx-sub" + (S.sub === k ? " on" : ""), () => { S.sub = k; S.detail = ""; S.tree = null; load(); });
      b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(S.sub === k));
      bar.append(b);
    });
    const scopes = document.getElementById("cx-scope");
    scopes.replaceChildren();
    ["all", "personal", "external"].forEach(s => {
      const n = d ? d.counts[s] : 0;
      const b = btn(`${T("cx.scope." + s)} ${n}`, "cx-sc" + (S.scope === s ? " on" : ""), () => { S.scope = s; load(); });
      b.setAttribute("aria-pressed", String(S.scope === s));
      scopes.append(b);
    });
    const host = document.getElementById("cx-bands");
    host.replaceChildren();
    document.getElementById("cx-top").hidden = S.sub === "providers";
    if (S.sub === "providers") { host.append(providersView()); return; }
    if (!d) return;
    host.append(band(T("cx.installed"), d.installed, T("cx.none")), band(T("cx.available"), d.available, T("cx.available.none")));
  }

  const filter = document.getElementById("cx-filter");
  let timer = 0;
  filter.addEventListener("input", () => { S.query = filter.value; clearTimeout(timer); timer = setTimeout(load, 250); });
  const stateSel = document.getElementById("cx-state");
  stateSel.addEventListener("change", () => { S.state = stateSel.value; load(); });

  const page = document.getElementById("view-connectors");
  new MutationObserver(() => { if (!page.hidden) load(); }).observe(page, { attributes: true, attributeFilter: ["hidden"] });
  if (!page.hidden) load();
})();
