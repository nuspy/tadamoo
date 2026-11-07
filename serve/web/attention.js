"use strict";
/* The tabs of Cron Jobs and Plans flash when something new came out of them since the person last opened that tab: a run that ended, a step a plan
   made without them. The server gives the time of the latest one (/api/attention); what the person has seen is kept in this browser. */
(function (root) {
  const TABS = { cron: "tab-cron", plans: "tab-plans" };
  const EVERY_MS = 20000;
  const KEY = name => "tadamoo.seen." + name;

  /* seen: the time the person last had of this tab (null: never); t: the latest result; viewing: they are on that tab now.
     Gives the time to keep as seen and whether to flash. The first ever look finds nothing new: only what comes after it is news. */
  function decide(seen, t, viewing) {
    const latest = Number(t) || 0;
    if (latest <= 0) return { seen, flash: false };
    if (seen === null || viewing) return { seen: latest, flash: false };
    return { seen, flash: latest > seen };
  }

  const api = { decide, TABS, KEY };
  root.Attention = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined" || !document.getElementById) return;

  const live = () => document.getElementById("attn-live");
  const latest = {};
  const read = name => { try { const v = localStorage.getItem(KEY(name)); return v === null ? null : Number(v); } catch (e) { return null; } };
  const keep = (name, v) => { try { localStorage.setItem(KEY(name), String(v)); } catch (e) { /* kept for this page only */ } };
  const mine = {};
  const viewing = name => document.body.dataset.view === name;

  function paint(name, flash) {
    const tab = document.getElementById(TABS[name]);
    if (!tab) return;
    const was = tab.classList.contains("attn");
    tab.classList.toggle("attn", flash);
    if (flash && !was && live()) live().textContent = T("attn.new", tab.textContent.trim());
    if (!flash && was && live()) live().textContent = "";
  }
  function apply(name, t) {
    latest[name] = Number(t) || 0;
    const seen = name in mine ? mine[name] : read(name);
    const out = decide(seen, latest[name], viewing(name));
    if (out.seen !== seen && out.seen !== null) { mine[name] = out.seen; keep(name, out.seen); }
    paint(name, out.flash);
  }
  async function poll() {
    if (document.hidden) return;
    try {
      const r = await fetch("api/attention", { cache: "no-store", headers: authHeaders() });
      if (!r.ok) return;
      const data = await r.json();
      Object.keys(TABS).forEach(name => apply(name, (data[name] || {}).t));
    } catch (e) { /* the next look tries again */ }
  }
  document.addEventListener("tadamoo:view", e => { const name = e.detail; if (TABS[name]) apply(name, latest[name] || 0); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });
  setInterval(poll, EVERY_MS);
  setTimeout(poll, 1500);
})(typeof window !== "undefined" ? window : globalThis);
