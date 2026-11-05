"use strict";
/* Models > the engine choice: one card per engine at the top of the Loader (Basalt-Plus, Strata, llama.cpp, LM Studio,
   vLLM). A card opens that engine's tab; the card of the tab on screen is the one marked. One engine runs at a time. */
(() => {
  const view = document.getElementById("view-loader");
  const strip = view && view.querySelector(".subtabs");
  if (!view || !strip) return;
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const byId = id => document.getElementById(id);
  const ENGINES = ["basalt", "strata", "llamacpp", "lmstudio", "vllm"];
  let data = null, lastProvider = "";
  const cards = new Map(), status = new Map();

  const box = h("section", "engpick");
  box.id = "engpick";
  box.setAttribute("aria-label", T("ep.title"));
  const title = h("h3", "engpick-title", T("ep.title"));
  const help = h("p", "setnote engpick-help", T("ep.help"));
  const list = h("div", "engpick-list");
  list.setAttribute("role", "radiogroup");
  list.setAttribute("aria-label", T("ep.title"));
  box.append(title, help, list);
  strip.before(box);

  const pause = ms => new Promise(r => setTimeout(r, ms));

  // the Strata tab to open: a compatible one first; when none is drawn the "All engines" switch draws them
  async function openStrata() {
    const tabs = ((data && data.tabs) || []).filter(t => t.engine === "strata");
    tabs.sort((a, b) => Number(b.compatible) - Number(a.compatible));
    const all = view.querySelector(".eng-all");
    for (let i = 0; i < 12; i++) {
      for (const t of tabs) { const b = byId("sub-eng-" + t.id); if (b) { b.click(); return; } }
      if (i === 0 && all && all.getAttribute("aria-pressed") !== "true") all.click();
      await pause(250);
    }
  }

  function choose(id) {
    if (id === "basalt") byId("sub-qwen").click();
    else if (id === "llamacpp") byId("sub-llamacpp").click();
    else if (id === "strata") openStrata();
    else { lastProvider = id; byId("sub-providers").click(); }
    mark();
  }

  function current() {
    const on = el => el && el.getAttribute("aria-selected") === "true";
    if (on(byId("sub-qwen"))) return "basalt";
    if (on(byId("sub-llamacpp"))) return "llamacpp";
    if (on(byId("sub-providers"))) return lastProvider;
    if (strip.querySelector('[id^="sub-eng-"][aria-selected="true"]')) return "strata";
    return "";
  }

  function mark() {
    const now = current();
    for (const [id, b] of cards) { b.setAttribute("aria-checked", String(id === now)); b.tabIndex = id === now || (!now && id === "basalt") ? 0 : -1; }
  }

  function state(id) {
    const tabs = (data && data.tabs) || [], engines = (data && data.engines) || [];
    if (id === "basalt") { const t = tabs.find(x => x.engine === "basalt"); return t ? (t.compatible ? T("ep.s.ok") : T("ep.s.no")) : ""; }
    if (id === "strata") {
      const e = engines.find(x => x.id === "strata");
      if (!e) return "";
      return e.installed ? T("ep.s.installed") : T("ep.s.notinstalled");
    }
    if (id === "llamacpp") return "";
    return T("ep.s.connect");
  }

  function paintStates() { for (const [id, p] of status) p.textContent = state(id); }

  function build() {
    list.replaceChildren();
    cards.clear(); status.clear();
    ENGINES.forEach((id, i) => {
      const b = h("button", "btn engcard");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", "false");
      b.dataset.engine = id;
      b.append(h("b", "", T("ep." + id)), h("span", "engcard-d", T("ep.d." + id)));
      const st = h("span", "engcard-s", "");
      b.append(st);
      b.onclick = () => choose(id);
      b.onkeydown = e => {
        const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
        if (!step) return;
        e.preventDefault();
        const next = cards.get(ENGINES[(i + step + ENGINES.length) % ENGINES.length]);
        next.focus();
        choose(next.dataset.engine);
      };
      list.append(b);
      cards.set(id, b);
      status.set(id, st);
    });
    paintStates();
    mark();
  }

  async function refresh() {
    try {
      const r = await fetch("api/loader/engines?all=1", { cache: "no-store", headers: authHeaders() });
      if (r.ok) data = await r.json();
    } catch (e) { /* offline: the cards stay without a status line */ }
    paintStates();
  }

  new MutationObserver(mark).observe(strip, { subtree: true, attributes: true, attributeFilter: ["aria-selected"] });
  document.addEventListener("basalt:models-sub", mark);
  document.addEventListener("tadamoo:lang", () => {
    box.setAttribute("aria-label", T("ep.title"));
    title.textContent = T("ep.title"); help.textContent = T("ep.help");
    build();
  });
  build();
  refresh();
  setInterval(() => { if (!document.hidden) refresh(); }, 5000);
})();
