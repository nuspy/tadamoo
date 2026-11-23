"use strict";
/* Settings > Voice/Avatar's own six sub-tabs (Common, Voice, Phone, Avatar, Meetings, Chat), nested inside the outer Settings sub-tab
   "voice". Everything inside that outer sub-tab is today one of four sections (vb-section, vo-section, vph-section, tgm-section); this
   file owns only which of them is visible, by their id, and tells the page that changed with "tadamoo:voicesub" so each section's own
   script can refresh and poll itself the moment it becomes the one showing - the same reason Models > its own sub-tabs fire
   "basalt:models-sub". Nothing here talks to the server. */
(function () {
  const bar = document.getElementById("vbs-tabs");
  if (!bar) return;

  const PANELS = {
    common: { sections: ["vb-section"], panel: "vbs-panel-common" },
    voice: { sections: ["vb-section", "vo-section"], panel: "vbs-panel-voice" },
    phone: { sections: ["vph-section"], panel: null },
    avatar: { sections: ["vb-section"], panel: "vbs-panel-avatar" },
    meetings: { sections: ["vb-section"], panel: "vbs-panel-meetings" },
    chat: { sections: ["tgm-section"], panel: null },
  };
  const ALL_SECTIONS = ["vb-section", "vo-section", "vph-section", "tgm-section"];
  const ALL_PANELS = ["vbs-panel-common", "vbs-panel-voice", "vbs-panel-avatar", "vbs-panel-meetings"];
  let sub = "common";

  function apply() {
    document.body.dataset.vbsSub = sub;                   // so a panel rebuilt later (a settings save, a language change) knows its own hidden state
    const cfg = PANELS[sub] || PANELS.common;
    ALL_SECTIONS.forEach(id => { const e = document.getElementById(id); if (e) e.hidden = !cfg.sections.includes(id); });
    ALL_PANELS.forEach(id => { const e = document.getElementById(id); if (e) e.hidden = id !== cfg.panel; });
    [...bar.querySelectorAll(".subtab")].forEach(b => b.setAttribute("aria-selected", String(b.dataset.vbs === sub)));
    document.dispatchEvent(new CustomEvent("tadamoo:voicesub", { detail: sub }));
  }
  bar.querySelectorAll(".subtab").forEach(b => { b.onclick = () => { sub = b.dataset.vbs; apply(); }; });
  document.addEventListener("tadamoo:sub", e => { if (e.detail === "voice") apply(); });
  if (document.body.dataset.view === "settings") apply();
})();
