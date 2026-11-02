"use strict";
/* The switch of the whole technical session in the chat header: on unless the person turns it off here. While it is off no quick job
   (summaries, routing, checks, cards, lessons) is made; the conversation itself works as before. One choice, kept on the server. */
(() => {
  const btn = document.getElementById("tech-toggle");
  if (!btn) return;
  let on = true;
  const status = t => { const s = document.getElementById("chat-status"); if (s) s.textContent = t; };
  function paint() {
    btn.textContent = T(on ? "tech.on" : "tech.off");
    btn.title = T("tech.tip");
    btn.setAttribute("aria-pressed", String(on));
  }
  async function load() {
    try {
      const r = await fetch("api/technical-session", { cache: "no-store", headers: authHeaders() });
      if (r.ok) on = !!(await r.json()).on;
    } catch (e) { /* the server is not there yet: the default shows */ }
    paint();
  }
  btn.onclick = async () => {
    try {
      const r = await fetch("api/technical-session", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ on: !on }) });
      if (!r.ok) { status(T("tech.fail")); return; }
      on = !!(await r.json()).on;
      status(T(on ? "tech.now.on" : "tech.now.off"));
    } catch (e) { status(T("tech.fail")); }
    paint();
  };
  document.addEventListener("basalt:lang", paint);
  paint();
  load();
})();
