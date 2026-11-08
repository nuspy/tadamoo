"use strict";
/* The text size and the width of the conversation, for Chat and Agent alike: four buttons in the top right corner of the header. The two views
   draw their messages with the same styles, so one pair of numbers (CSS variables on the page) serves both, and the last choice is kept for the
   next session. Nothing here needs the server. */
(() => {
  const KEY = "tadamoo.chat.size";
  const FONTS = [13, 14, 15, 16, 18, 20, 22, 24, 28];
  const WIDTHS = [600, 720, 860, 1040, 1280, 1600, 2000, 2600];
  const START = { font: FONTS.indexOf(16), width: WIDTHS.indexOf(1040) };
  const $ = id => document.getElementById(id);
  const inRange = (v, list) => Number.isInteger(v) && v >= 0 && v < list.length;

  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(KEY) || "null");
      if (saved && inRange(saved.font, FONTS) && inRange(saved.width, WIDTHS)) return { font: saved.font, width: saved.width };
    } catch (e) { /* no storage, or not ours: the defaults */ }
    return { ...START };
  }
  const now = load();

  function paint() {
    const root = document.documentElement.style;
    root.setProperty("--chat-font", FONTS[now.font] + "px");
    root.setProperty("--chat-width", WIDTHS[now.width] + "px");
    const label = T("vs.now", FONTS[now.font], WIDTHS[now.width]);
    const group = $("vs-group");
    if (group) group.title = label;
    const set = (id, off) => { const b = $(id); if (b) { b.disabled = off; b.setAttribute("aria-label", T("vs." + id.slice(3)) + ". " + label); } };
    set("vs-font-down", now.font === 0);
    set("vs-font-up", now.font === FONTS.length - 1);
    set("vs-width-down", now.width === 0);
    set("vs-width-up", now.width === WIDTHS.length - 1);
  }
  function step(kind, by) {
    const list = kind === "font" ? FONTS : WIDTHS;
    const next = Math.max(0, Math.min(list.length - 1, now[kind] + by));
    if (next === now[kind]) return;
    now[kind] = next;
    try { localStorage.setItem(KEY, JSON.stringify(now)); } catch (e) { /* this session only */ }
    paint();
  }
  const wire = (id, kind, by) => { const b = $(id); if (b) b.addEventListener("click", () => step(kind, by)); };
  wire("vs-font-down", "font", -1); wire("vs-font-up", "font", 1);
  wire("vs-width-down", "width", -1); wire("vs-width-up", "width", 1);
  paint();
})();
