"use strict";
/* The strip of pages under a tab: Monitor (Server LLM, Hardware, LOGS) and Settings (General, Inference, Models, Cloud and
   privacy, Phones, Memories, Wallet). Hardware, LOGS and Wallet are still views of their own, so the scripts that serve them keep
   working; the strip only chooses which one shows. The Settings groups are sections of one view. */
(function () {
  const GROUPS = {
    stats: [
      { id: "stats", view: "stats", label: () => T("sub.server") },
      { id: "hardware", view: "hardware", label: () => T("tab.hardware") },
      { id: "logs", view: "logs", label: () => T("tab.logs") },
      { id: "security", view: "security", label: () => T("tab.security") }],
    canary: [
      { id: "results", view: "canary", label: () => T("sub.cn.results") },
      { id: "sessions", view: "canary", label: () => T("sub.cn.sessions") },
      { id: "catalog", view: "canary", label: () => T("sub.cn.catalog") },
      { id: "settings", view: "canary", label: () => T("sub.cn.settings") },
      { id: "run", view: "canary", label: () => T("sub.cn.run") }],
    settings: [
      { id: "general", view: "settings", label: () => T("sub.general") },
      { id: "inference", view: "settings", label: () => T("sub.inference") },
      { id: "models", view: "settings", label: () => T("sub.models") },
      { id: "privacy", view: "settings", label: () => T("sub.privacy") },
      { id: "mobile", view: "settings", label: () => T("sub.mobile") },
      { id: "memories", view: "settings", label: () => T("sub.memories") },
      { id: "personas", view: "settings", label: () => T("sub.personas") },
      { id: "websearch", view: "settings", label: () => T("sub.websearch") },
      { id: "voice", view: "settings", label: () => T("sub.voice") },
      { id: "benchmarks", view: "settings", label: () => T("sub.benchmarks") },
      { id: "wallet", view: "wallet", label: () => T("tab.wallet") }],
  };
  const strip = document.getElementById("viewsub");
  const settingsView = document.getElementById("view-settings");
  let settingsSub = "general", canarySub = "results";

  const groupOf = v => TAB_OF[v] || (GROUPS[v] ? v : "");
  const activeId = (group, v) => group === "settings" && v === "settings" ? settingsSub : group === "canary" && v === "canary" ? canarySub : v;

  function showSettingsSub() {
    settingsView.querySelectorAll(":scope > section.sec").forEach(sec => { sec.hidden = (sec.dataset.sub || "general") !== settingsSub; });
  }

  function choose(item) {
    if (item.view === "settings") settingsSub = item.id;
    if (item.view === "canary") canarySub = item.id;
    if (viewOn !== item.view) view(item.view);
    else paint();
  }

  function paint() {
    const group = groupOf(viewOn), items = GROUPS[group];
    strip.hidden = !items;
    if (!items) return;
    const on = activeId(group, viewOn);
    strip.replaceChildren(...items.map(item => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "btn subtab";
      b.setAttribute("role", "tab");
      b.id = "sub-" + item.id;
      b.setAttribute("aria-controls", "view-" + item.view);
      b.setAttribute("aria-selected", String(item.id === on));
      b.tabIndex = item.id === on ? 0 : -1;
      b.textContent = item.label();
      b.addEventListener("click", () => choose(item));
      return b;
    }));
    if (viewOn === "settings") showSettingsSub();
    document.dispatchEvent(new CustomEvent("tadamoo:sub", { detail: viewOn === "settings" ? settingsSub : viewOn === "canary" ? "canary:" + canarySub : viewOn }));
  }

  // arrow keys move along the strip, Home and End go to the ends
  strip.addEventListener("keydown", e => {
    const tabs = [...strip.querySelectorAll(".subtab")], i = tabs.indexOf(document.activeElement);
    const to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: tabs.length - 1 }[e.key];
    if (i < 0 || to === undefined) return;
    e.preventDefault();
    const next = tabs[(to + tabs.length) % tabs.length];
    next.click();
    document.getElementById(next.id).focus();
  });

  document.addEventListener("tadamoo:view", paint);
  document.addEventListener("tadamoo:lang", paint);
  paint();
})();
