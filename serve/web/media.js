"use strict";
/* Settings > General > Pictures and video: one line that says whether the chat and the agent can make pictures and video now (LTX Studio found, its
   environment and weights there, the switch on) and, for what cannot be made, why. Read only; the switch and the paths are changed through the server's
   route. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("md-root");
  if (!root) return;

  async function read() {
    try {
      const r = await fetch("api/media", { cache: "no-store", headers: authHeaders() });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }
  function why(k) {
    if (k.error) return k.error;
    if (k.missing && k.missing.length) return k.missing.join(", ");
    return "?";
  }
  function lines(s) {
    if (!s) return [T("md.e.offline")];
    if (!s.settings.enabled) return [T("md.off")];
    const kinds = ["image", "video"], can = kinds.filter(k => s[k] && s[k].ready), cannot = kinds.filter(k => !(s[k] && s[k].ready));
    const out = [];
    if (can.length) out.push(T("md.can", can.map(k => T("md." + k)).join(", ")));
    else out.push(T("md.nothing"));
    cannot.forEach(k => out.push(T("md.cannot", T("md." + k), why(s[k] || {}))));
    return out;
  }
  async function draw() {
    const box = document.createElement("div");
    lines(await read()).forEach(t => {
      const p = document.createElement("p");
      p.className = "setnote";
      p.textContent = t;
      box.appendChild(p);
    });
    box.setAttribute("role", "status");
    root.replaceChildren(box);
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") draw(); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "settings") draw(); });
  if (document.body.dataset.view === "settings") draw();
})();
