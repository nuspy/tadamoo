"use strict";
/* The Council box in the header of a conversation: while it is on, every real question of the conversation goes to the council first and the
   model answers with its verdict read. The choice is kept on the server with the conversation, so it stays on until it is turned off. The
   model-only chat has no harness and does not offer it. */
(function () {
  const label = document.getElementById("council-label"), box = document.getElementById("council-on");
  if (!label || !box) return;

  let pending = false;                                    // the box as set before the conversation exists
  const chat = () => window.agentChat || null;
  const withSessions = () => !!(chat() && chat().active());
  const current = () => { const rec = withSessions() ? chat().record() : null; return rec ? rec.council === true : pending; };

  function paint() {
    const view = document.body.dataset.view;
    label.hidden = !(view === "chat" || view === "agent") || !withSessions();
    if (label.hidden) return;
    if (document.activeElement !== box) box.checked = current();
    label.title = T("co.tip");
  }
  box.onchange = async () => {
    const sid = withSessions() ? chat().current() : "";
    if (!sid) { pending = box.checked; paint(); return; }
    let ok = false;
    try {
      const r = await fetch(`api/agent/sessions/${encodeURIComponent(sid)}/council`, { method: "POST", cache: "no-store",
        headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify({ on: box.checked }) });
      ok = r.ok;
    } catch (e) { /* offline: the box goes back */ }
    if (!ok) { const s = document.getElementById("chat-status"); if (s) s.textContent = T("co.fail"); paint(); return; }
    chat().refresh();
  };
  window.councilUi = { pending: () => withSessions() && !chat().current() && box.checked, paint };
  document.addEventListener("tadamoo:conversation", () => { pending = false; setTimeout(paint, 0); });
  document.addEventListener("tadamoo:view", () => setTimeout(paint, 0));
  document.getElementById("chatmode").addEventListener("change", () => setTimeout(paint, 0));
  document.addEventListener("tadamoo:lang", paint);
  paint();
})();
