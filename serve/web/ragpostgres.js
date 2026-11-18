"use strict";
/* Knowledge > a separate PostgreSQL of our own, next to the per-project Database block: install, start and stop. A project still points its own database at this
   one the same way as any other PostgreSQL (the Database block, "use an existing one"), with the host, port and user this page shows and the superuser password
   kept server-side only (Connect and prepare reads it there; this page never shows it). Nothing is downloaded until the Install button is pressed. */
(() => {
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };
  const why = r => (r.status === 0 ? T("rp.e.offline") : ((r.data && r.data.error && r.data.error.message) || `HTTP ${r.status}`));
  const mb = n => `${(n / 1e6).toFixed(1)} MB`;
  async function api(method, path, body) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  let view = null, note = "", bad = false, busy = "", open = false, repaint = () => {};

  async function load() {
    const r = await api("GET", "api/rag/postgres");
    if (r.ok) view = r.data; else { note = why(r); bad = true; }
    repaint();
  }
  async function act(name, path) {
    busy = name; note = ""; repaint();
    const r = await api("POST", path, {});
    busy = "";
    if (r.ok) { view = r.data; note = T("rp.done"); bad = false; } else { note = why(r); bad = true; }
    repaint();
  }

  function build() {
    const box = el("details", "rg-settings rp-box");
    const sum = el("summary", "rg-sum", T("rp.title")); box.append(sum);
    box.addEventListener("toggle", () => { open = box.open; });
    box.open = open;
    if (!view) { box.append(el("p", "rg-help", T("rp.loading"))); return box; }
    box.append(el("p", "rg-help", T("rp.help")));
    if (view.found.programs.length) box.append(el("p", "rg-help", T("rp.found", view.found.programs.map(p => p.path).join(", "))));
    if (view.found.listening.length) box.append(el("p", "rg-help", T("rp.listening", view.found.listening.join(", "))));
    if (view.found.broken.length) box.append(el("p", "rg-help", T("rp.broken", view.found.broken.map(p => p.path).join(", "))));
    if (!view.found.programs.length && !view.found.listening.length) box.append(el("p", "rg-help", T("rp.none")));
    view.options.forEach(o => { if (o.id !== "skip") box.append(el("p", "rg-dmeta", "• " + T("rp.opt." + o.id))); });

    const own = view.own;
    if (own.installed) {
      box.append(el("p", "rg-line", T(own.running ? "rp.own.running" : "rp.own.stopped", own.host ? `${own.host}:${own.port}` : "")));
      if (own.running) box.append(el("p", "rg-help", T("rp.own.connect", own.host, own.port, own.user, own.database)));
      box.append(btn(T(own.running ? "rp.stop" : "rp.start"), "", () => act("own", `api/rag/postgres/${own.running ? "stop" : "start"}`)));
    } else {
      const inst = btn(T(busy === "install" ? "rp.running" : "rp.install"), "primary", () => act("install", "api/rag/postgres/install"));
      inst.disabled = !!busy;
      box.append(el("p", "rg-help", T("rp.install.help", view.release.version, mb(view.release.bytes))), inst);
    }
    const status = el("p", "rg-note", note); status.setAttribute("role", "status"); status.dataset.bad = bad ? "1" : ""; status.hidden = !note;
    box.append(status);
    return box;
  }

  window.RagPostgres = {
    block(again) { repaint = again || (() => {}); if (!view) load(); return build(); },
  };
})();
