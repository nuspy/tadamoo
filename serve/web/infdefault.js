"use strict";
/* Settings > General > Inference settings: the machine default. The same panel as the Chat's settings, bound to the inference preset "Machine Default"
   (serve/profiles.py). Every new conversation and every plan starts from it; one that was changed is not touched. */
(() => {
  const open = document.getElementById("inf-open"), host = document.getElementById("inf-host");
  if (!open || !host) return;
  const NAME = "Machine Default";

  async function save(settings) {
    try {
      const r = await fetch("api/profiles", { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() },
        body: JSON.stringify({ action: "save", kind: "inference", name: NAME, settings }) });
      if (r.ok) { document.dispatchEvent(new CustomEvent("basalt:machine")); return true; }
      return ((await r.json().catch(() => ({}))).error || {}).message || false;
    } catch (e) { return false; }
  }
  function paint(on) {
    open.setAttribute("aria-expanded", String(on));
    open.textContent = T(on ? "inf.close" : "inf.open");
  }
  open.onclick = () => {
    if (chatSettings.editing() === "machine") { chatSettings.endEdit(); return; }
    chatSettings.edit(host, { kind: "machine", label: T("inf.title"), settings: chatSettings.machineSettings(), save, end: () => paint(false) });
    paint(true);
  };
  document.addEventListener("tadamoo:view", e => { if (e.detail !== "settings" && chatSettings.editing() === "machine") chatSettings.endEdit(); });
})();
