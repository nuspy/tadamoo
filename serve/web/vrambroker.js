"use strict";
/* Settings > General > VRAM lent: one line that says whether the model's expert cache has lent VRAM to a heavy function now, and what is free.
   Read only; the hold time and the switch are changed through the server's route. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("vq-root");
  if (!root) return;
  let timer = 0;

  const gb = mib => T("vq.gb", (mib / 1024).toFixed(1));
  async function read() {
    try {
      const r = await fetch("api/vram/leases", { cache: "no-store", headers: authHeaders() });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }
  function text(s) {
    if (!s) return T("vq.e.offline");
    if (!s.enabled) return T("vq.off");
    const free = s.free_mib == null ? T("vq.unknown") : gb(s.free_mib), l = s.leases[0];
    if (l) return T("vq.lent", gb(l.granted_mib), l.owner, Math.round(l.expires_in_s), free);
    return T("vq.idle", free, s.hold_s);
  }
  async function draw() {
    clearTimeout(timer);
    const p = document.createElement("p");
    p.className = "setnote";
    p.id = "vq-line";
    p.setAttribute("role", "status");
    p.textContent = text(await read());
    root.replaceChildren(p);
    if (document.body.dataset.view === "settings") timer = setTimeout(draw, 5000);
  }
  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") draw(); else clearTimeout(timer); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "settings") draw(); });
  if (document.body.dataset.view === "settings") draw();
})();
