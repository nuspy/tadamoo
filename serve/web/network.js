"use strict";
/* Settings > Network: where the server listens (host and port), the names it is reached by (the public name and the allowed host names), a test that says whether an address answers and whether the
   Host rule lets its name through (also behind a reverse proxy), and a short guided set-up. The values go to the server's config file (server.*); a change of host or port applies when the
   server is started again, and the page says so. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("nw-root");
  if (!root) return;
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.addEventListener("click", fn); return b; };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  const why = r => (r.status === 0 ? T("nw.e.offline") : ((r.data.error || {}).message || `HTTP ${r.status}`));

  /* ---- the pure parts: the words for what the server's checks found ---- */
  const lines = text => String(text || "").split(/[\n,;\s]+/).map(s => s.trim()).filter(Boolean);
  function explain(check) {
    if (check.id === "rule") {
      const out = [];
      if (check.ok) out.push({ ok: true, text: T("nw.rule.ok", check.name) });
      else if (check.after_restart) out.push({ ok: null, text: T("nw.rule.later", check.name) });
      else out.push({ ok: false, text: T("nw.rule.no", check.name) });
      if (check.off_by_key) out.push({ ok: null, text: T("nw.rule.key") });
      return out;
    }
    if (check.id === "listen") {
      const where = `${check.host}:${check.port}`;
      if (check.answers) return [{ ok: true, text: check.running ? T("nw.listen.ok", where) : T("nw.listen.other", where) }];
      return [{ ok: check.running ? false : null, text: check.running ? T("nw.listen.no", where, check.reason || "") : T("nw.listen.later", where) }];
    }
    if (check.id === "reach") {
      if (check.ok) {
        const s = check.seen || {};
        return [{ ok: true, text: T("nw.reach.ok", check.url) + " " + T("nw.reach.seen", s.host || "?") + (s.proto ? " " + T("nw.reach.proxy", s.proto, s.forwarded_host || s.host || "?") : "") }];
      }
      if (check.error === "host_not_allowed") return [{ ok: false, text: T("nw.reach.rule", check.url) }];
      if (check.status) return [{ ok: false, text: T("nw.reach.http", check.url, check.status) }];
      return [{ ok: false, text: T("nw.reach.no", check.url, check.error || "?") }];
    }
    return [];
  }
  window.NetworkSettings = { lines, explain };

  /* ---- the form ---- */
  const S = { data: null, msg: "", bad: false, wizard: 0, results: [], testing: false };
  const row = (node, help) => h("div", "nw-field", node, help ? h("p", "nw-help", help) : null);
  const input = (id, type, value, extra) => { const i = h("input", "nw-input"); i.id = id; i.type = type; i.value = value == null ? "" : String(value); i.autocomplete = "off"; Object.assign(i, extra || {}); return i; };
  let host, port, pub, allowed, scheme, status;
  const labelFor = (el, text) => { const l = h("label", "nw-label", text); l.htmlFor = el.id; return l; };

  function say(text, bad) { S.msg = text || ""; S.bad = !!bad; if (status) { status.textContent = S.msg; status.classList.toggle("bad", S.bad); } }

  function build() {
    const d = S.data, run = d.running, saved = d.saved || {}, cli = new Set(d.from_command_line || []);
    const editable = !!d.editable;
    host = input("nw-host", "text", saved.host !== undefined ? saved.host : run.host, { disabled: !editable, spellcheck: false });
    port = input("nw-port", "number", saved.port !== undefined ? saved.port : run.port, { disabled: !editable, min: "1", max: "65535", inputMode: "numeric" });
    pub = input("nw-public", "text", saved.public_name !== undefined ? saved.public_name : run.public_name, { disabled: !editable, spellcheck: false, placeholder: "tadamoo.example.org" });
    allowed = h("textarea", "nw-input nw-allowed"); allowed.id = "nw-allowed"; allowed.rows = 3; allowed.disabled = !editable; allowed.spellcheck = false;
    allowed.value = (saved.allowed_hosts !== undefined ? saved.allowed_hosts : run.allowed_hosts).join("\n");
    scheme = h("select", "nw-input"); scheme.id = "nw-scheme";
    [["https", "https"], ["http", "http"]].forEach(([v, t]) => { const o = h("option", "", t); o.value = v; scheme.append(o); });
    status = h("p", "nw-status"); status.setAttribute("role", "status"); status.setAttribute("aria-live", "polite");
    const pending = h("p", "nw-pending");
    pending.hidden = !d.pending.length;
    if (d.pending.length) pending.textContent = T("nw.pending", d.pending.map(k => T("nw.f." + k)).join(", "));
    const out = h("ul", "nw-results"); out.id = "nw-results";
    const save = btn(T("nw.save"), "primary nw-btn", async () => {
      save.disabled = true;
      const r = await call("POST", "api/network", { host: host.value.trim(), port: port.value === "" ? null : Number(port.value), allowed_hosts: lines(allowed.value), public_name: pub.value.trim() });
      save.disabled = false;
      if (!r.ok) { say(T("nw.e.save", why(r)), true); return; }
      await load(true);
      say(T("nw.saved"));
    });
    save.disabled = !editable;
    const test = btn(T("nw.test"), "nw-btn", async () => {
      test.disabled = true; out.replaceChildren(h("li", "dim", T("nw.testing")));
      const name = pub.value.trim().toLowerCase(), calls = [];
      calls.push(call("POST", "api/network/test", { host: host.value.trim() || run.host, port: Number(port.value) || run.port }));
      if (name) calls.push(call("POST", "api/network/test", { name, scheme: scheme.value }));
      const res = await Promise.all(calls);
      out.replaceChildren();
      res.forEach(r => {
        if (!r.ok) { out.append(h("li", "nw-res bad", T("nw.e.test", why(r)))); return; }
        (r.data.checks || []).forEach(c => explain(c).forEach(x => out.append(h("li", "nw-res " + (x.ok === true ? "ok" : x.ok === false ? "bad" : "warn"), h("span", "nw-mark", x.ok === true ? "✓" : x.ok === false ? "✗" : "!"), h("span", "nw-text", x.text)))));
      });
      test.disabled = false;
    });
    const listenOpts = h("div", "nw-quick",
      btn(T("nw.quick.local"), "nw-btn", () => { host.value = "127.0.0.1"; }),
      btn(T("nw.quick.network"), "nw-btn", () => { host.value = "0.0.0.0"; }));
    if (!editable) listenOpts.querySelectorAll("button").forEach(b => { b.disabled = true; });
    const note = (key) => cli.has(key) ? h("p", "nw-help", T("nw.cli")) : null;
    const wiz = wizard(editable);
    const keybox = keyBox();
    root.replaceChildren(
      h("p", "help", T("nw.help")),
      d.reason ? h("p", "nw-reason", T("nw.reason." + d.reason)) : null,
      h("fieldset", "nw-set", h("legend", "", T("nw.listen")), h("p", "nw-help", T("nw.listen.help")),
        row(h("div", "nw-pair", h("div", "nw-col", labelFor(host, T("nw.f.host")), host), h("div", "nw-col nw-narrow", labelFor(port, T("nw.f.port")), port))),
        listenOpts, note("host"), note("port")),
      h("fieldset", "nw-set", h("legend", "", T("nw.names")), h("p", "nw-help", T("nw.names.help")),
        row(h("div", "nw-col", labelFor(pub, T("nw.f.public_name")), pub), T("nw.public.help")),
        row(h("div", "nw-col", labelFor(allowed, T("nw.f.allowed_hosts")), allowed), T("nw.allowed.help")), note("public-name"), note("allowed-hosts")),
      pending,
      h("div", "nw-actions", save, wiz.open, h("span", "nw-testbox", labelFor(scheme, T("nw.scheme")), scheme), test),
      status, out, wiz.box, keybox,
      h("p", "nw-help", T("nw.own", d.own_name, (d.addresses || []).join(", ") || "-")));
  }

  /* ---- the server's key: changed here, applied at once, and this page goes on with the new one ---- */
  function keyBox() {
    const cur = input("nw-key-cur", "password", typeof apiKey === "string" ? apiKey : "", { autocomplete: "current-password" });
    const next = input("nw-key-new", "password", "", { autocomplete: "new-password" });
    const again = input("nw-key-again", "password", "", { autocomplete: "new-password" });
    const msg = h("p", "nw-status"); msg.setAttribute("role", "status"); msg.setAttribute("aria-live", "polite");
    const go = btn(T("nw.key.change"), "primary nw-btn", async () => {
      msg.classList.remove("bad");
      if (!next.value) { msg.textContent = T("nw.key.empty"); msg.classList.add("bad"); next.focus(); return; }
      if (next.value !== again.value) { msg.textContent = T("nw.key.mismatch"); msg.classList.add("bad"); again.focus(); return; }
      go.disabled = true;
      const r = await call("POST", "api/server-key", { current: cur.value, new: next.value });
      go.disabled = false;
      if (!r.ok) { msg.textContent = T("nw.key.err", why(r)); msg.classList.add("bad"); return; }
      if (window.setServerKey) window.setServerKey(next.value);
      cur.value = next.value; next.value = ""; again.value = "";
      msg.textContent = T("nw.key.done");
    });
    const asked = h("p", "nw-help", "");
    call("GET", "api/server-key").then(r => { if (r.ok) asked.textContent = r.data.set ? T("nw.key.set") : T("nw.key.none"); });
    return h("fieldset", "nw-set", h("legend", "", T("nw.key")), h("p", "nw-help", T("nw.key.help")), asked,
      row(h("div", "nw-col", labelFor(cur, T("nw.key.cur")), cur)), row(h("div", "nw-col", labelFor(next, T("nw.key.new")), next)), row(h("div", "nw-col", labelFor(again, T("nw.key.again")), again)),
      h("div", "nw-actions", go), msg);
  }

  /* ---- the guided set-up: how the server is reached, then the fields it needs; Save and Test are the same buttons ---- */
  function wizard(editable) {
    const box = h("section", "nw-wiz"); box.hidden = true; box.setAttribute("aria-label", T("nw.wiz.title"));
    const open = btn(T("nw.wiz.open"), "nw-btn", () => { S.wizard = 1; draw(); box.hidden = false; box.scrollIntoView({ block: "nearest" }); });
    open.disabled = !editable;
    let choice = "";
    function draw() {
      const body = h("div", "nw-wiz-body");
      const step = S.wizard;
      body.append(h("h3", "nw-wiz-h", T("nw.wiz.step", step, 3)));
      if (step === 1) {
        body.append(h("p", "", T("nw.wiz.q")));
        [["local", "nw.wiz.local"], ["network", "nw.wiz.network"], ["proxy", "nw.wiz.proxy"]].forEach(([v, key]) => {
          const id = "nw-wiz-" + v, r = h("input", ""); r.type = "radio"; r.name = "nw-wiz"; r.value = v; r.id = id; r.checked = choice === v;
          r.addEventListener("change", () => { choice = v; });
          const l = h("label", "nw-wiz-opt", r, h("span", "", T(key)));
          body.append(l);
        });
      } else if (step === 2) {
        body.append(h("p", "", T("nw.wiz.do." + (choice || "local"))));
      } else {
        body.append(h("p", "", T("nw.wiz.done")));
      }
      const nav = h("div", "nw-wiz-nav");
      nav.append(btn(T("nw.wiz.cancel"), "nw-btn", () => { box.hidden = true; S.wizard = 0; }));
      if (step > 1) nav.append(btn(T("nw.wiz.back"), "nw-btn", () => { S.wizard = step - 1; draw(); }));
      if (step < 3) nav.append(btn(T("nw.wiz.next"), "primary nw-btn", () => {
        if (step === 1) {
          if (!choice) { say(T("nw.wiz.pick"), true); return; }
          say("");
          if (choice === "local") { host.value = "127.0.0.1"; pub.value = ""; }
          if (choice === "network") host.value = "0.0.0.0";
          if (choice === "proxy") { host.value = "127.0.0.1"; scheme.value = "https"; pub.focus(); }
        }
        S.wizard = step + 1; draw();
      }));
      box.replaceChildren(body, nav);
    }
    return { box, open };
  }

  async function load(quiet) {
    const r = await call("GET", "api/network");
    if (!r.ok) { root.replaceChildren(h("p", "nw-reason", T("nw.e.load", why(r)))); return; }
    S.data = r.data;
    build();
    if (!quiet) say("");
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings" && !S.data) load(); });
  if (document.body.dataset.view === "settings") load();
  const strip = document.getElementById("viewsub");
  if (strip) strip.addEventListener("click", () => { if (!S.data) load(); });
})();
