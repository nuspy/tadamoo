"use strict";
/* Connectors: skills, MCP servers, MCP apps and plugins on this PC, and the personal skills, in one list. One filter, a switch for personal / external / all, a
   sub-tab for each kind, and two bands (installed above, installable below). Text from skills and servers enters the page as text only. */
(function () {
  const root = document.getElementById("cx-root");
  if (!root) return;

  const SUBTABS = ["skill", "mcp_server", "mcp_app", "plugin", "providers"];
  const S = { sub: "skill", scope: "all", query: "", state: "", data: null, detail: "", tree: null, busy: false, off: null, reviews: {}, armed: "", installing: "", failed: {}, originals: null, updates: null, checking: false };
  let armTimer = 0;
  const ARM_MS = 4000;
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
    loadOriginals();
  }
  async function changeProviders(body) {
    const r = await api("POST", "api/connectors/providers", body);
    if (!r.ok) { say(errText(r), true); return; }
    S.providers = r.data; say(""); draw();
  }
  /* where the downloaded originals are kept: the installed copy is the one that may be changed, the original stays as it came */
  async function loadOriginals() {
    const r = await api("GET", "api/connectors/settings");
    if (r.ok) { S.originals = r.data; draw(); }
  }
  function originalsBlock() {
    const box = el("section", "cx-orig"), input = el("input");
    input.type = "text"; input.id = "cx-orig-dir"; input.maxLength = 500; input.autocomplete = "off"; input.spellcheck = false; input.value = S.originals ? S.originals.originals_dir : "";
    const lab = el("label", "", T("cx.orig.title")); lab.htmlFor = "cx-orig-dir";
    const save = btn(T("cx.orig.save"), "", async () => {
      const r = await api("POST", "api/connectors/settings", { originals_dir: input.value.trim() });
      if (r.ok) { S.originals = r.data; say(T("cx.orig.saved"), false); draw(); } else say(errText(r), true);
    }, T("cx.orig.save.tip"));
    save.id = "cx-orig-save";
    const row = el("div", "cx-pf"); row.append(input, save);
    box.append(lab, el("p", "help", T("cx.orig.help")), row);
    return box;
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
    host.append(originalsBlock());
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

  /* the dependency tree of a plugin in a window of its own: the plugin and what it brings (servers, skills), each with its state, its description, what else uses it and its own switch */
  async function treeDialog(id) {
    const dlg = el("dialog", "cx-dlg");
    dlg.id = "cx-treedlg";
    dlg.onclose = () => dlg.remove();
    const fill = async () => {
      const r = await api("GET", "api/connectors/item?id=" + encodeURIComponent(id));
      if (!r.ok) { dlg.replaceChildren(el("p", "cx-why", errText(r)), btn(T("cx.tree.close"), "", () => dlg.close())); return; }
      const d = r.data, body = el("div", "cx-treebody");
      dlg.setAttribute("aria-label", T("cx.tree.title", d.name));
      body.append(el("h3", "cx-bh", T("cx.tree.title", d.name)), el("p", "help", T("cx.tree.help")));
      const ul = el("ul", "cx-tree cx-treebig");
      [d, ...(d.tree || [])].forEach(n => {
        const li = el("li", "cx-leaf cx-" + n.state + (n.id === d.id ? " cx-root" : ""));
        li.dataset.node = n.id;
        const head = el("div", "cx-leafhead");
        head.append(el("span", "cx-name", n.name), el("span", "cx-badge", stateText(n.state)), el("span", "cx-src", T("cx.kind." + n.kind)));
        li.append(head, el("p", "cx-desc", n.description || T("cx.nodesc")));
        if (n.why) li.append(el("p", "cx-why", n.why));
        if (n.used_by && n.used_by.length) li.append(el("p", "help", T("cx.usedby", n.used_by.join(", "))));
        if (S.off !== null && n.scope === "external" && n.kind !== "plugin") {
          const on = n.state !== "disabled";
          const b = btn(on ? T("cx.disable") : T("cx.enable"), "", async () => { await setEnabled(n, !on); await fill(); }, on ? T("cx.disable.tip") : T("cx.enable.tip"));
          b.id = "cx-tn-" + n.id.replace(/[^a-z0-9]+/gi, "-");
          li.append(b);
        }
        ul.append(li);
      });
      body.append(ul, el("p", "help", T("cx.removal", d.removal.removed.length, d.removal.kept.length)));
      const close = btn(T("cx.tree.close"), "primary", () => dlg.close()); close.id = "cx-tree-close";
      dlg.replaceChildren(body, close);
    };
    await fill();
    document.body.append(dlg);
    dlg.showModal();
  }

  async function showDetail(id) {
    S.detail = id;
    const r = await api("GET", "api/connectors/item?id=" + encodeURIComponent(id));
    S.tree = r.ok ? r.data : null;
    if (!r.ok) say(errText(r), true);
    draw();
  }

  const stateText = s => T("cx.state." + s);

  /* the facts sheet and the advisory review of an item that is not installed yet: the sheet is counted by a program, the review is advice from a model and can be wrong */
  async function reviewItem(id) {
    S.reviews[id] = { busy: true };
    draw();
    const r = await api("POST", "api/connectors/review", { id });
    S.reviews[id] = r.ok ? r.data : { error: errText(r) };
    draw();
  }
  /* installing and removing: the press of the button is the authorisation, and it takes two presses (the second says what will happen) */
  function arm(key) { S.armed = key; clearTimeout(armTimer); armTimer = setTimeout(() => { S.armed = ""; draw(); }, ARM_MS); draw(); }
  function twice(key, label, again, tip, run, id) {
    const armed = S.armed === key;
    const b = btn(armed ? again : label, armed ? "primary" : "", () => { if (armed) { S.armed = ""; run(); } else arm(key); }, tip);
    if (id) b.id = id;
    return b;
  }
  async function installItem(d, flags) {
    S.installing = d.id; delete S.failed[d.id]; draw();
    const r = await api("POST", "api/connectors/install", { id: d.id, ...(flags || {}) });
    S.installing = "";
    if (r.ok) { delete S.reviews[d.id]; S.detail = ""; S.tree = null; say(T("cx.done.installed", r.data.installed.name || d.name), false); await load(); return; }
    S.failed[d.id] = { message: errText(r), code: (r.data && r.data.error && r.data.error.code) || "" };
    draw();
  }
  /* updates: asks GitHub and the registry for the newest version of what was installed from here; an update installs over it (the old one comes back if the new one fails) */
  async function checkUpdates() {
    S.checking = true; draw();
    const r = await api("POST", "api/connectors/updates", {});
    S.checking = false;
    if (r.ok) { S.updates = Object.fromEntries(r.data.updates.map(u => [u.id, u])); say(r.data.updates.length ? T("cx.upd.some", r.data.updates.length) : T("cx.upd.none") + (r.data.errors.length ? " " + T("cx.upd.errors", r.data.errors.length) : ""), false); }
    else say(errText(r), true);
    draw();
  }
  async function updateItem(item) {
    S.installing = item.id; draw();
    const r = await api("POST", "api/connectors/update", { id: item.id });
    S.installing = "";
    if (r.ok) { delete S.updates[item.id]; say(T("cx.upd.done", item.name), false); await load(); return; }
    say(errText(r), true); draw();
  }

  /* configure: the variables an installed server reads. A secret is typed here once and goes to the vault; the page never gets it back (it only says whether it is set) */
  async function configureDialog(item) {
    const r = await api("GET", "api/connectors/config?id=" + encodeURIComponent(item.id));
    if (!r.ok) { say(errText(r), true); return; }
    const d = r.data, dlg = el("dialog", "cx-dlg"), form = el("form", "cx-cfg"), inputs = {};
    dlg.id = "cx-cfgdlg"; dlg.setAttribute("aria-label", T("cx.cfg.title", d.name));
    form.append(el("h3", "cx-bh", T("cx.cfg.title", d.name)), el("p", "help", T("cx.cfg.help")));
    if (!d.variables.length) form.append(el("p", "help", d.remote ? T("cx.cfg.remote") : T("cx.cfg.none")));
    d.variables.forEach((v, n) => {
      const wrap = el("div", "cx-pf"), id = "cx-cv-" + n, lab = el("label", "", v.name + (v.required ? " *" : "")), inp = el("input");
      lab.htmlFor = id; inp.id = id; inp.type = v.secret ? "password" : "text"; inp.autocomplete = "off"; inp.spellcheck = false; inp.maxLength = 4000;
      inp.value = v.secret ? "" : v.value; inp.placeholder = v.secret ? (v.set ? T("cx.cfg.kept") : T("cx.cfg.empty")) : "";
      inputs[v.name] = { inp, secret: v.secret };
      wrap.append(lab, inp, el("p", "help", v.secret ? T(v.set ? "cx.cfg.secret.set" : "cx.cfg.secret.unset") : T("cx.cfg.plain")));
      form.append(wrap);
    });
    const save = btn(T("cx.cfg.save"), "primary", () => {}); save.type = "submit"; save.id = "cx-cfg-save"; save.disabled = !d.variables.length;
    const close = btn(T("cx.tree.close"), "", () => dlg.close());
    const bar = el("div", "dw-bar"); bar.append(save, close);
    const note = el("p", "help"); note.setAttribute("role", "status");
    form.append(bar, note);
    form.onsubmit = async e => {
      e.preventDefault();
      const values = {};
      Object.entries(inputs).forEach(([name, f]) => { if (!f.secret || f.inp.value) values[name] = f.inp.value; });                   // an empty secret leaves what is there
      const w = await api("POST", "api/connectors/configure", { id: item.id, values });
      if (w.ok) { note.textContent = T("cx.cfg.saved"); note.className = "help"; Object.values(inputs).forEach(f => { if (f.secret) f.inp.value = ""; }); await load(); }
      else { note.textContent = errText(w); note.className = "cx-why"; note.setAttribute("role", "alert"); }
    };
    dlg.onclose = () => dlg.remove();
    dlg.append(form);
    document.body.append(dlg); dlg.showModal();
  }

  async function uninstallItem(item) {
    S.installing = item.id; draw();
    const r = await api("POST", "api/connectors/uninstall", { id: item.id });
    S.installing = "";
    if (r.ok) { S.detail = ""; S.tree = null; say(T("cx.removed", item.name), false); await load(); return; }
    say(errText(r), true); draw();
  }
  function installControls(d) {
    const box = el("div", "cx-install"), bad = S.failed[d.id];
    if (S.installing === d.id) { const p = el("p", "help", T("cx.installing")); p.setAttribute("role", "status"); box.append(p); return box; }
    if (bad) {
      const p = el("p", "cx-why", bad.message); p.setAttribute("role", "alert"); box.append(p);
      if (bad.code === "installed") box.append(btn(T("cx.update"), "", () => installItem(d, { update: true }), T("cx.update.tip")));
      if (bad.code === "integrity") box.append(btn(T("cx.anyway"), "", () => installItem(d, { anyway: true }), T("cx.anyway.tip")));
      return box;
    }
    box.append(twice("i" + d.id, T("cx.install"), T("cx.install.again"), T("cx.install.tip"), () => installItem(d, {}), "cx-install-go"));
    return box;
  }
  const factLine = (label, values) => { const items = (values || []).filter(Boolean); return items.length ? el("li", "", `${label}: ${items.join(", ")}`) : null; };
  function reviewBlock(d) {
    const box = el("section", "cx-review"), got = S.reviews[d.id];
    box.setAttribute("aria-label", T("cx.review.title"));
    if (!got) {
      const go = btn(T("cx.review.go"), "", () => reviewItem(d.id), T("cx.review.go.tip")); go.id = "cx-review-go";
      box.append(go);
      return box;
    }
    if (got.busy) { const p = el("p", "help", T("cx.review.busy")); p.setAttribute("role", "status"); box.append(p); return box; }
    if (got.error) { const p = el("p", "cx-why", got.error); p.setAttribute("role", "alert"); box.append(p); box.append(btn(T("cx.review.go"), "", () => reviewItem(d.id))); return box; }
    const f = got.facts, rv = got.review;
    box.append(el("h4", "cx-treeh", T("cx.review.sheet")));
    const ul = el("ul", "cx-facts");
    [factLine(T("cx.f.commands"), f.commands), factLine(T("cx.f.hosts"), f.hosts), factLine(T("cx.f.secrets"), f.secrets), factLine(T("cx.f.repository"), [f.repository]),
     factLine(T("cx.f.scripts"), f.scripts), factLine(T("cx.f.tools"), [f.allowed_tools]), ...Object.entries(f.signs || {}).map(([k, v]) => factLine(k, v)), ...(f.notes || []).map(n => el("li", "", n))]
      .filter(Boolean).forEach(li => ul.append(li));
    box.append(ul);
    box.append(el("h4", "cx-treeh", T("cx.review.advice")));
    const verdict = el("p", "cx-verdict cx-v-" + rv.verdict, T("cx.verdict." + rv.verdict) + (rv.summary ? ": " + rv.summary : "") + (rv.note ? " (" + rv.note + ")" : ""));
    box.append(verdict);
    if (rv.risks && rv.risks.length) { const r = el("ul", "cx-facts"); rv.risks.forEach(x => r.append(el("li", "", x))); box.append(r); }
    box.append(el("p", "help", T("cx.review.warn")));
    box.append(installControls(d));
    return box;
  }

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
    if (item.installed && item.source === "tadamoo") {                           // what TaDaMoo installed from here can be removed from here
      acts.append(S.installing === item.id ? el("span", "help", T("cx.installing")) : twice("u" + item.id, T("cx.uninstall"), T("cx.uninstall.again"), T("cx.uninstall.tip"), () => uninstallItem(item), "cx-uninstall-" + item.id.replace(/[^a-z0-9]+/gi, "-")));
    }
    if (item.installed && item.source === "tadamoo") {
      const cfg = btn(T("cx.cfg"), "", () => configureDialog(item), T("cx.cfg.tip")); cfg.id = "cx-config-" + item.id.replace(/[^a-z0-9]+/gi, "-");
      acts.append(cfg);
      const up = S.updates && S.updates[item.id];
      if (up) acts.append(twice("p" + item.id, T("cx.upd", up.to), T("cx.upd.again"), T("cx.upd.tip", up.from, up.to), () => updateItem(item), "cx-update-" + item.id.replace(/[^a-z0-9]+/gi, "-")));
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
    if (!d.installed && /^(registry|folder|github):/.test(d.id)) box.append(reviewBlock(d));
    if (d.used_by.length) box.append(el("p", "help", T("cx.usedby", d.used_by.join(", "))));
    if (d.tree && d.tree.length) {
      box.append(el("p", "cx-treeh", T("cx.tree")));
      if (d.kind === "plugin") { const open = btn(T("cx.tree.open"), "", () => treeDialog(d.id), T("cx.tree.open.tip")); open.id = "cx-tree-open"; box.append(open); }
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
    const chk = btn(S.checking ? T("cx.upd.checking") : T("cx.upd.check"), "", checkUpdates, T("cx.upd.check.tip")); chk.id = "cx-upd-check"; chk.disabled = S.checking;
    host.append(chk, band(T("cx.installed"), d.installed, T("cx.none")), band(T("cx.available"), d.available, T("cx.available.none")));
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
