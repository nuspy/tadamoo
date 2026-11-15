"use strict";
/* Required actions: the one list of what is missing from the person (serve/actions.py): the questions of the conversations, what the plans ask, the changes of a plan to approve or refuse, the
   requests of the routines and the security authorizations. The tab lists them with their buttons and a field for text where the entry takes it; the number is on the tab. When a new entry arrives
   while the page is open (and the person is not on the list already) a dialog shows it, one at a time, with the same buttons; each entry is shown once, there are no reminders. What the plans ask
   keeps its own window (human.js), so it is not shown twice. Everything the server says is put in the page as text. */
(() => {
  const root = document.getElementById("ac-root");
  if (!root) return;
  const EVERY_MS = 8000, CHANNEL = "web";
  const lang = () => (typeof LANG === "string" && LANG === "it" ? "it" : "en");
  const h = (tag, cls, ...kids) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    kids.flat().forEach(k => { if (k != null && k !== false) e.append(k.nodeType ? k : document.createTextNode(String(k))); });
    return e;
  };
  const btn = (text, cls, fn) => { const b = h("button", "btn " + (cls || ""), text); b.type = "button"; b.addEventListener("click", fn); return b; };
  async function call(method, path, payload) {
    try {
      const r = await fetch(path, { method, cache: "no-store", headers: { ...(payload ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: payload ? JSON.stringify(payload) : undefined });
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) { return { ok: false, status: 0, data: {} }; }
  }

  /* ---- the pure parts ---- */
  const labelOf = o => (lang() === "en" ? o.label_en || o.label : o.label || o.label_en) || o.value;
  const titleOf = a => (lang() === "en" ? a.title_en || a.title : a.title) || a.id;
  /* the entries a dialog should announce: not announced on this channel yet, not already shown here, and not a plan's own question (its window shows those) */
  const toAnnounce = (list, seen) => list.filter(a => !a.notified && !seen.has(a.id) && a.source !== "plan");
  const detailRows = a => {
    const d = a.details || {}, rows = [];
    if (d.instructions) rows.push([T("ac.d.instructions"), String(d.instructions)]);
    if (d.facts && typeof d.facts === "object") Object.entries(d.facts).forEach(([k, v]) => { if (k !== "instructions" && v !== "" && v != null) rows.push([k, typeof v === "string" ? v : JSON.stringify(v)]); });
    if (d.tool) rows.push([T("ac.d.tool"), String(d.tool)]);
    if (d.arguments != null && d.arguments !== "") rows.push([T("ac.d.arguments"), typeof d.arguments === "string" ? d.arguments : JSON.stringify(d.arguments)]);
    if (d.explain) rows.push([T("ac.d.explain"), String(d.explain)]);
    if (d.effect) rows.push([T("ac.d.effect"), String(d.effect)]);
    return rows;
  };
  const api = { toAnnounce, titleOf, labelOf, detailRows };
  window.Actions = api;

  /* ---- state ---- */
  const S = { list: [], count: 0, loaded: false, seen: new Set(), queue: [], current: null, msg: "", bad: false, loading: false };
  const active = () => document.body.dataset.view === "actions";
  let dlg = null, timer = 0;

  /* ---- one entry, with its answer controls (the list and the dialog draw the same) ---- */
  function entryView(a, after) {
    const sec = h("article", "ac-item"); sec.dataset.id = a.id; sec.dataset.source = a.source;
    const lab = h("p", "ac-ctx", T("ac.src." + a.source), a.context ? " · " + a.context : "");
    const title = h("h3", "ac-q", titleOf(a));
    sec.append(lab, title);
    if (a.text) sec.append(h("p", "ac-text", a.text));
    const rows = detailRows(a);
    if (rows.length) {
      const det = h("details", "ac-cause", h("summary", "", T("ac.cause")));
      rows.forEach(([k, v]) => det.append(h("p", "ac-fact", h("b", "", k + ": "), h("span", "ac-fv", v.length > 1500 ? v.slice(0, 1500) + "…" : v))));
      if (a.source === "session" && rows.some(r => r[0] === T("ac.d.instructions"))) det.open = true;      // orders found in a text are shown before the person decides
      sec.append(det);
    }
    const err = h("p", "ac-err"); err.hidden = true; err.setAttribute("role", "alert");
    let field = null;
    if (a.free) {
      const num = a.free_hint === "number";
      field = h(num || a.free_hint === "code" ? "input" : "textarea", "ac-field");
      if (num) { field.type = "number"; field.step = "any"; field.inputMode = "decimal"; } else if (field.tagName === "INPUT") { field.type = "text"; field.maxLength = 32; field.autocomplete = "off"; } else { field.rows = 2; }
      field.setAttribute("aria-label", a.kind === "task" ? T("ac.note") : T("ac.field." + (a.free_hint || "text")));
      field.placeholder = a.kind === "task" ? T("ac.note") : a.options.length ? T("ac.or.write") : T("ac.field." + (a.free_hint || "text"));
      if (a.proposed != null && a.proposed !== "" && !a.options.length) field.value = String(a.proposed);
      sec.append(field);
    }
    const bar = h("div", "ac-buttons");
    const all = () => bar.querySelectorAll("button");
    async function submit(value, text) {
      all().forEach(b => { b.disabled = true; });
      err.hidden = true;
      const r = await call("POST", "api/actions/" + encodeURIComponent(a.id) + "/answer", { value, text });
      if (r.ok) { say(T("ac.sent")); S.list = S.list.filter(x => x.id !== a.id); paint(); if (after) after(true); refresh(); return; }
      all().forEach(b => { b.disabled = false; });
      const code = (r.data.error || {}).code;
      err.textContent = code === "gone" ? T("ac.gone") : T("ac.e.send", (r.data.error || {}).message || (r.status ? "HTTP " + r.status : T("ac.e.offline")));
      err.hidden = false;
      if (code === "gone") { S.list = S.list.filter(x => x.id !== a.id); setTimeout(() => { paint(); if (after) after(true); }, 1800); }
    }
    (a.options || []).forEach((o, i) => bar.append(btn(labelOf(o), i === 0 && a.kind !== "confirm" ? "primary ac-btn" : "ac-btn", () => submit(o.value, field ? field.value.trim() : ""))));
    if (a.free && !(a.kind === "task") && !(a.options || []).length) bar.append(btn(T("ac.send"), "primary ac-btn", () => {
      const v = field.value.trim();
      if (!v) { err.textContent = T("ac.e.empty"); err.hidden = false; field.focus(); return; }
      submit("", v);
    }));
    else if (a.free && (a.options || []).length && a.kind !== "task") bar.append(btn(T("ac.send"), "ac-btn", () => {
      const v = field.value.trim();
      if (!v) { err.textContent = T("ac.e.empty"); err.hidden = false; field.focus(); return; }
      submit("", v);
    }));
    if (a.open && a.open.session) bar.append(btn(T("ac.open.conv"), "primary ac-btn", () => { if (after) after(false); document.dispatchEvent(new CustomEvent("tadamoo:open-session", { detail: { id: a.open.session } })); }));
    if ((a.source === "plan" || a.source === "proposal") && a.details && a.details.plan_id) bar.append(btn(T("ac.open.plan"), "ac-btn ac-link", () => {
      try { sessionStorage.setItem("tadamoo.openplan", a.details.plan_id); } catch (e) { /* the tab opens anyway */ }
      if (after) after(false);
      location.hash = "plans";
    }));
    sec.append(bar, err);
    return sec;
  }

  /* ---- the tab ---- */
  const live = h("p", "ac-live"); live.setAttribute("role", "status"); live.setAttribute("aria-live", "polite");
  function say(text, bad) { S.msg = text || ""; S.bad = !!bad; live.textContent = S.msg; live.classList.toggle("bad", S.bad); }
  const badge = () => {
    const tab = document.getElementById("tab-actions"), n = S.count;
    if (!tab) return;
    let b = tab.querySelector(".ac-badge");
    if (!n) { if (b) b.remove(); tab.removeAttribute("data-count"); return; }
    if (!b) { b = h("span", "ac-badge"); tab.append(b); }
    b.textContent = String(n); b.setAttribute("aria-label", T("ac.badge", n)); tab.dataset.count = String(n);
  };
  function paint() {
    S.count = S.list.length;
    badge();
    const cnt = document.getElementById("ac-count");
    if (cnt) cnt.textContent = S.count ? T("ac.count", S.count) : "";
    if (!active()) return;
    const focused = root.contains(document.activeElement) && document.activeElement.matches("input, textarea") && document.activeElement.value;
    if (focused) return;                                                // not while the person is writing an answer
    root.replaceChildren(h("p", "help", T("ac.help")), live);
    if (!S.loaded) { root.append(h("p", "dim", T("ac.loading"))); return; }
    if (!S.list.length) { root.append(h("p", "ac-none", T("ac.none"))); return; }
    const list = h("div", "ac-list");
    S.list.forEach(a => list.append(entryView(a)));
    root.append(list);
  }

  /* ---- the dialog for a new entry: one at a time ---- */
  function ensureDialog() {
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.className = "ac-dlg"; dlg.setAttribute("aria-labelledby", "ac-dlg-h");
    dlg.addEventListener("close", () => { S.current = null; next(); });
    dlg.addEventListener("cancel", () => { /* Escape: the same as Later */ });
    document.body.append(dlg);
    return dlg;
  }
  function next() {
    if (S.current && !(dlg && dlg.open)) S.current = null;                 // a dialog that is closed shows nothing, whatever the event said
    if (S.current || !S.queue.length || typeof HTMLDialogElement === "undefined") return;
    if (active()) { S.queue = []; return; }
    const a = S.queue.shift();
    if (!S.list.some(x => x.id === a.id)) { next(); return; }                // answered meanwhile
    S.current = a;
    const d = ensureDialog();
    const closeBtn = btn(T("ac.later"), "ac-btn", () => d.close());
    const listBtn = btn(T("ac.open.list"), "ac-btn ac-link", () => { d.close(); location.hash = "actions"; });
    const head = h("h2", "ac-dlg-h", T("ac.new.title")); head.id = "ac-dlg-h";
    d.replaceChildren(h("div", "ac-dlg-body", head, entryView(a, done => { if (done) d.close(); }), h("div", "ac-dlg-foot", closeBtn, listBtn)));
    try { d.showModal(); } catch (e) { S.current = null; return; }
    const first = d.querySelector(".ac-buttons button, .ac-field");
    if (first) first.focus({ preventScroll: true });
    call("POST", "api/actions/notified", { ids: [a.id], channel: CHANNEL });
  }
  /* in the desktop app the window may be hidden in the tray or behind others: the app shows a balloon of the tray icon (channel desktop) and, when it is clicked, opens this tab */
  const app = window.chrome && window.chrome.webview;
  const inBack = () => document.hidden || !document.hasFocus();
  function announce(fresh) {
    if (!fresh.length) return;
    fresh.forEach(a => S.seen.add(a.id));
    if (app && inBack()) {
      call("POST", "api/actions/notified", { ids: fresh.map(a => a.id), channel: "desktop" });
      try { app.postMessage({ type: "actionsNew", count: S.list.length, items: fresh.slice(0, 3).map(a => ({ title: titleOf(a), context: a.context || "" })) }); } catch (e) { /* the app does not take it */ }
      return;
    }
    if (active() || document.hidden) { call("POST", "api/actions/notified", { ids: fresh.map(a => a.id), channel: CHANNEL }); return; }
    S.queue.push(...fresh);
    next();
  }

  async function refresh() {
    if (S.loading) return;
    S.loading = true;
    try {
      const r = await call("GET", "api/actions?channel=" + CHANNEL);
      if (!r.ok) return;
      S.list = r.data.actions || [];
      if (!S.loaded) {                                                  // the first look after the page opened: what is already waiting is not news, the number on the tab says it
        S.loaded = true;
        const old = S.list.filter(a => !a.notified);
        old.forEach(a => S.seen.add(a.id));
        if (old.length) call("POST", "api/actions/notified", { ids: old.map(a => a.id), channel: CHANNEL });
      } else announce(toAnnounce(S.list, S.seen));
      paint();
    } finally { S.loading = false; }
  }
  function loop() {
    clearTimeout(timer);
    timer = setTimeout(async () => { if (!document.hidden || app) await refresh(); loop(); }, EVERY_MS);       // the app's window may be hidden and still has to notice
  }
  if (app) app.addEventListener("message", e => { if (e.data && e.data.type === "openActions") location.hash = "actions"; });
  document.addEventListener("tadamoo:view", e => { if (e.detail === "actions") { paint(); refresh(); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
  document.addEventListener("tadamoo:lang", () => { badge(); paint(); });
  paint();
  setTimeout(refresh, 1200);
  loop();
})();
