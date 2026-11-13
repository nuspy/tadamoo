"use strict";
/* The top bar says which model answers now: "Provider: model" when the Chat is on a provider (its own choice, the Primary model, or a fall-back
   because no model is loaded in the local engine). With the local engine answering, the bar keeps showing the loaded model as it always did.
   Asked again every few seconds, and at once when Providers changes something. What the server says is put in as text. */
(() => {
  const el = document.getElementById("model-now");
  if (!el) return;
  let timer = 0;
  async function ask() {
    clearTimeout(timer);
    try {
      const r = await fetch("api/llm/answering", { cache: "no-store", headers: authHeaders() });
      const d = r.ok ? await r.json() : null;
      const chat = d && d.chat;
      const onProvider = !!(chat && chat.provider), fell = !!(chat && chat.via === "fallback");
      el.hidden = !chat || !chat.label || chat.label === "—";
      if (!el.hidden) {
        el.dataset.local = onProvider ? "" : "1";                      // the loaded model is already in the bar on a wide screen; on a narrow one it is not
        el.dataset.fell = fell ? "1" : "";
        el.textContent = `${T("hd.answers")}: ${chat.label}` + (fell ? ` · ${T("hd.answers.fell")}` : "");
      }
    } catch (e) { el.hidden = true; }
    timer = setTimeout(ask, 8000);
  }
  document.addEventListener("basalt:llm-changed", ask);
  ask();
})();
