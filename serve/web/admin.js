"use strict";
/* Settings > Admin: restarts the server from the page, the same way whatever started it would (a plain process started again, or a named service
   stopped and started through its own manager) - it asks the voice service to restart too, when one is running. A second, separate box restarts
   just the voice service, a different process started by the desktop app, without touching the server or the app. Asks once before doing
   either: there is no undo for a restart. */
(() => {
  const root = document.getElementById("adm-root");
  if (!root) return;
  let busy = false, note = "", bad = false;
  let voiceBusy = false, voiceNote = "", voiceBad = false;

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

  async function restartVoice() {
    if (voiceBusy) return;
    voiceBusy = true; voiceNote = ""; draw();
    try {
      const r = await fetch("api/admin/voice-service/restart", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", ...authHeaders() }, body: "{}" });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      voiceNote = r.ok ? T(data.restarted ? "adm.voice.restart.done" : "adm.voice.restart.none") : T("adm.e.offline");
      voiceBad = !r.ok;
    } catch (e) {
      voiceNote = T("adm.e.offline"); voiceBad = true;
    }
    voiceBusy = false; draw();
  }

  function confirm_(titleKey, helpKey, goKey, go) {
    const dlg = el("dialog", "rg-dlg");
    const goBtn = btn(T(goKey), "rg-warn", () => { dlg.close(); go(); });
    dlg.append(el("h3", "rg-h2", T(titleKey)), el("p", "rg-help", T(helpKey)),
      goBtn, btn(T("rg.cancel"), "", () => dlg.close()));
    dlg.addEventListener("close", () => dlg.remove());
    document.body.append(dlg);
    dlg.showModal();
  }
  const confirmRestart = () => confirm_("adm.restart.confirm.title", "adm.restart.confirm.help", "adm.restart.go", restart);
  const confirmVoiceRestart = () => confirm_("adm.voice.restart.confirm.title", "adm.voice.restart.confirm.help", "adm.voice.restart.go", restartVoice);

  function draw() {
    const box = el("div", "adm-box");
    box.append(el("p", "setnote", T("adm.restart.help")));
    const go = btn(T(busy ? "adm.restart.going" : "adm.restart"), "primary", confirmRestart);
    go.disabled = busy;
    box.append(go);
    if (note) { const p = el("p", "setnote", note); p.setAttribute("role", "status"); p.dataset.bad = bad ? "1" : ""; box.append(p); }

    const voiceBox = el("div", "adm-box");
    voiceBox.append(el("h3", "", T("adm.voice.title")), el("p", "setnote", T("adm.voice.restart.help")));
    const voiceGo = btn(T(voiceBusy ? "adm.voice.restart.going" : "adm.voice.restart"), "", confirmVoiceRestart);
    voiceGo.disabled = voiceBusy;
    voiceBox.append(voiceGo);
    if (voiceNote) { const p = el("p", "setnote", voiceNote); p.setAttribute("role", "status"); p.dataset.bad = voiceBad ? "1" : ""; voiceBox.append(p); }

    root.replaceChildren(box, voiceBox);
  }

  document.addEventListener("tadamoo:view", e => { if (e.detail === "settings") draw(); });
  document.addEventListener("tadamoo:lang", () => { if (document.body.dataset.view === "settings") draw(); });
  if (document.body.dataset.view === "settings") draw();
})();
