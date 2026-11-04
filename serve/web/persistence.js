"use strict";
/* The "work until done" switch in the chat header, for the Chat and the coding Agent alike: while it is on, the agent goes on by itself when the
   request is not met. One choice for every conversation, kept on the server (api/persistence), so it holds for new conversations and after a restart. */
(() => {
  const btn = document.getElementById("wud-toggle");
  if (!btn) return;
  let on = true;
  const status = t => { const s = document.getElementById("chat-status"); if (s) s.textContent = t; };
  function paint() {
    btn.textContent = T(on ? "wud.on" : "wud.off");
    btn.title = T("wud.tip");
    btn.setAttribute("aria-pressed", String(on));
  }
  async function load() {
    try {
      const r = await fetch("api/persistence", { cache: "no-store", headers: authHeaders() });
      if (r.ok) on = !!(await r.json()).on;
    } catch (e) { /* the server is not there yet: the default shows */ }
    paint();
  }
  btn.onclick = async () => {
    try {
      const r = await fetch("api/persistence", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ on: !on }) });
      if (!r.ok) { status(T("wud.fail")); return; }
      on = !!(await r.json()).on;
      status(T(on ? "wud.now.on" : "wud.now.off"));
    } catch (e) { status(T("wud.fail")); }
    paint();
  };
  document.addEventListener("tadamoo:lang", paint);
  paint();
  load();
})();
