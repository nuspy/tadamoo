"use strict";
/* The app shell: interface size for very wide screens, keyboard shortcuts, the command palette (Ctrl+K), the
   "no model is loaded" notice and the index of the Settings page. Nothing here talks to the model. */
(function () {
  const store = {
    get(k, d) { try { return localStorage.getItem(k) ?? d; } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* storage blocked: the choice lasts until the page closes */ } },
  };
  const strip = s => s.normalize("NFD").replace(/[̀-ͯ]/g, "").toLowerCase();
  const reduceMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /* ---- interface size: "auto" follows the window, or a fixed percentage ---- */
  const SCALES = ["auto", "100", "125", "150", "175", "200"];
  const autoScale = w => (w >= 5000 ? 2 : w >= 3800 ? 1.75 : w >= 3000 ? 1.5 : w >= 2400 ? 1.25 : 1);
  function scaleValue() {
    const pref = store.get("basalt.scale", "auto");
    return pref === "auto" || !SCALES.includes(pref) ? autoScale(window.innerWidth) : Number(pref) / 100;
  }
  function applyScale() {
    const z = scaleValue();
    document.documentElement.style.setProperty("--z", String(z));
    document.body.style.zoom = String(z);
    const note = document.getElementById("st-ui-note");
    if (note) note.textContent = T("ui.scale.now", Math.round(z * 100), window.innerWidth);
  }
  let resizeTimer = 0;
  window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(applyScale, 120); });

  function buildScaleControl() {
    const select = document.getElementById("st-ui-scale");
    if (!select) return;
    select.replaceChildren(...SCALES.map(v => {
      const o = document.createElement("option");
      o.value = v;
      o.textContent = v === "auto" ? T("ui.scale.auto") : v + "%";
      return o;
    }));
    select.value = SCALES.includes(store.get("basalt.scale", "auto")) ? store.get("basalt.scale", "auto") : "auto";
    select.addEventListener("change", () => { store.set("basalt.scale", select.value); applyScale(); });
  }

  /* ---- views: shortcuts Alt+1..7 follow the tab order ---- */
  const tabs = () => [...document.querySelectorAll(".tabs .tab")];
  function goto(v, then) {
    view(v);
    if (then) setTimeout(then, 0);
  }
  document.addEventListener("keydown", e => {
    if (e.altKey && !e.ctrlKey && !e.metaKey && /^[1-9]$/.test(e.key)) {
      const t = tabs()[Number(e.key) - 1];
      if (t) { e.preventDefault(); view(t.dataset.view); }
    }
    if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === "k") { e.preventDefault(); openPalette(); }
  });

  /* ---- command palette ---- */
  let dialog, input, list, items = [], active = 0;
  function commands() {
    const nth = {};
    tabs().forEach((t, i) => { nth[t.dataset.view] = i + 1; });
    const go = (v, key) => ({ label: T("pal.go", T("tab." + v)), hint: key, group: T("pal.g.views"), run: () => goto(v) });
    const click = id => () => { const b = document.getElementById(id); if (b) b.click(); };
    const settingsAt = id => () => goto("settings", () => { const h = document.getElementById(id); if (h) h.scrollIntoView({ block: "start", behavior: reduceMotion() ? "auto" : "smooth" }); });
    return [
      ...tabs().map(t => go(t.dataset.view, "Alt+" + nth[t.dataset.view])),
      { label: T("pal.chat.new"), group: T("pal.g.actions"), run: () => goto("chat", click("new-chat")) },
      { label: T("pal.chat.set"), group: T("pal.g.actions"), run: () => goto("chat", click("cs-open")) },
      ...["agent", "ask", "direct"].map(m => ({ label: T("pal.chat.mode", T("ch.mode." + m)), group: T("pal.g.actions"), run: () => goto("chat", () => {
        const s = document.getElementById("chatmode"); if (s && !s.querySelector(`option[value="${m}"]`)?.disabled) { s.value = m; s.dispatchEvent(new Event("change")); }
      }) })),
      { label: T("pal.model.load"), group: T("pal.g.actions"), run: () => goto("loader", () => { const q = document.getElementById("sub-qwen"); if (q) q.click(); }) },
      { label: T("pal.agent.run"), group: T("pal.g.actions"), run: () => goto("agent", () => { const g = document.getElementById("ag-goal"); if (g && !g.disabled) g.focus(); }) },
      { label: T("pal.agent.stop"), group: T("pal.g.actions"), run: () => goto("agent", () => { const b = document.getElementById("ag-stop"); if (b && !b.hidden) b.click(); }) },
      { label: T("pal.plan.new"), group: T("pal.g.actions"), run: () => goto("plans", () => { const d = document.getElementById("pl-new"); if (d) { d.open = true; const i = document.getElementById("pl-new-title"); if (i) i.focus(); } }) },
      { label: T("pal.set.lang"), group: T("pal.g.settings"), run: settingsAt("st-lang-h") },
      { label: T("pal.set.ui"), group: T("pal.g.settings"), run: settingsAt("st-ui-h") },
      { label: T("pal.set.folders"), group: T("pal.g.settings"), run: settingsAt("st-models") },
      { label: T("pal.set.cloud"), group: T("pal.g.settings"), run: settingsAt("st-planner-h") },
      { label: T("pal.addr"), group: T("pal.g.actions"), run: async () => { try { await navigator.clipboard.writeText(location.origin + "/"); } catch (e) { /* the clipboard may be blocked */ } } },
    ];
  }
  function buildPalette() {
    dialog = document.getElementById("palette");
    input = document.getElementById("pal-input");
    list = document.getElementById("pal-list");
    if (!dialog) return;
    input.addEventListener("input", () => { active = 0; draw(); });
    input.addEventListener("keydown", e => {
      if (e.key === "ArrowDown") { e.preventDefault(); move(1); }
      else if (e.key === "ArrowUp") { e.preventDefault(); move(-1); }
      else if (e.key === "Enter") { e.preventDefault(); choose(active); }
      else if (e.key === "Home") { e.preventDefault(); active = 0; draw(); }
      else if (e.key === "End") { e.preventDefault(); active = items.length - 1; draw(); }
    });
    dialog.addEventListener("click", e => { if (e.target === dialog) dialog.close(); });
    document.getElementById("pal-open").addEventListener("click", openPalette);
  }
  function openPalette() {
    if (!dialog || dialog.open) return;
    input.value = "";
    active = 0;
    draw();
    dialog.showModal();
    input.focus();
  }
  function draw() {
    const q = strip(input.value.trim());
    items = commands().filter(c => !q || strip(c.label + " " + c.group).includes(q));
    active = Math.min(active, Math.max(items.length - 1, 0));
    list.replaceChildren();
    let group = "";
    items.forEach((c, i) => {
      if (c.group !== group) {
        group = c.group;
        const h = document.createElement("li");
        h.className = "pal-group";
        h.setAttribute("role", "presentation");
        h.textContent = group;
        list.appendChild(h);
      }
      const li = document.createElement("li");
      li.id = "pal-" + i;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      li.className = "pal-item" + (i === active ? " on" : "");
      const label = document.createElement("span");
      label.textContent = c.label;
      li.appendChild(label);
      if (c.hint) {
        const k = document.createElement("kbd");
        k.textContent = c.hint;
        li.appendChild(k);
      }
      li.addEventListener("click", () => choose(i));
      li.addEventListener("mousemove", () => { if (active !== i) { active = i; mark(); } });
      list.appendChild(li);
    });
    if (!items.length) {
      const none = document.createElement("li");
      none.className = "pal-none";
      none.setAttribute("role", "presentation");
      none.textContent = T("pal.none");
      list.appendChild(none);
    }
    input.setAttribute("aria-activedescendant", items.length ? "pal-" + active : "");
  }
  function mark() {
    list.querySelectorAll(".pal-item").forEach((li, i) => { li.classList.toggle("on", i === active); li.setAttribute("aria-selected", String(i === active)); });
    input.setAttribute("aria-activedescendant", "pal-" + active);
    const cur = document.getElementById("pal-" + active);
    if (cur) cur.scrollIntoView({ block: "nearest" });
  }
  function move(d) {
    if (!items.length) return;
    active = (active + d + items.length) % items.length;
    mark();
  }
  function choose(i) {
    const c = items[i];
    if (!c) return;
    dialog.close();
    c.run();
  }

  /* ---- "no model is loaded" ---- */
  let notice, noticeTimer = 0;
  async function checkModel() {
    if (!notice) return;
    let st = null;
    try {
      const r = await fetch("api/loader/status", { cache: "no-store", headers: authHeaders() });
      if (r.ok) st = await r.json();
    } catch (e) { /* offline: the state pill says so */ }
    const empty = st && !st.loaded && document.body.dataset.view !== "loader";
    notice.hidden = !empty;
    if (empty) {
      notice.querySelector("span").textContent = st.loading ? T("nm.loading") : T("nm.none");
      notice.querySelector("button").hidden = !!st.loading;
    }
  }

  /* ---- an index for the Settings page, built from its own sections ---- */
  function buildSettingsIndex() {
    const view = document.getElementById("view-settings");
    const nav = document.getElementById("st-index");
    if (!view || !nav) return;
    const links = [...view.querySelectorAll(":scope > section.sec")].map((sec, i) => {
      const h = sec.querySelector(".sh b");
      if (!h) return null;
      if (!h.id) h.id = "st-h-" + i;
      const a = document.createElement("button");
      a.type = "button";
      a.className = "st-link";
      a.textContent = h.textContent;
      a.addEventListener("click", () => { sec.scrollIntoView({ block: "start", behavior: reduceMotion() ? "auto" : "smooth" }); });
      return a;
    }).filter(Boolean);
    nav.replaceChildren(...links);
  }

  document.addEventListener("DOMContentLoaded", () => {
    ["nomodel", "palette", "pal-open", "view-settings"].forEach(id => { const n = document.getElementById(id); if (n) applyT(n); });
    notice = document.getElementById("nomodel");
    if (notice) notice.querySelector("button").addEventListener("click", () => { view("loader"); const q = document.getElementById("sub-qwen"); if (q) q.click(); });
    buildScaleControl();
    applyScale();
    buildPalette();
    buildSettingsIndex();
    checkModel();
    noticeTimer = setInterval(() => { if (!document.hidden) checkModel(); }, 5000);
  });
  document.addEventListener("basalt:view", e => { checkModel(); if (e.detail === "settings") buildSettingsIndex(); });
  document.addEventListener("basalt:lang", () => { buildSettingsIndex(); });
})();
