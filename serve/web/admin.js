"use strict";
/* Settings > Admin: restarts the server from the page, the same way whatever started it would (a plain process started again, or a named service
   stopped and started through its own manager). Asks once before doing it: there is no undo for a restart. */
(() => {
  const root = document.getElementById("adm-root");
  if (!root) return;
  let busy = false, note = "", bad = false;

  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = el("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  async function restart() {
    if (busy) return;
    busy = true; note = ""; draw();
    try {
      const r = await fetch("api/admin/restart", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() }, body: "{}" });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      note = r.ok ? T(data.way === "service" ? "adm.restart.service" : "adm.restart.relaunch", data.name || "") : T("adm.e.offline");
      bad = !r.ok;
    } catch (e) {
      note = T("adm.e.offline"); bad = true;
    }
    busy = false; draw();
  }

  function confirmRestart() {
    const dlg = el("dialog", "rg-dlg");
    const go = btn(T("adm.restart.go"), "rg-warn", () => { dlg.close(); restart(); });
    dlg.append(el("h3", "rg-h2", T("adm.restart.confirm.title")), el("p", "rg-help", T("adm.restart.confirm.help")),
      go, btn(T("rg.cancel"), "", () => dlg.close()));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }

  function draw() {
    const box = el("div", "adm-box");
    box.append(el("p", "setnote", T("adm.restart.help")));
    const go = btn(T(busy ? "adm.restart.going" : "adm.restart"), "primary", confirmRestart);
    go.disabled = busy;
    box.append(go);
    if (note) { const p = el("p", "setnote", note); p.setAttribute("role", "status"); p.dataset.bad = bad ? "1" : ""; box.append(p); }
    root.replaceChildren(box);
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") draw(); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "settings") draw(); });
  if (document.body.dataset.view === "settings") draw();
})();
