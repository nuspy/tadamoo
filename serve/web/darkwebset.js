"use strict";
/* Settings > Inference > Dark web: the switch of the dark web option, what it may do (search, read) and its Tor. Off until it is switched on; it applies to the conversations started after.
   The Tor bundle is downloaded only when its button is pressed (official file, hash and signature checked first). Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("dw-root");
  if (!root) return;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick, tip) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; if (tip) b.title = tip; return b; };
  const why = r => (r.status === 0 ? T("dw.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }
  let view = null, busy = "", note = "", bad = false, armed = false, armTimer = 0;

  async function load() {
    const r = await api("GET", "api/darkweb");
    if (r.ok) view = r.data; else { note = why(r); bad = true; }
    draw();
  }
  async function change(body) {
    const r = await api("POST", "api/darkweb", body);
    if (r.ok) { view = r.data; note = T("dw.saved"); bad = false; } else { note = why(r); bad = true; }
    draw();
  }
  async function tor(action) {
    busy = action; note = ""; draw();
    const r = await api("POST", "api/darkweb/tor/" + action, {});
    busy = "";
    if (r.ok) { view = r.data; note = T("dw.tor." + action + ".done"); bad = false; } else { note = why(r); bad = true; }
    draw();
  }
  function check(id, key, on, onchange, disabled) {
    const wrap = el("div", "dw-check"), c = el("input"), l = el("label", "", T(key));
    c.type = "checkbox"; c.id = id; c.checked = !!on; c.disabled = !!disabled; l.htmlFor = id;
    c.onchange = () => onchange(c.checked);
    wrap.append(c, l);
    return wrap;
  }

  function draw() {
    const box = [];
    if (!view) {
      const p = el("p", bad ? "cx-why" : "help", bad ? note : T("dw.loading")); if (bad) p.setAttribute("role", "alert");
      const kids = [p]; if (bad) kids.push(el("p", "help", T("dw.restart")), btn(T("dw.retry"), "", load));
      root.replaceChildren(...kids); return;
    }
    if (!view.package) { root.replaceChildren(el("p", "cx-why", T("dw.nopackage"))); return; }
    const s = view.settings || {};
    box.push(el("p", "help", T("dw.help")));
    box.push(check("dw-on", "dw.on", s.darkweb, v => change({ darkweb: v })));
    box.push(el("p", "help", T("dw.on.help")));
    box.push(check("dw-search", "dw.search", s.search, v => change({ search: v }), !s.darkweb));
    box.push(check("dw-read", "dw.read", s.read, v => change({ read: v }), !s.darkweb));
    box.push(check("dw-watches", "dw.watches", s.watches, v => change({ watches: v }), !s.darkweb));
    box.push(el("p", "help", T("dw.watches.help")));
    const nav = el("select"), navId = "dw-nav";
    nav.id = navId; nav.disabled = !s.darkweb;
    ["own", "agent-browser"].forEach(v => { const o = el("option", "", T("dw.nav." + v)); o.value = v; nav.append(o); });
    nav.value = s.navigation || "own";
    nav.onchange = () => change({ navigation: nav.value });
    const navLab = el("label", "", T("dw.nav")); navLab.htmlFor = navId;
    const navRow = el("div", "dw-nav"); navRow.append(navLab, nav);
    box.push(navRow, el("p", "help", T("dw.nav.help")));
    box.push(el("p", "help", T("dw.writeoff")));
    const t = view.tor || {}, line = el("p", "dw-tor", T(t.running ? "dw.tor.running" : t.installed ? "dw.tor.installed" : "dw.tor.missing"));
    box.push(line);
    const bar = el("div", "dw-bar");
    if (!t.installed) {
      bar.append(btn(armed ? T("dw.tor.install.again") : T("dw.tor.install"), armed ? "primary" : "", () => {
        if (armed) { armed = false; tor("install"); } else { armed = true; clearTimeout(armTimer); armTimer = setTimeout(() => { armed = false; draw(); }, 4000); draw(); }
      }, T("dw.tor.install.tip")));
      if (!view.gpg) box.push(el("p", "cx-why", T("dw.nogpg")));
    } else {
      bar.append(btn(T("dw.tor.start"), "", () => tor("start"), T("dw.tor.start.tip")), btn(T("dw.tor.stop"), "", () => tor("stop"), T("dw.tor.stop.tip")));
    }
    [...bar.children].forEach(b => { b.disabled = !!busy; });
    bar.id = "dw-bar";
    box.push(bar);
    if (busy) { const p = el("p", "help", T("dw.working")); p.setAttribute("role", "status"); box.push(p); }
    if (note) { const p = el("p", bad ? "cx-why" : "help", note); p.setAttribute("role", bad ? "alert" : "status"); box.push(p); }
    root.replaceChildren(...box);
  }
  /* what a saved inference profile keeps of this section, and what loading one does: the three switches are written to the server (the file of the option), the Tor of this PC is left as it is */
  window.dwSet = {
    read: () => { const s = (view && view.settings) || {}; return { darkweb: !!s.darkweb, darkweb_search: s.search !== false, darkweb_read: s.read !== false, darkweb_navigation: s.navigation || "own", darkweb_watches: !!s.watches }; },
    write: async s => {
      const body = {};
      if (s.darkweb !== undefined) body.darkweb = !!s.darkweb;
      if (s.darkweb_search !== undefined) body.search = !!s.darkweb_search;
      if (s.darkweb_read !== undefined) body.read = !!s.darkweb_read;
      if (s.darkweb_navigation !== undefined) body.navigation = s.darkweb_navigation;
      if (s.darkweb_watches !== undefined) body.watches = !!s.darkweb_watches;
      if (Object.keys(body).length && view && view.package) await change(body);
    },
  };
  load();
  document.addEventListener("tadamoo:lang", draw);
})();
