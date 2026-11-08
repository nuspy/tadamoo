"use strict";
/* Plans view: the workflow graph of a plan (action, sensor, decision and merge nodes), the changes the
   model proposes (shown on the graph before anything is applied), the offers for tasks the software
   cannot do, and a box to judge incoming events. Every piece of the graph is also in an ordered list
   that works by keyboard and at phone width; the list is the default on narrow screens.

   Live updates come over a fetch stream of server-sent events (EventSource cannot send the API key),
   with polling as the fallback. Everything is built with textContent: plan text is never markup. */
(function () {
  const root = document.getElementById("plans-root");
  if (!root) return;

  const SVGNS = "http://www.w3.org/2000/svg";
  const W = 156, H = 56, GX = 54, GY = 28, PAD = 16;
  const ICON = { pending: "○", in_progress: "▶", completed: "✓", failed: "✕", blocked: "⏸", skipped: "↷" };
  const TYPES = ["action", "sensor", "decision", "merge"];
  /* events that write into the plan carry the revision the page last read, so a late one cannot overwrite a newer one */
  const DIRECT = ["plan:update_node", "plan:move", "plan:mark", "plan:rewind", "plan:update_meta", "plan:fallback_response"];
  const S = {
    plans: [], id: "", snap: null, selected: "", proposal: "", mode: "", seq: 0, running: false, ready: false,
    settings: null, refreshTimer: null, abort: null, flash: {}, align: null, rewindAsk: null, rewindFocus: false,
    local: {}, drag: null, refocus: "", justDragged: false,
    tab: "define", edge: "", delArmed: "", rowFocus: "", auto: null,
    kindsKey: "",
  };
  const TABS = ["aims", "details", "events", "define"];
  const COL_KEY = "tadamoo.plans.col";
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* not kept */ } };
  const recall = (k, d) => { try { const v = localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } };

  /* ---- small helpers ---- */
  function el(tag, props, ...kids) {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, "");
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    kids.flat().forEach(c => n.append(c));
    return n;
  }
  function svg(tag, attrs, ...kids) {
    const n = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) n.setAttribute(k, v);
    kids.flat().forEach(c => n.append(c));
    return n;
  }
  const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
  const plan = () => (S.snap ? S.snap.plan : null);
  const errText = d => (d && d.error ? (typeof d.error === "string" ? d.error : d.error.message) : "") || "";
  const narrow = () => window.matchMedia("(max-width: 720px)").matches;

  async function api(path, body) {
    const opts = body === undefined
      ? { cache: "no-store", headers: authHeaders() }
      : { method: "POST", headers: { "Content-Type": "application/json", ...authHeaders() }, body: JSON.stringify(body) };
    try {
      const r = await fetch(path, opts);
      let data = {};
      try { data = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: { error: T("pl.offline") } };
    }
  }
  const planPath = tail => `api/plans/${encodeURIComponent(S.id)}${tail || ""}`;

  /* ---- status line (one live region for everything the user should hear) ---- */
  let statusEl, connEl;
  function say(msg, bad) {
    statusEl.textContent = msg;
    statusEl.classList.toggle("bad", !!bad);
  }
  function conn(state) {
    if (!connEl) return;
    connEl.dataset.s = state;
    connEl.textContent = T("pl.conn." + state);
  }

  /* ---- skeleton ---- */
  const ui = {};
  const head = text => el("div", { class: "sh" }, el("b", { text }));
  function build() {
    statusEl = el("p", { class: "pl-status", role: "status", "aria-live": "polite" });
    connEl = el("span", { class: "pl-conn", "data-s": "off" });
    ui.newForm = el("details", { class: "pl-new", id: "pl-new" },
      el("summary", { class: "btn pl-btn", title: T("pl.tip.new"), text: T("pl.new") }),
      el("form", { class: "pl-newform", onsubmit: createPlan },
        el("label", { for: "pl-new-title", text: T("pl.new.title") }),
        el("input", { id: "pl-new-title", required: true, maxlength: "120", autocomplete: "off" }),
        el("button", { class: "btn primary pl-btn", text: T("pl.create") })));
    ui.plist = el("ul", { class: "cv-list", "aria-label": T("pl.col.title") });
    ui.empty = el("p", { class: "cv-empty", text: T("pl.none") });
    ui.toggle = el("button", { type: "button", class: "cv-toggle", id: "pl-toggle", "aria-controls": "pl-col-body", onclick: () => setColumn(ui.col.classList.contains("shut")) });
    ui.col = el("aside", { class: "convs plcol", id: "pl-col", "aria-label": T("pl.col.title") },
      el("div", { class: "cv-head" }, el("b", { text: T("pl.col.title") }), ui.toggle),
      el("div", { class: "cv-body", id: "pl-col-body" }, ui.newForm, ui.plist, ui.empty));
    setColumn(recall(COL_KEY, "1") !== "0");

    ui.modeGraph = el("button", { type: "button", class: "btn pl-btn", "aria-pressed": "false", title: T("pl.tip.graph"), text: T("pl.view.graph"), onclick: () => setMode("graph") });
    ui.modeList = el("button", { type: "button", class: "btn pl-btn", "aria-pressed": "false", title: T("pl.tip.list"), text: T("pl.view.list"), onclick: () => setMode("list") });
    ui.summary = el("p", { class: "pl-summary" });
    ui.graph = el("div", { class: "pl-graph", role: "group", "aria-label": T("pl.graph.label") });
    ui.list = el("ol", { class: "pl-list", "aria-label": T("pl.list.label") });
    ui.detail = el("section", { class: "sec pl-detail", "aria-labelledby": "pl-detail-h" });
    ui.proposals = el("section", { class: "sec pl-props", "aria-labelledby": "pl-props-h" });
    ui.offers = el("section", { class: "sec pl-offers", "aria-labelledby": "pl-offers-h" });
    ui.palBody = el("div", { id: "pl-pal-body" });
    ui.palette = el("div", { class: "pl-palette", role: "group", "aria-label": T("pl.pal.title"), title: T("pl.pal.help"), hidden: true }, ui.palBody);
    ui.autoBtn = el("button", { type: "button", class: "pl-autobtn", id: "pl-auto-open", "aria-expanded": "false", "aria-controls": "pl-auto", title: T("pl.auto.tip"), "aria-label": T("pl.auto.title"),
      onclick: () => openAuto(!S.auto) }, PlanKinds.layoutIcon());
    ui.autoBox = el("div", { class: "pl-autobox", id: "pl-auto", role: "group", "aria-label": T("pl.auto.title"), hidden: true });
    ui.manage = el("section", { class: "sec pl-manage" });
    ui.history = el("section", { class: "sec pl-history", "aria-labelledby": "pl-hist-h" });
    ui.logHost = el("div", { class: "pad", id: "pl-log-root" });
    ui.chatlog = el("section", { class: "sec pl-chatlog", "aria-labelledby": "pl-log-h" }, el("div", { class: "sh" }, el("b", { id: "pl-log-h", text: T("rl.title.plan") })),
      el("p", { class: "help pad", text: T("rl.help.plan") }), ui.logHost);
    ui.align = el("section", { class: "sec pl-align", "aria-labelledby": "pl-al-h" });
    ui.horizon = el("section", { class: "sec pl-horizon" });
    ui.archived = el("section", { class: "sec pl-archived" });
    ui.triage = el("section", { class: "sec pl-triage" });
    ui.title = el("b", { id: "pl-h", text: T("pl.title") });
    ui.pick = el("p", { class: "pl-empty dim", text: T("pl.pick") });
    ui.tabs = el("div", { class: "pl-tabs", role: "tablist", "aria-label": T("pl.tabs.label") },
      TABS.map(k => el("button", { type: "button", class: "btn subtab", role: "tab", id: "pl-tab-" + k, "aria-controls": "pl-panel-" + k, title: T("pl.tab." + k + ".tip"),
        text: T("pl.tab." + k), onclick: () => setTab(k) })));
    ui.tabs.addEventListener("keydown", e => {
      const i = TABS.indexOf(S.tab), to = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
      if (to === undefined) return;
      e.preventDefault();
      setTab(TABS[(to + TABS.length) % TABS.length]);
      document.getElementById("pl-tab-" + S.tab).focus();
    });
    const panel = (k, ...kids) => (ui["panel_" + k] = el("div", { class: "pl-panel", role: "tabpanel", id: "pl-panel-" + k, "aria-labelledby": "pl-tab-" + k }, ...kids));
    ui.skillBtn = el("button", { type: "button", class: "btn pl-btn", id: "pl-skill", hidden: true, title: T("pl.skill.tip"), text: T("pl.skill"),
      onclick: () => { if (S.id && window.pskills) window.pskills.makePlan(S.id); } });
    ui.defineBar = el("div", { class: "pl-bar" },
      el("div", { class: "pl-modes", role: "group", "aria-label": T("pl.view.label") }, ui.modeGraph, ui.modeList), el("span", { class: "grow" }), ui.skillBtn);
    ui.instr = el("section", { class: "sec pl-instr", "aria-labelledby": "pl-instr-h", hidden: true },
      el("div", { class: "sh", title: T("sc.help.plan") }, el("b", { id: "pl-instr-h", text: T("sc.title.plan") })), el("div", { id: "pl-instr-root" }));
    ui.main = el("section", { class: "sec plmain", "aria-labelledby": "pl-h" },
      el("div", { class: "sh" }, ui.title, connEl),
      ui.pick, ui.tabs, statusEl,
      panel("aims", ui.horizon, ui.align),
      panel("details", ui.manage, ui.archived),
      panel("events", ui.triage),
      panel("define", ui.defineBar, ui.summary,
        el("div", { class: "pl-main" },
          el("div", { class: "pl-canvas" }, el("div", { class: "pl-stage" }, ui.graph, ui.list, ui.autoBtn, ui.autoBox), ui.detail),
          el("div", { class: "pl-side" }, ui.proposals, ui.offers, ui.chatlog, ui.history)),
        el("div", { class: "pl-dock" }, ui.palette, ui.instr)));
    root.replaceChildren(ui.col, ui.main);
    ui.runlog = RunLog.mount(ui.logHost, { id: "pl-log", emptyKey: "rl.empty.plan", labelKey: "rl.title.plan", placeholderKey: "rl.ph.plan", noReplyKey: "rl.noreply.plan", send: null, after: refreshSoon });
    SessChat.mount(document.getElementById("pl-instr-root"), {
      id: "pl-instr", kind: "plan", emptyKey: "sc.empty.plan", placeholderKey: "sc.ph.plan", labelKey: "sc.title.plan",
      target: () => (S.id && S.snap ? `${plan().title} (id ${S.id})` : ""),
      onDone: () => document.dispatchEvent(new CustomEvent("tadamoo:edited")) });
    document.addEventListener("tadamoo:edited", () => { if (S.id) refreshSoon(); });
    buildTriage();
    buildArchived();
    setMode(narrow() ? "list" : "graph", true);
    setTab(S.tab, true);
  }

  /* the column of plans at the left: open, duplicate and delete each, shut with the arrow like the Chat's */
  function setColumn(open) {
    ui.col.classList.toggle("shut", !open);
    ui.toggle.setAttribute("aria-expanded", String(open));
    ui.toggle.textContent = open ? "\u2039" : "\u203A";
    ui.toggle.title = T(open ? "cv.shut" : "cv.open");
    ui.toggle.setAttribute("aria-label", ui.toggle.title);
    keep(COL_KEY, open ? "1" : "0");
  }
  function planRow(p) {
    const li = el("li", { class: "cv" });
    if (p.plan_id === S.id) li.setAttribute("aria-current", "true");
    const flags = [p.conflicts && p.conflicts.length ? T("pl.al.n", p.conflicts.length) : "", p.review_due ? T("pl.review.due") : ""].filter(Boolean);
    const act = (cls, sym, tip, fn) => el("button", { type: "button", class: "cv-act " + cls, text: sym, title: tip, "aria-label": `${tip}: ${p.title}`, onclick: fn });
    const armed = S.delArmed === p.plan_id;
    li.append(
      el("button", { type: "button", class: "cv-open", title: T("pl.tip.open"), onclick: () => { if (p.plan_id !== S.id) choose(p.plan_id); } },
        el("span", { class: "cv-t", text: p.title }), el("span", { class: "cv-s", text: [`${p.completed}/${p.nodes}`, ...flags].join(" \u00B7 ") })),
      act("", "\u2197", T("pl.open"), () => { if (p.plan_id !== S.id) choose(p.plan_id); }),
      act("", "\u29C9", T("pl.dup"), () => duplicatePlan(p)),
      act(armed ? "sure" : "", armed ? T("pl.del.sure") : "\u2715", armed ? T("pl.del.tip") : T("pl.del"), () => deletePlan(p)));
    return li;
  }
  function paintPlans() {
    ui.plist.replaceChildren(...S.plans.map(planRow));
    ui.empty.hidden = S.plans.length > 0;
  }
  let delTimer = 0;
  async function deletePlan(p) {
    if (S.delArmed !== p.plan_id) {                                          // pressed once: it asks again, and forgets after a few seconds
      S.delArmed = p.plan_id; clearTimeout(delTimer); delTimer = setTimeout(() => { S.delArmed = ""; paintPlans(); }, 4000); paintPlans(); return;
    }
    clearTimeout(delTimer); S.delArmed = "";
    const r = await api(`api/plans/${encodeURIComponent(p.plan_id)}/delete`, {});
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); paintPlans(); return; }
    if (p.plan_id === S.id) S.id = "";
    await loadList(true);
    say(T("pl.deleted", p.title));
  }
  async function duplicatePlan(p) {
    const r = await api(`api/plans/${encodeURIComponent(p.plan_id)}/duplicate`, { title: T("pl.copyof", p.title) });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.id = r.data.plan.plan_id;
    await loadList(true);
    say(T("pl.duplicated", r.data.plan.title));
  }

  function setTab(k, quiet) {
    S.tab = k;
    TABS.forEach(t => {
      const on = t === k, b = document.getElementById("pl-tab-" + t);
      if (b) { b.setAttribute("aria-selected", String(on)); b.tabIndex = on ? 0 : -1; }
    });
    if (!quiet) render();
    if (k === "details") loadArchived();
    if (k === "events") loadTele();
  }
  function setMode(mode, quiet) {
    S.mode = mode;
    ui.modeGraph.setAttribute("aria-pressed", String(mode === "graph"));
    ui.modeList.setAttribute("aria-pressed", String(mode === "list"));
    ui.graph.hidden = mode !== "graph";
    ui.list.hidden = mode !== "list";
    if (!quiet && S.snap) render();
  }

  /* ---- loading ---- */
  async function loadList(stay) {
    const r = await api("api/plans");
    if (!r.ok) { say(errText(r.data) || T("pl.loadfail"), true); return; }
    S.plans = r.data.plans || [];
    const id = stay && S.plans.some(p => p.plan_id === S.id) ? S.id : (S.plans[0] ? S.plans[0].plan_id : "");
    S.id = id;
    paintPlans();
    await choose(id);
  }

  async function choose(id) {
    stopStream();
    S.id = id;
    S.snap = null;
    S.selected = S.proposal = S.edge = "";
    paintPlans();
    S.seq = 0;
    if (!id) { render(); return; }
    await refresh();
    S.seq = S.snap ? S.snap.seq : 0;
    startStream();
  }

  async function refresh() {
    if (!S.id) return;
    const r = await api(planPath());
    if (!r.ok) { say(errText(r.data) || T("pl.loadfail"), true); return; }
    S.snap = r.data;
    const al = await api(planPath("/alignment"));
    S.align = al.ok ? al.data : null;
    if (S.selected && !r.data.plan.nodes[S.selected]) S.selected = "";
    if (S.proposal && !r.data.proposals.some(p => p.proposal_id === S.proposal)) S.proposal = "";
    if (!S.proposal && r.data.proposals.length) S.proposal = r.data.proposals[0].proposal_id;
    render();
  }
  function refreshSoon() {
    clearTimeout(S.refreshTimer);
    S.refreshTimer = setTimeout(refresh, 150);
  }

  /* ---- the event stream (fetch + reader, polling when it fails) ---- */
  function stopStream() {
    S.running = false;
    if (S.abort) S.abort.abort();
    S.abort = null;
    conn("off");
  }
  function startStream() {
    if (!S.id || S.running) return;
    S.running = true;
    const planId = S.id;
    (async () => {
      let wait = 1000;
      while (S.running && S.id === planId) {
        try {
          S.abort = new AbortController();
          const res = await fetch(planPath(`/stream?after=${S.seq}`), { headers: authHeaders(), signal: S.abort.signal });
          if (!res.ok || !res.body) throw new Error(String(res.status));
          conn("live");
          wait = 1000;
          const reader = res.body.getReader();
          const dec = new TextDecoder();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let i;
            while ((i = buf.indexOf("\n\n")) >= 0) {
              frame(buf.slice(0, i));
              buf = buf.slice(i + 2);
            }
          }
        } catch (e) {
          if (!S.running) return;
        }
        if (!S.running || S.id !== planId) return;
        conn("retry");
        await refresh();                                   // polling while the stream is down
        await new Promise(r => setTimeout(r, wait));
        wait = Math.min(wait * 2, 15000);
      }
    })();
  }
  function frame(text) {
    const line = text.split("\n").find(l => l.startsWith("data: "));
    if (!line) return;
    let ev;
    try { ev = JSON.parse(line.slice(6)); } catch (e) { return; }
    S.seq = Math.max(S.seq, ev.seq || 0);
    onEvent(ev);
  }
  function onEvent(ev) {
    if (ev.type === "plan:diff_proposed") {
      const ch = ev.proposal.changes;
      [...ch.added, ...ch.updated, ...ch.removed].forEach(id => { S.flash[id] = Date.now() + 3000; });
      say(T("pl.ev.proposed", ev.proposal.title));
    } else if (ev.type === "plan:replan_ready") {
      const a = ev.applied || {};
      say(a.applied ? T(a.remaining ? "pl.ev.autoparked" : "pl.ev.autoapplied") : T("pl.ev.replan", ev.proposals.length));
    } else if (ev.type === "plan:replan_failed") {
      say(T("pl.ev.replanfail", ev.error), true);
    } else if (ev.type === "plan:fallback_offered") {
      say(T("pl.ev.offer", ev.offer.node_id));
    } else if (ev.type === "plan:triage") {
      showTriage(ev.result);
      if (ui.triage.open) loadTele();
    } else if (ev.type === "plan:diff_applied") {
      say(T("pl.ev.applied"));
    } else if (ev.type === "plan:archived") {
      if (ev.plan_id !== S.id) return;                       // this page archived it itself and already moved on
      say(T("pl.ev.archived", T("pl.actor." + ev.actor)));
      S.id = "";
      loadList(false);
      if (ui.archived.open) loadArchived();
      return;
    }
    refreshSoon();
  }

  /* ---- layout and drawing ---- */
  function layoutOf(snap) {
    const pos = {}, ghosts = [];
    const layerOf = {};
    snap.layers.forEach((ids, i) => ids.forEach(id => { layerOf[id] = i; }));
    const rows = snap.layers.map(ids => ids.length);
    snap.layers.forEach((ids, i) => ids.forEach((id, j) => { pos[id] = { layer: i, row: j }; }));
    const prop = snap.proposals.find(p => p.proposal_id === S.proposal);
    if (prop) {
      (prop.diff.add_nodes || []).forEach(n => {
        const known = (n.dependencies || []).filter(d => d in layerOf).map(d => layerOf[d]);
        const after = (n.outputs || []).filter(o => o in layerOf).map(o => layerOf[o]);
        const layer = known.length ? Math.max(...known) + 1 : after.length ? Math.max(0, Math.min(...after) - 1) : 0;
        while (rows.length <= layer) rows.push(0);
        pos[n.id] = { layer, row: rows[layer]++ };
        ghosts.push(n);
      });
    }
    const maxRows = Math.max(1, ...rows);
    const cols = Math.max(1, rows.length);
    Object.values(pos).forEach(p => {
      p.x = PAD + p.layer * (W + GX);
      p.y = PAD + p.row * (H + GY) + ((maxRows - rows[p.layer]) * (H + GY)) / 2;
    });
    let width = PAD * 2 + cols * W + (cols - 1) * GX, height = PAD * 2 + maxRows * H + (maxRows - 1) * GY;
    Object.entries(pos).forEach(([id, p]) => {
      const own = placeOf(id);
      if (own) { p.x = own.x; p.y = own.y; }
      width = Math.max(width, p.x + W + PAD);
      height = Math.max(height, p.y + H + PAD);
    });
    return { pos, ghosts, width, height };
  }

  /* where a step is drawn when it has a place of its own: being dragged, moved and not yet saved, or saved in the step */
  function placeOf(id) {
    if (S.drag && S.drag.id === id) return S.drag;
    if (S.auto && S.auto.at && S.auto.at[id]) return S.auto.at[id];
    if (S.local[id]) return S.local[id];
    const n = S.snap && S.snap.plan.nodes[id];
    return n && n.position ? n.position : null;
  }

  function hlClass(id) {
    const prop = S.snap.proposals.find(p => p.proposal_id === S.proposal);
    let c = "";
    if (prop) {
      const ch = prop.changes;
      c = ch.added.includes(id) ? "hl-added" : ch.removed.includes(id) ? "hl-removed" : ch.updated.includes(id) ? "hl-updated" : "";
    }
    if (S.flash[id] && S.flash[id] > Date.now()) c += " flash";
    return c;
  }

  function shape(type, x, y) {
    if (type === "decision") return svg("polygon", { points: `${x + W / 2},${y} ${x + W},${y + H / 2} ${x + W / 2},${y + H} ${x},${y + H / 2}` });
    if (type === "merge") return svg("polygon", { points: `${x + 14},${y} ${x + W - 14},${y} ${x + W},${y + H / 2} ${x + W - 14},${y + H} ${x + 14},${y + H} ${x},${y + H / 2}` });
    return svg("rect", { x, y, width: W, height: H, rx: type === "sensor" || type.startsWith("wait_") ? H / 2 : 8 });
  }

  function drawGraph() {
    const lay = layoutOf(S.snap);
    S.lastLayout = lay;
    const nodes = S.snap.plan.nodes;
    const root_ = svg("svg", { class: "pl-svg", viewBox: `0 0 ${lay.width} ${lay.height}`, role: "img", "aria-label": T("pl.graph.label") },
      svg("defs", {}, svg("marker", { id: "pl-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        svg("path", { d: "M0,0 L10,5 L0,10 z", class: "pl-arrowhead" }))));
    const edge = (a, b, cls) => {
      const p = lay.pos[a], q = lay.pos[b];
      if (!p || !q) return;
      const x1 = p.x + W, y1 = p.y + H / 2, x2 = q.x, y2 = q.y + H / 2, dx = Math.max(24, (x2 - x1) / 2);
      const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, key = a + ">" + b, sel = S.edge === key;
      const g = svg("g", { class: "pl-edgeg" + (sel ? " sel" : ""), "data-edge": key },
        svg("path", { d, class: "pl-edge " + cls + (sel ? " sel" : ""), "marker-end": "url(#pl-arrow)" }));
      if (!cls.includes("ghost") && !cls.includes("hl-removed")) {
        const names = [(nodes[a] || {}).title || a, (nodes[b] || {}).title || b];
        const hit = svg("path", { d, class: "pl-edgehit", tabindex: "0", role: "button", "aria-pressed": String(sel), "aria-label": T("pl.link.label", ...names) },
          svg("title", {}, document.createTextNode(T("pl.link.tip"))));
        hit.addEventListener("click", () => pickEdge(key));
        hit.addEventListener("keydown", e => {
          if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); removeLink(a, b); }
          else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); pickEdge(key); }
        });
        g.append(hit);
        if (sel) {
          const mark = svg("g", { class: "pl-x", tabindex: "0", role: "button", "aria-label": T("pl.link.remove"), transform: `translate(${(x1 + x2) / 2},${(y1 + y2) / 2})` },
            svg("title", {}, document.createTextNode(T("pl.link.remove"))), svg("circle", { r: "11" }), svg("text", { y: "5", "text-anchor": "middle" }, document.createTextNode("\u00D7")));
          mark.addEventListener("click", () => removeLink(a, b));
          mark.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); removeLink(a, b); } });
          g.append(mark);
        }
      }
      root_.append(g);
    };
    Object.values(nodes).forEach(n => n.outputs.forEach(c => {
      const dead = nodes[n.id].status === "skipped" || nodes[c].status === "skipped";
      const prop = S.snap.proposals.find(p => p.proposal_id === S.proposal);
      const gone = prop && (prop.diff.remove_edges || []).some(e => e[0] === n.id && e[1] === c);
      edge(n.id, c, (dead ? "dead " : "") + (gone ? "hl-removed" : ""));
    }));
    lay.ghosts.forEach(g => {
      (g.dependencies || []).forEach(d => edge(d, g.id, "ghost"));
      (g.outputs || []).forEach(o => edge(g.id, o, "ghost"));
    });
    const prop = S.snap.proposals.find(p => p.proposal_id === S.proposal);
    (prop && prop.diff.add_edges || []).forEach(e => edge(e[0], e[1], "ghost"));

    const drawNode = (n, ghost) => {
      const p = lay.pos[n.id];
      if (!p) return;
      const status = ghost ? "pending" : n.status;
      const g = svg("g", {
        class: `pl-node t-${n.type} st-${status} ${ghost ? "ghost hl-added" : hlClass(n.id)} ${S.selected === n.id ? "sel" : ""}${S.drag && S.drag.id === n.id ? " dragging" : ""}${n.state_variables && n.state_variables.draft ? " draft" : ""}`,
        "data-id": n.id, tabindex: "0", role: "button", "aria-pressed": String(S.selected === n.id),
        "aria-label": `${n.title}. ${T("pl.type." + n.type)}, ${T("pl.status." + status)}, ${T("pl.assignee." + n.assignee)}`,
      }, svg("title", {}, document.createTextNode(`${n.title}\n${n.description || ""}`)), shape(n.type, p.x, p.y));
      const label = svg("text", { x: p.x + W / 2, y: p.y + H / 2 - 4, class: "pl-t", "text-anchor": "middle" });
      label.textContent = clip(n.title, 20);
      const sub = svg("text", { x: p.x + W / 2, y: p.y + H / 2 + 14, class: "pl-s", "text-anchor": "middle" });
      const words = PlanKinds.brief(n.type, n.spec);
      sub.textContent = n.state_variables && n.state_variables.draft ? T("pl.draft") : `${ICON[status]} ${T("pl.status." + status)} · ${words ? clip(words, 16) : T("pl.assignee.short." + n.assignee)}`;
      g.append(label, sub, PlanKinds.iconGroup(n.type, p.x + 6, p.y + 3, 0.45));
      g.addEventListener("click", () => { if (S.justDragged) { S.justDragged = false; return; } select(n.id); });
      g.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " ") { e.preventDefault(); S.refocus = n.id; select(n.id); }
        else if (!ghost && S.selected === n.id && ARROWS[e.key]) {
          e.preventDefault();
          const [dx, dy] = ARROWS[e.key], step = e.shiftKey ? STEP * 4 : STEP;
          moveBy(n.id, dx * step, dy * step, true);
        }
      });
      if (!ghost) {
        g.addEventListener("pointerdown", e => startDrag(e, n.id, lay));
        for (const side of ["in", "out"]) {                              // the ends a link is drawn from: out to the step that waits, in from the step it waits for
          const port = svg("circle", { class: "pl-port " + side, cx: side === "out" ? p.x + W : p.x, cy: p.y + H / 2, r: "6" },
            svg("title", {}, document.createTextNode(T(side === "out" ? "pl.port.out" : "pl.port.in"))));
          port.addEventListener("pointerdown", e => startLink(e, n.id, side));
          g.append(port);
        }
      }
      if (ghost) g.setAttribute("aria-disabled", "true");
      root_.append(g);
    };
    Object.values(nodes).forEach(n => drawNode(n, false));
    lay.ghosts.forEach(n => drawNode({ ...n, status: "pending", description: n.description || "" }, true));
    ui.graph.replaceChildren(root_);
    S.svg = root_;
    if (S.refocus) {
      const g = root_.querySelector(`[data-id="${S.refocus}"]`);
      if (g) g.focus();
      S.refocus = "";
    }
  }

  /* ---- links: drawn from a port to another step, picked on the graph and removed ---- */
  function pickEdge(key) {
    S.edge = S.edge === key ? "" : key;
    S.selected = "";
    if (S.edge) { const [a, b] = key.split(">"), nodes = S.snap.plan.nodes; say(T("pl.link.picked", (nodes[a] || {}).title || a, (nodes[b] || {}).title || b)); }
    render();
  }
  async function removeLink(a, b) {
    S.edge = "";
    await userDiff({ remove_edges: [[a, b]] }, T("pl.link.removed"));
  }
  async function addLink(a, b) {
    const nodes = S.snap.plan.nodes;
    if ((nodes[a] || { outputs: [] }).outputs.includes(b)) { say(T("pl.link.exists"), true); return; }
    await userDiff({ add_edges: [[a, b]] }, T("pl.link.added"));
    S.edge = a + ">" + b;
    render();
  }
  /* a link follows the pointer from the port until it is let go over a step; anywhere else it is dropped */
  function startLink(e, id, side) {
    if (e.button !== undefined && e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    const svgEl = S.svg, ctm = svgEl && svgEl.getScreenCTM();
    if (!ctm) return;
    const toSvg = ev => { const pt = svgEl.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY; return pt.matrixTransform(ctm.inverse()); };
    const at = shownAt(id), from = { x: side === "out" ? at.x + W : at.x, y: at.y + H / 2 };
    const line = svg("path", { class: "pl-edge ghost pl-linking" });
    svgEl.append(line);
    let over = "";
    const stepAt = ev => {
      const hit = document.elementFromPoint(ev.clientX, ev.clientY), g = hit && hit.closest ? hit.closest(".pl-node:not(.ghost)") : null;
      const target = g ? g.getAttribute("data-id") : "";
      return target && target !== id ? target : "";
    };
    const curve = (a, b) => { const dx = Math.max(24, Math.abs(b.x - a.x) / 2); return `M${a.x},${a.y} C${a.x + dx},${a.y} ${b.x - dx},${b.y} ${b.x},${b.y}`; };
    const mark = target => {
      if (target === over) return;
      const old = over && svgEl.querySelector(`.pl-node[data-id="${over}"]`), now = target && svgEl.querySelector(`.pl-node[data-id="${target}"]`);
      if (old) old.classList.remove("linktarget");
      if (now) now.classList.add("linktarget");
      over = target;
    };
    const move = ev => { const q = toSvg(ev); line.setAttribute("d", side === "out" ? curve(from, q) : curve(q, from)); mark(stepAt(ev)); };
    const done = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", cancel);
      window.removeEventListener("keydown", esc, true);
      mark("");
      line.remove();
    };
    const up = ev => {
      const target = stepAt(ev);
      done();
      S.justDragged = true;                                            // the click that follows must not select a step
      setTimeout(() => { S.justDragged = false; }, 0);
      if (target) addLink(side === "out" ? id : target, side === "out" ? target : id);
    };
    const cancel = () => done();
    const esc = ev => { if (ev.key === "Escape") { ev.preventDefault(); done(); } };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", cancel);
    window.addEventListener("keydown", esc, true);
  }

  /* ---- moving a step: with the pointer, with the arrow keys of a selected step, or with the buttons of its panel ---- */
  const STEP = 16, LIMIT = 4000;
  const ARROWS = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
  const clamp = v => Math.max(0, Math.min(LIMIT, Math.round(v)));
  let saveTimers = {};

  function shownAt(id) {
    const p = placeOf(id);
    if (p) return { x: p.x, y: p.y };
    const l = S.lastLayout && S.lastLayout.pos[id];
    return l ? { x: l.x, y: l.y } : { x: PAD, y: PAD };
  }
  async function savePosition(id, pos) {
    const r = await api(planPath("/events"), { type: "plan:update_node", node_id: id, changes: { position: pos } });
    delete S.local[id];
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); }
    await refresh();
  }
  /* by one step; `later`: a run of key presses is saved once, a moment after the last */
  function moveBy(id, dx, dy, later) {
    const at = shownAt(id), to = { x: clamp(at.x + dx), y: clamp(at.y + dy) };
    S.local[id] = to;
    const n = S.snap.plan.nodes[id];
    say(T("pl.moved", n ? n.title : id, to.x, to.y));
    if (later) {
      S.refocus = id;
      render();
      clearTimeout(saveTimers[id]);
      saveTimers[id] = setTimeout(() => savePosition(id, to), 400);
    } else {
      render();
      savePosition(id, to);
    }
  }
  function startDrag(e, id, lay) {
    if (e.button !== undefined && e.button !== 0) return;
    const svgEl = S.svg, ctm = svgEl && svgEl.getScreenCTM();
    if (!ctm) return;
    const toSvg = ev => { const pt = svgEl.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY; return pt.matrixTransform(ctm.inverse()); };
    S.lastLayout = lay;
    const start = toSvg(e), at = shownAt(id);
    let moved = false, frame = 0;
    const move = ev => {
      const q = toSvg(ev), dx = q.x - start.x, dy = q.y - start.y;
      if (!moved && Math.hypot(dx, dy) < 5) return;                   // a click is not a move
      moved = true;
      S.drag = { id, x: clamp(at.x + dx), y: clamp(at.y + dy) };
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; drawGraph(); });
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      window.removeEventListener("pointercancel", up);
      if (frame) cancelAnimationFrame(frame);
      if (!moved) return;
      const to = { x: S.drag.x, y: S.drag.y };
      S.drag = null;
      S.justDragged = true;                                          // the click that follows a drag must not select
      setTimeout(() => { S.justDragged = false; }, 0);
      S.local[id] = to;
      S.selected = id;
      render();
      savePosition(id, to);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    window.addEventListener("pointercancel", up);
  }


  /* ---- the layout of the whole plan: an icon at the corner of the graph opens the kinds of layout; choosing one shows it, Apply keeps it, a click outside
     (or Escape) puts everything back ---- */
  const AUTO = PlanKinds.LAYOUTS;
  const autoPlace = kind => PlanKinds.arrange(kind, S.snap, { W, H, GX, GY, PAD, clamp });
  function openAuto(open) {
    S.auto = open ? { kind: "", at: null } : null;
    ui.autoBox.hidden = !open;
    ui.autoBtn.setAttribute("aria-expanded", String(!!open));
    if (open) drawAutoBox();
    drawGraph();
  }
  function drawAutoBox() {
    ui.autoBox.replaceChildren(
      el("b", { class: "pl-autoh", text: T("pl.auto.title") }),
      ...AUTO.map(k => el("button", { type: "button", class: "btn pl-autoopt" + (S.auto && S.auto.kind === k ? " sel" : ""), "aria-pressed": String(!!S.auto && S.auto.kind === k),
        title: T("pl.auto." + k + ".d"), onclick: () => previewAuto(k) },
        el("span", { class: "pl-autoname", text: T("pl.auto." + k) }), el("span", { class: "pl-autod", text: T("pl.auto." + k + ".d") }))),
      el("div", { class: "pl-autoacts" },
        el("button", { type: "button", class: "btn primary pl-btn", id: "pl-auto-apply", disabled: !(S.auto && S.auto.kind), title: T("pl.auto.apply.tip"), text: T("pl.auto.apply"), onclick: applyAuto }),
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.auto.cancel.tip"), text: T("pl.auto.cancel"), onclick: () => openAuto(false) })));
  }
  function previewAuto(kind) {
    S.auto = { kind, at: autoPlace(kind) };
    drawAutoBox();
    drawGraph();
  }
  async function applyAuto() {
    if (!S.auto || !S.auto.at) return;
    const update = {};
    Object.entries(S.auto.at).forEach(([id, pos]) => { update[id] = { position: pos }; });
    const kind = S.auto.kind;
    openAuto(false);
    S.local = {};
    await userDiff({ update_nodes: update }, T("pl.auto." + kind));
  }
  document.addEventListener("pointerdown", e => {
    if (S.auto && ui.autoBox && !ui.autoBox.contains(e.target) && !ui.autoBtn.contains(e.target)) openAuto(false);
  }, true);
  document.addEventListener("keydown", e => { if (e.key === "Escape" && S.auto) { openAuto(false); ui.autoBtn.focus(); } });

  function drawList() {
    const order = S.snap.order || S.snap.layers.flat();
    const nodes = S.snap.plan.nodes;
    ui.list.replaceChildren(...order.map((id, i) => {
      const n = nodes[id];
      const step = (dir, sym, tip, off) => el("button", { type: "button", class: "btn pl-rowbtn", text: sym, title: tip, "aria-label": `${tip}: ${n.title}`, disabled: off,
        "data-row": `${id}:${dir}`, onclick: () => moveStep(id, dir) });
      return el("li", { class: `pl-li st-${n.status} ${hlClass(id)}` },
        el("button", { type: "button", class: "pl-libtn" + (S.selected === id ? " sel" : ""), "aria-pressed": String(S.selected === id), onclick: () => select(id) },
          el("span", { class: "pl-li-title", text: n.title }),
          el("span", { class: "pl-li-meta", text: `${n.state_variables.draft ? T("pl.draft") + " \u00B7 " : ""}${ICON[n.status]} ${T("pl.status." + n.status)} \u00B7 ${T("pl.type." + n.type)} \u00B7 ${T("pl.assignee." + n.assignee)}` })),
        el("div", { class: "pl-rowmove", role: "group", "aria-label": T("pl.row.group") }, step(-1, "\u2191", T("pl.row.up"), i === 0), step(1, "\u2193", T("pl.row.down"), i === order.length - 1)));
    }));
    if (S.rowFocus) {
      const [id, dir] = S.rowFocus.split(":"), want = ui.list.querySelector(`[data-row="${id}:${dir}"]`), other = ui.list.querySelector(`[data-row="${id}:${-dir}"]`);
      const b = want && !want.disabled ? want : other;
      if (b && !b.disabled) b.focus();
      S.rowFocus = "";
    }
  }
  async function moveStep(id, dir) {
    S.rowFocus = `${id}:${dir}`;
    const n = S.snap.plan.nodes[id];
    await event({ type: "plan:move", node_id: id, direction: dir }, T("pl.row.moved", n ? n.title : id));
  }

  function select(id) {
    if (S.rewindAsk && S.rewindAsk.id !== id) S.rewindAsk = null;
    S.selected = id;
    S.edge = "";
    render();
    const h = document.getElementById("pl-detail-h");
    if (h && narrow()) h.scrollIntoView({ block: "nearest" });
  }

  /* ---- detail of one node ---- */
  function drawDetail() {
    const n = S.selected && S.snap.plan.nodes[S.selected];
    ui.detail.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-detail-h", text: T("pl.detail") })));
    if (!n && S.edge) {
      const [a, b] = S.edge.split(">"), nodes = S.snap.plan.nodes;
      ui.detail.append(el("div", { class: "pad" }, el("p", { text: T("pl.link.picked", (nodes[a] || {}).title || a, (nodes[b] || {}).title || b) }),
        el("button", { type: "button", class: "btn pl-btn pl-danger", title: T("pl.link.tip"), text: T("pl.link.remove"), onclick: () => removeLink(a, b) })));
      return;
    }
    if (!n) { ui.detail.append(el("p", { class: "pad dim", text: T("pl.detail.none") })); return; }
    const done = n.status === "completed";
    const title = el("input", { id: "pl-d-title", value: n.title, maxlength: "200", disabled: done });
    const desc = el("textarea", { id: "pl-d-desc", rows: "3", disabled: done, text: n.description });
    const assignee = el("select", { id: "pl-d-assignee", disabled: done },
      ["agent", "user", "third_party"].map(a => el("option", { value: a, text: T("pl.assignee." + a), selected: a === n.assignee })));
    const cond = n.type === "decision" || n.type === "sensor"
      ? el("textarea", { id: "pl-d-cond", rows: "4", disabled: done, spellcheck: "false", text: JSON.stringify(n.conditions, null, 2) })
      : null;
    const produces = el("input", { id: "pl-d-produces", value: (n.produces || []).map(p => p.type === "any" ? p.name : `${p.name}:${p.type}`).join(", "), disabled: done, autocomplete: "off", spellcheck: "false", placeholder: "uova:number, presenti:number" });
    const policy = n.retry_policy || {};
    const tries = el("input", { id: "pl-d-tries", type: "number", min: "1", max: "10", inputmode: "numeric", disabled: done, value: String(policy.max_attempts || 3) });
    const wait = el("input", { id: "pl-d-wait", type: "number", min: "0", max: "604800", inputmode: "numeric", disabled: done, value: String(policy.wait_seconds ?? 30) });
    const out = el("textarea", { id: "pl-d-out", rows: "2", spellcheck: "false", placeholder: '{"invitati_confermati": ["Anna"]}' });
    const facts = [];
    if (n.state_variables.blocked_by) facts.push(T("pl.blocked_by", n.state_variables.blocked_by.map(i => (S.snap.plan.nodes[i] || {}).title || i).join(", ")));
    if (n.state_variables.parked_by) facts.push(T("pl.parked_by", n.state_variables.parked_by.map(i => (S.snap.plan.nodes[i] || {}).title || i).join(", ")));
    if (n.state_variables.timed_out) facts.push(T("pl.timed_out"));
    if (n.state_variables.fallback_reason) facts.push(T("pl.fallback", n.state_variables.fallback_reason));
    if (n.state_variables.selected) facts.push(T("pl.selected", n.state_variables.selected.join(", ")));
    if (n.state_variables.outputs) facts.push(T("pl.outputs", JSON.stringify(n.state_variables.outputs)));
    if (n.state_variables.retry_at) facts.push(T("pl.fact.retry", n.state_variables.attempts || 1, new Date(n.state_variables.retry_at * 1000).toLocaleTimeString()));
    if (n.state_variables.matched) facts.push(T("pl.fact.matched", n.state_variables.matched.text));
    if (n.state_variables.sensor_error) facts.push(T("pl.fact.sensorerr", n.state_variables.sensor_error));
    if (n.state_variables.no_route) facts.push(T("pl.fact.noroute", noRouteText(n.state_variables.no_route)));
    if (n.call_request) facts.push(T("pl.fact.call", n.call_request.number, n.call_request.reason));
    if (n.wiki_query) facts.push(T("pl.fact.wiki", n.wiki_query));
    const sv = n.state_variables;
    if (sv.wake_at && n.status === "pending") facts.push(T("pl.fact.wake", new Date(sv.wake_at * 1000).toLocaleString()));
    if (sv.judged) facts.push(T("pl.fact.judged", T("pl.judged." + sv.judged.answer), new Date(sv.judged.at * 1000).toLocaleTimeString()));
    if (sv.auto_error) facts.push(T("pl.fact.autoerr", sv.auto_error));
    if (sv.runs) facts.push(T("pl.fact.runs", sv.runs));
    const kindFields = PlanKinds.fieldsOf(kindList(), n.type);
    const specForm = kindFields.length ? PlanKinds.specForm(kindFields, n.spec, "pl-d-sp", T, done) : null;
    const isReady = S.snap.ready.includes(n.id);
    if (S.rewindAsk && S.rewindAsk.id !== n.id) S.rewindAsk = null;
    ui.detail.append(el("form", { class: "pad pl-dform", onsubmit: e => { e.preventDefault(); saveNode(n.id, cond, specForm); } },
      S.rewindAsk ? rewindBox(S.rewindAsk) : [],
      el("p", { class: "pl-dmeta", text: `${T("pl.type." + n.type)} · ${ICON[n.status]} ${T("pl.status." + n.status)}${isReady ? " · " + T("pl.ready") : ""}` }),
      n.state_variables.draft ? el("p", { class: "pl-fact", text: T("pl.draft.help") }) : [],
      ...facts.map(f => el("p", { class: "pl-fact", text: f })),
      el("label", { for: "pl-d-title", text: T("pl.f.title") }), title,
      el("label", { for: "pl-d-desc", text: T("pl.f.desc") }), desc,
      el("label", { for: "pl-d-assignee", text: T("pl.f.assignee") }), assignee,
      n.type === "action" ? [el("label", { for: "pl-d-produces", text: T("pl.f.produces") }), produces, el("p", { class: "help", text: T("pl.f.produces.help") })] : [],
      specForm ? el("fieldset", { class: "pl-parents pl-spec" }, el("legend", { text: T("pl.type." + n.type) }), el("p", { class: "help", text: T("pl.kind." + n.type + ".d") }), specForm.el) : [],
      cond ? [el("label", { for: "pl-d-cond", text: T("pl.f.cond") }), cond, el("p", { class: "help", text: T(n.type === "sensor" ? "pl.f.cond.sensor.help" : "pl.f.cond.help") })] : [],
      el("label", { for: "pl-d-tries", text: T("pl.f.tries") }), tries,
      el("label", { for: "pl-d-wait", text: T("pl.f.wait") }), wait,
      el("p", { class: "help", text: T("pl.f.retry.help") }),
      done ? el("p", { class: "help", text: T("pl.completed.help") }) : el("button", { class: "btn primary pl-btn", title: T("pl.tip.save"), text: T("pl.save") }),
      el("label", { for: "pl-d-out", text: T("pl.f.out") }), out,
      el("div", { class: "pl-acts", role: "group", "aria-label": T("pl.progress") },
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.start"), text: T("pl.start.btn"), onclick: () => mark(n.id, "in_progress") }),
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.done"), text: T("pl.done"), onclick: () => mark(n.id, "completed", out) }),
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.fail"), text: T("pl.fail"), onclick: () => mark(n.id, "failed") }),
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.rewind"), text: T("pl.rewind"), onclick: () => rewind(n.id) }),
        removeButton(n))));
  }

  function removeButton(n) {
    const b = el("button", { type: "button", class: "btn pl-btn pl-danger", title: T("pl.tip.remove"), text: T("pl.remove") });
    let armed = false, t = 0;
    b.addEventListener("click", () => {
      if (!armed) { armed = true; b.textContent = T("pl.remove.sure"); t = setTimeout(() => { armed = false; b.textContent = T("pl.remove"); }, 4000); return; }
      clearTimeout(t);
      userDiff({ remove_nodes: [n.id] }, T("pl.remove"));
    });
    return b;
  }

  async function event(ev, okMsg) {
    const r = await api(planPath("/events"), S.snap && DIRECT.includes(ev.type) ? { ...ev, base_rev: S.snap.rev } : ev);
    if (!r.ok) {
      if (r.data && r.data.stale) { say(T("pl.stale"), true); await refresh(); return r; }
      say(errText(r.data) || (r.data && r.data.error) || T("pl.failed"), true);
      return r;
    }
    if (okMsg) say(okMsg);
    await refresh();
    return r;
  }
  async function saveNode(id, cond, specForm) {
    const changes = { title: document.getElementById("pl-d-title").value.trim(), description: document.getElementById("pl-d-desc").value,
      assignee: document.getElementById("pl-d-assignee").value };
    if (cond) {
      try { changes.conditions = JSON.parse(cond.value || "{}"); } catch (e) { say(T("pl.badjson"), true); return; }
    }
    if (!changes.title) { say(T("pl.f.title.required"), true); return; }
    const was = S.snap.plan.nodes[id], isDraft = !!(was && was.state_variables && was.state_variables.draft);
    let missing = [];
    if (specForm) {
      const got = specForm.collect();
      if (got.errors.length) {
        if (!isDraft) { say(T("pl.spec.need", got.errors.map(n => T("pl.spec." + n)).join(", ")), true); return; }
        missing = got.errors;
      } else changes.spec = got.spec;
    }
    const field = document.getElementById("pl-d-produces");
    if (field) {
      const parts = field.value.split(",").map(x => x.trim()).filter(Boolean).map(x => x.split(":").map(y => y.trim()));
      if (parts.some(p => !/^[A-Za-z_][A-Za-z0-9_]*$/.test(p[0]) || p.length > 2)) { say(T("pl.produces.bad"), true); return; }
      changes.produces = parts.map(p => ({ name: p[0], type: p[1] || "any" }));
    }
    const tries = Number(document.getElementById("pl-d-tries").value) || 1, wait = Number(document.getElementById("pl-d-wait").value) || 0;
    const old = (S.snap.plan.nodes[id] || {}).retry_policy || {};
    changes.retry_policy = { max_attempts: tries, wait_seconds: wait, backoff: old.backoff || 2 };
    if (isDraft && !missing.length) { const sv = { ...was.state_variables }; delete sv.draft; changes.state_variables = sv; }
    const r = await event({ type: "plan:update_node", node_id: id, changes }, missing.length ? null : T("pl.saved"));
    if (isDraft && r && !r.ok && !missing.length) {                                         // not complete yet: what was typed is kept, the step stays a draft
      const { title, description, assignee, retry_policy } = changes;
      await event({ type: "plan:update_node", node_id: id, changes: { title, description, assignee, retry_policy } }, null);
    } else if (r && r.ok && missing.length) say(T("pl.draft.saved", missing.map(n => T("pl.spec." + n)).join(", ")));
  }
  function noRouteText(f) {
    const exits = (f.exits || []).map(x => x.condition + " → " + (x.holds === true ? T("pl.nr.holds") : x.holds === false ? T("pl.nr.not") : T("pl.nr.unreadable", x.error || "")));
    return exits.join("; ") + " · " + JSON.stringify(f.values || {});
  }
  async function mark(id, status, outBox) {
    let outputs;
    if (outBox && outBox.value.trim()) {
      try { outputs = JSON.parse(outBox.value); } catch (e) { say(T("pl.badjson"), true); return; }
      if (!outputs || typeof outputs !== "object" || Array.isArray(outputs)) { say(T("pl.badjson"), true); return; }
    }
    const r = await event({ type: "plan:mark", node_id: id, status, ...(outputs ? { outputs } : {}) }, null);
    if (r && r.ok) say(r.data.no_route ? T("pl.noroute.say") : T("pl.marked"), !!r.data.no_route);
  }
  /* Going back does not undo what already happened outside (a call, a payment, a command): the page asks the server what would stay done,
     and when something would, lists it and waits for an explicit "Rewind anyway". */
  async function rewind(id) {
    const r = await api(planPath("/events"), { type: "plan:rewind_impact", node_id: id });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    const stays = r.data.not_undone || [];
    if (!stays.length) { S.rewindAsk = null; await event({ type: "plan:rewind", node_id: id }, T("pl.rewound")); return; }
    S.rewindAsk = { id, stays };
    S.rewindFocus = true;
    render();
  }
  async function rewindAnyway() {
    const ask = S.rewindAsk;
    if (!ask) return;
    S.rewindAsk = null;
    await event({ type: "plan:rewind", node_id: ask.id, accept_irreversible: true }, T("pl.rewound"));
  }
  function cancelRewind() { S.rewindAsk = null; render(); }
  function rewindBox(ask) {
    const node = S.snap.plan.nodes[ask.id];
    return el("div", { class: "pl-rewind", role: "group", "aria-labelledby": "pl-rw-h" },
      el("p", { class: "pl-rw-h", id: "pl-rw-h", text: T("pl.rewind.stays", node ? node.title : ask.id) }),
      el("ul", { class: "pl-rw-list" }, ask.stays.map(e => el("li", { text: T("pl.rewind.line", e.title || e.step, e.what || e.tool || "") }))),
      el("div", { class: "pl-acts" },
        el("button", { type: "button", id: "pl-rw-go", class: "btn pl-btn pl-danger", text: T("pl.rewind.anyway"), onclick: rewindAnyway }),
        el("button", { type: "button", id: "pl-rw-no", class: "btn pl-btn", text: T("pl.rewind.cancel"), onclick: cancelRewind })));
  }
  async function userDiff(diff, title) {
    const p = await api(planPath("/events"), { type: "plan:propose_diff", title, source: "user", diff });
    if (!p.ok) { say(errText(p.data) || T("pl.failed"), true); return false; }
    const r = await event({ type: "plan:approve", proposal_id: p.data.proposal_id }, T("pl.ev.applied"));
    return !!(r && r.ok);
  }

  /* ---- proposals and offers ---- */
  function drawProposals() {
    ui.proposals.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-props-h", text: T("pl.props") })));
    const list = S.snap.proposals;
    if (!list.length) { ui.proposals.append(el("p", { class: "pad dim", text: T("pl.props.none") })); return; }
    ui.proposals.append(el("ul", { class: "pad pl-plist" }, list.map(p => {
      const ch = p.changes;
      const chips = [["added", ch.added], ["updated", ch.updated], ["removed", ch.removed]].filter(c => c[1].length)
        .map(c => el("span", { class: `pl-chip c-${c[0]}`, text: `${T("pl.chg." + c[0])}: ${c[1].map(id => (S.snap.plan.nodes[id] || {}).title || id).join(", ")}` }));
      return el("li", { class: "pl-prop" + (S.proposal === p.proposal_id ? " sel" : "") },
        el("button", { type: "button", class: "pl-ptitle", "aria-pressed": String(S.proposal === p.proposal_id),
          onclick: () => { S.proposal = p.proposal_id; render(); } }, el("b", { text: p.title })),
        p.rationale ? el("p", { class: "pl-prat", text: p.rationale }) : [],
        (p.involves || []).length ? el("p", { class: "pl-prat pl-waits", text: T("pl.props.involves", p.involves.join("; ")) }) : [],
        el("div", { class: "pl-chips" }, chips),
        el("div", { class: "pl-acts" },
          el("button", { type: "button", class: "btn primary pl-btn", title: T("pl.tip.approve"), text: T("pl.approve"),
            onclick: () => event({ type: "plan:approve", proposal_id: p.proposal_id }, T("pl.ev.applied")) }),
          el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.reject"), text: T("pl.reject"),
            onclick: () => event({ type: "plan:reject", proposal_id: p.proposal_id }, T("pl.rejected")) })));
    })));
  }

  function drawOffers() {
    ui.offers.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-offers-h", text: T("pl.offers") })));
    const list = S.snap.offers;
    if (!list.length) { ui.offers.append(el("p", { class: "pad dim", text: T("pl.offers.none") })); return; }
    ui.offers.append(el("ul", { class: "pad pl-plist" }, list.map(o => {
      const node = S.snap.plan.nodes[o.node_id];
      return el("li", { class: "pl-prop" },
        el("p", { class: "pl-ptitle" }, el("b", { text: node ? node.title : o.node_id })),
        el("p", { class: "pl-prat", text: T("pl.offer.reason", o.reason) }),
        el("p", { class: "pl-prat", text: T("pl.offer.alt", o.alternative) }),
        el("div", { class: "pl-acts" },
          el("button", { type: "button", class: "btn primary pl-btn", text: T("pl.offer.accept"),
            onclick: () => event({ type: "plan:fallback_response", offer_id: o.offer_id, accept_alternative: true }, T("pl.offer.accepted")) }),
          el("button", { type: "button", class: "btn pl-btn", text: T("pl.offer.mine"),
            onclick: () => event({ type: "plan:fallback_response", offer_id: o.offer_id, accept_alternative: false }, T("pl.offer.assigned")) })));
    })));
  }

  /* ---- add a task ---- */
  /* the kinds of step the server offers, and the fields of the one chosen */
  const kindList = () => (S.snap && S.snap.kinds) || TYPES.map(t => ({ type: t, family: "action", fields: [] }));
  /* what the plan did and wrote, as a conversation with whoever works on it */
  function drawLog() {
    const snap = S.snap, ref = (snap.schedule && snap.schedule.session_ref) || snap.created_by || "";
    ui.runlog.setSend(ref ? text => RunLog.sendToSession(ref, T("rl.about.plan", snap.plan.title, snap.plan.plan_id, text)) : null);
    ui.runlog.set(RunLog.planEntries(snap.plan.journal, snap.history, a => T("pl.actor." + a)));
  }
  /* the palette: a kind dragged onto the plan, or tapped, is put on the plan at once, with the place it was let go; it needs no links and no properties yet */
  function drawPalette() {
    const kinds = kindList(), key = kinds.map(k => k.type).join(",") + document.documentElement.lang;
    if (key === S.kindsKey) return;
    S.kindsKey = key;
    if (ui.palBox && ui.palBox.dispose) ui.palBox.dispose();
    ui.palBody.replaceChildren(ui.palBox = PlanKinds.palette(kinds, {
      T, canvas: () => ui.graph, over: e => { const hit = document.elementFromPoint(e.clientX, e.clientY); return !!(hit && ui.graph.contains(hit)); },
      pick: type => putKind(type, null),
      drop: (type, e) => {
        const svgEl = S.svg, ctm = svgEl && svgEl.getScreenCTM();
        let at = null;
        if (ctm) { const pt = svgEl.createSVGPoint(); pt.x = e.clientX; pt.y = e.clientY; const q = pt.matrixTransform(ctm.inverse()); at = { x: clamp(q.x - W / 2), y: clamp(q.y - H / 2) }; }
        putKind(type, at);
      },
    }));
  }
  /* a step put on the plan before it is set up is a draft: the server keeps whatever it is given and does not run it until it is complete */
  async function putKind(type, at) {
    if (!S.snap) return;
    const fields = PlanKinds.fieldsOf(kindList(), type);
    const needs = fields.length > 0 || type === "decision" || type === "sensor";
    const id = "n_" + Math.random().toString(36).slice(2, 8);
    const node = { id, type, title: T("pl.type." + type), description: "", assignee: type === "input" ? "user" : "agent", dependencies: [], outputs: [],
      ...(needs ? { state_variables: { draft: true } } : {}), ...(at ? { position: at } : {}) };
    if (!(await userDiff({ add_nodes: [node] }, T("pl.add")))) return;
    S.selected = id;
    S.edge = "";
    S.tab = "define";
    render();
    say(T(at ? "pl.pal.dropped" : "pl.pal.picked", T("pl.type." + type)));
  }
  async function createPlan(e) {
    e.preventDefault();
    const title = document.getElementById("pl-new-title").value.trim();
    if (!title) return;
    const slug = title.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "plan";
    const plan_id = `${slug}_${Math.random().toString(36).slice(2, 6)}`;
    const r = await api("api/plans", { plan: { plan_id, title, target_type: "occasional" } });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    document.getElementById("pl-new-title").value = "";
    ui.newForm.open = false;
    S.id = plan_id;
    await loadList(true);
    say(T("pl.created", title));
  }

  /* ---- the plan's own details, and archiving ---- */
  /* A button that asks twice before it acts: the first press changes its label, the second goes ahead. */
  function twiceButton(label, sure, act, cls) {
    const b = el("button", { type: "button", class: cls, text: label });
    let armed = false, t = 0;
    b.addEventListener("click", () => {
      if (!armed) { armed = true; b.textContent = sure; t = setTimeout(() => { armed = false; b.textContent = label; }, 5000); return; }
      clearTimeout(t);
      armed = false;
      b.textContent = label;
      act();
    });
    return b;
  }
  const toLines = obj => Object.entries(obj || {}).map(([k, v]) => `${k}: ${v}`).join("\n");
  /* "name: text" on each line; null when a line has no name or no colon */
  function fromLines(text) {
    const out = {};
    for (const line of text.split("\n").map(l => l.trim()).filter(Boolean)) {
      const i = line.indexOf(":");
      if (i < 1) return null;
      out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
    }
    return out;
  }
  const lines = text => text.split("\n").map(l => l.trim()).filter(Boolean);

  async function copyMermaid() {
    const r = await fetch(`api/plans/${encodeURIComponent(S.id)}/mermaid`, { cache: "no-store", headers: authHeaders() });
    if (!r.ok) { say(T("pl.mermaid.fail")); return; }
    const text = (await r.json()).mermaid || "";
    try { await navigator.clipboard.writeText(text); say(T("pl.mermaid.copied")); }
    catch (e) {                                           // no clipboard here: show the text to copy by hand
      const box = el("textarea", { class: "pl-mermaid", rows: "10", readonly: true, "aria-label": T("pl.mermaid"), text });
      ui.manage.append(box); box.focus(); box.select(); say(T("pl.mermaid.manual"));
    }
  }

  /* ---- how this plan sits with the long-term ones: what the dates and amounts show, and what the model is asked on request ---- */
  function drawAlign() {
    if (!S.snap) return;
    const a = S.align || { conflicts: [], checks: [] };
    const items = [
      ...a.conflicts.map(c => el("li", { class: "pl-al conflict" }, el("b", { text: T("pl.al.conflict") }), " ", conflictText(c))),
      ...a.checks.map(c => el("li", { class: "pl-al check" }, el("b", { text: T("pl.al.check") }), " ", c.text))];
    const ask = el("button", { type: "button", class: "btn pl-btn", text: T("pl.al.ask"), onclick: askModel });
    const body = el("div", { class: "pad" });
    if (S.snap.review_due) {
      body.append(el("p", { class: "pl-due", role: "status", text: T("pl.al.due") }),
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.al.reviewed"), onclick: reviewedToday }));
    }
    body.append(el("p", { class: "help", text: T("pl.al.help") }),
      items.length ? el("ul", { class: "pl-allist" }, items) : el("p", { class: "dim", text: T("pl.al.none") }),
      ...(a.out_of_date ? [el("p", { class: "help", text: T("pl.al.old") })] : []), ask);
    ui.align.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-al-h", text: T("pl.al.title") })), body);
  }
  /* the conflicts the server finds by arithmetic come with their values; the page says them in its own language */
  function conflictText(c) {
    const d = c.data || {};
    if (c.kind === "time" && d.start) return T("pl.al.t.time", c.plan_title, d.start, d.end, c.with_title, d.commitment, d.from, d.to);
    if (c.kind === "budget" && d.total) {
      const where = d.where === "all" ? T("pl.al.t.all") : d.where === "own" ? T("pl.al.t.own") : T("pl.al.t.in", d.where);
      return T("pl.al.t.budget", `${d.total} ${d.currency}`, where, `${d.limit} ${d.currency}`, c.with_title, d.period === "total" ? T("pl.hz.period.total") : T("pl.hz.period.month"));
    }
    return c.text;
  }
  async function askModel(e) {
    const b = e.currentTarget;
    b.disabled = true; b.textContent = T("pl.al.reading");
    const r = await api(planPath("/alignment"), { model: true });
    if (!r.ok) { say(errText(r.data) || T("pl.al.failed"), true); b.disabled = false; b.textContent = T("pl.al.ask"); return; }
    S.align = r.data;
    drawAlign();
  }
  const today = () => new Date().toISOString().slice(0, 10);
  async function reviewedToday() {
    await event({ type: "plan:update_meta", changes: { profile: { ...S.snap.profile, last_review: today() } } }, T("pl.hz.saved"));
    await refresh();
  }

  /* ---- aims, dates and amounts: what a plan says about time and money, which is what plans are compared on ---- */
  function drawHorizon() {
    if (!S.snap) return;
    const key = `${S.id}:${S.snap.rev}`;
    if (ui.horizon.dataset.key === key) return;
    if (ui.horizon.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
    ui.horizon.dataset.key = key;
    const p = S.snap.profile || {}, constant = plan().target_type === "constant";
    const field = (id, label, node) => [el("label", { for: id, text: label }), node];
    const money = (m, d) => (m ? m.amount : d || "");
    const rows = [];
    if (constant) {
      rows.push(
        ...field("pl-h-goals", T("pl.hz.goals"), el("textarea", { id: "pl-h-goals", rows: "3", text: (p.goals || []).join("\n") })),
        ...field("pl-h-review", T("pl.hz.review"), el("input", { id: "pl-h-review", type: "number", min: "1", max: "30", inputmode: "numeric", value: String(p.review_every_days || 30) })),
        ...field("pl-h-last", T("pl.hz.last"), el("input", { id: "pl-h-last", type: "date", value: p.last_review || "" })),
        ...field("pl-h-bud", T("pl.hz.budget"), el("input", { id: "pl-h-bud", inputmode: "decimal", autocomplete: "off", value: money(p.budget) })),
        ...field("pl-h-budcur", T("pl.hz.currency"), el("input", { id: "pl-h-budcur", maxlength: "3", autocomplete: "off", value: p.budget ? p.budget.currency : "EUR" })),
        ...field("pl-h-per", T("pl.hz.period"), el("select", { id: "pl-h-per" }, ["month", "total"].map(k =>
          el("option", { value: k, text: T(k === "month" ? "pl.hz.period.month" : "pl.hz.period.total"), selected: (p.budget ? p.budget.period : "month") === k })))),
        ...field("pl-h-com", T("pl.hz.commit"), el("textarea", { id: "pl-h-com", rows: "3", spellcheck: "false", placeholder: T("pl.hz.commit.ph"),
          text: (p.commitments || []).map(c => `${c.text} | ${c.from} | ${c.to}`).join("\n") })));
    } else {
      rows.push(
        ...field("pl-h-start", T("pl.hz.start"), el("input", { id: "pl-h-start", type: "date", value: p.starts_on || "" })),
        ...field("pl-h-end", T("pl.hz.end"), el("input", { id: "pl-h-end", type: "date", value: p.ends_on || "" })),
        ...field("pl-h-cost", T("pl.hz.cost"), el("input", { id: "pl-h-cost", inputmode: "decimal", autocomplete: "off", value: money(p.cost) })),
        ...field("pl-h-costcur", T("pl.hz.currency"), el("input", { id: "pl-h-costcur", maxlength: "3", autocomplete: "off", value: p.cost ? p.cost.currency : "EUR" })));
    }
    ui.horizon.replaceChildren(head(T("pl.hz.title")),
      el("form", { class: "pad pl-dform", onsubmit: saveHorizon }, el("p", { class: "help", text: T(constant ? "pl.hz.help.long" : "pl.hz.help.short") }), ...rows,
        el("button", { class: "btn primary pl-btn", text: T("pl.save") })));
  }
  async function saveHorizon(e) {
    e.preventDefault();
    const val = id => document.getElementById(id).value.trim();
    const amount = (a, c, extra) => (val(a) ? { amount: val(a), currency: val(c).toUpperCase(), ...extra } : null);
    let profile;
    if (plan().target_type === "constant") {
      const commitments = [];
      for (const line of val("pl-h-com").split("\n").map(l => l.trim()).filter(Boolean)) {
        const [text, from, to] = line.split("|").map(x => x.trim());
        if (!text || !from || !to) { say(T("pl.hz.badlines"), true); return; }
        commitments.push({ text, from, to });
      }
      profile = { ...S.snap.profile, goals: val("pl-h-goals").split("\n").map(l => l.trim()).filter(Boolean), review_every_days: Number(val("pl-h-review")) || 30,
        last_review: val("pl-h-last"), budget: amount("pl-h-bud", "pl-h-budcur", { period: document.getElementById("pl-h-per").value }), commitments };
    } else {
      profile = { ...S.snap.profile, starts_on: val("pl-h-start"), ends_on: val("pl-h-end"), cost: amount("pl-h-cost", "pl-h-costcur") };
    }
    ui.horizon.dataset.key = "";
    await event({ type: "plan:update_meta", changes: { profile } }, T("pl.hz.saved"));
    await refresh();
  }

  function drawManage() {
    if (!S.snap) return;
    const key = `${S.id}:${S.snap.rev}`;
    if (ui.manage.dataset.key === key) return;
    if (ui.manage.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
    ui.manage.dataset.key = key;
    const p = plan(), card = S.snap.card || {};
    const field = (id, label, node) => [el("label", { for: id, text: label }), node];
    ui.manage.replaceChildren(head(T("pl.manage")),
      el("form", { class: "pad pl-dform", onsubmit: saveMeta },
        el("p", { class: "help", text: T("pl.manage.help") }),
        field("pl-m-title", T("pl.f.title"), el("input", { id: "pl-m-title", value: p.title, maxlength: "200", required: true, autocomplete: "off" })),
        field("pl-m-kind", T("pl.f.kind"), el("select", { id: "pl-m-kind" },
          ["occasional", "constant"].map(k => el("option", { value: k, text: T("pl.kind." + k), selected: k === p.target_type })))),
        el("p", { class: "help", text: T("pl.kind.help") }),
        el("p", { class: "help", text: T("pl.card.help") }),
        ...((S.snap.card_missing || []).length ? [el("p", { class: "pl-fact", role: "status", text: T("pl.card.missing", S.snap.card_missing.map(f => T("pl.card." + f)).join(", ")) })] : []),
        ...((S.snap.card_auto || []).length ? [el("p", { class: "help", text: T("pl.card.auto", S.snap.card_auto.map(f => T("pl.card." + f)).join(", ")) })] : []),
        field("pl-m-c-titolo", T("pl.meta.cardtitle"), el("input", { id: "pl-m-c-titolo", value: card.titolo || p.title, maxlength: "200", autocomplete: "off" })),
        field("pl-m-c-date", T("pl.card.date"), el("input", { id: "pl-m-c-date", value: card.date || "", autocomplete: "off" })),
        field("pl-m-c-dest", T("pl.card.destinazione"), el("input", { id: "pl-m-c-dest", value: card.destinazione || "", autocomplete: "off" })),
        field("pl-m-c-people", T("pl.card.partecipanti"), el("textarea", { id: "pl-m-c-people", rows: "3", text: (card.partecipanti || []).join("\n") })),
        el("p", { class: "help", text: T("pl.meta.people.help") }),
        field("pl-m-c-logi", T("pl.card.logistica_e_prenotazioni"), el("textarea", { id: "pl-m-c-logi", rows: "3", spellcheck: "false", text: toLines(card.logistica_e_prenotazioni) })),
        field("pl-m-c-prof", T("pl.card.profilo_e_vincoli_partecipanti"), el("textarea", { id: "pl-m-c-prof", rows: "3", spellcheck: "false", text: toLines(card.profilo_e_vincoli_partecipanti) })),
        el("p", { class: "help", text: T("pl.meta.lines.help") }),
        el("button", { class: "btn primary pl-btn", text: T("pl.save") })),
      el("div", { class: "pad" },
        el("p", { class: "help", text: T("pl.mermaid.help") }),
        el("button", { type: "button", class: "btn pl-btn", title: T("pl.tip.mermaid"), text: T("pl.mermaid"), onclick: copyMermaid })),
      el("div", { class: "pad pl-archive-zone" },
        el("p", { class: "help", text: T("pl.archive.help") }),
        twiceButton(T("pl.archive"), T("pl.archive.sure"), archivePlan, "btn pl-btn pl-danger")));
  }

  async function saveMeta(e) {
    e.preventDefault();
    const val = id => document.getElementById(id).value;
    const title = val("pl-m-title").trim();
    if (!title) { say(T("pl.f.title.required"), true); return; }
    const changes = { title, target_type: val("pl-m-kind") };
    const logistics = fromLines(val("pl-m-c-logi")), profiles = fromLines(val("pl-m-c-prof"));
    if (logistics === null || profiles === null) { say(T("pl.meta.badlines"), true); return; }
    const card = { titolo: val("pl-m-c-titolo").trim() || title, date: val("pl-m-c-date").trim(), destinazione: val("pl-m-c-dest").trim(),
      partecipanti: lines(val("pl-m-c-people")), logistica_e_prenotazioni: logistics, profilo_e_vincoli_partecipanti: profiles };
    if (S.snap.card || card.date || card.destinazione || card.partecipanti.length || Object.keys(logistics).length || Object.keys(profiles).length) {
      changes.card = card;
    }
    ui.manage.dataset.key = "";
    await event({ type: "plan:update_meta", changes }, T("pl.meta.saved"));
    await loadList(true);
  }

  async function archivePlan() {
    const title = plan().title;
    const r = await api(planPath("/archive"), { actor: "user" });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.id = "";
    await loadList(false);
    if (ui.archived.open) await loadArchived();
    say(T("pl.archived", title));
  }

  function buildArchived() {
    ui.archivedBody = el("div", { class: "pad" });
    ui.archived.replaceChildren(head(T("pl.archives")), ui.archivedBody);
  }
  async function loadArchived() {
    const r = await api("api/plans?archived=1");
    if (!r.ok) { ui.archivedBody.replaceChildren(el("p", { class: "dim", text: errText(r.data) || T("pl.loadfail") })); return; }
    const list = r.data.plans || [];
    if (!list.length) { ui.archivedBody.replaceChildren(el("p", { class: "dim", text: T("pl.archives.none") })); return; }
    ui.archivedBody.replaceChildren(el("ul", { class: "pl-plist" }, list.map(p => el("li", { class: "pl-prop" },
      el("p", { class: "pl-ptitle" }, el("b", { text: `${p.title} (${p.completed}/${p.nodes})` })),
      el("div", { class: "pl-acts" }, el("button", { type: "button", class: "btn pl-btn", text: T("pl.restore"),
        "aria-label": `${T("pl.restore")}: ${p.title}`, onclick: () => restorePlan(p.plan_id, p.title) }))))));
  }
  async function restorePlan(id, title) {
    const r = await api(`api/plans/${encodeURIComponent(id)}/restore`, { actor: "user" });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.id = id;
    await loadList(true);
    await loadArchived();
    say(T("pl.restored", title));
  }

  /* ---- who changed what ---- */
  function drawHistory() {
    ui.history.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-hist-h", text: T("pl.hist") })));
    const items = (S.snap.history || []).slice().reverse();
    if (!items.length) { ui.history.append(el("p", { class: "pad dim", text: T("pl.hist.none") })); return; }
    ui.history.append(el("ul", { class: "pl-hist" }, items.map(x => {
      const when = new Date(x.t * 1000);
      return el("li", {},
        el("time", { datetime: when.toISOString(), text: when.toLocaleString() }),
        el("span", { class: `pl-actor a-${x.actor}`, text: T("pl.actor." + x.actor) }),
        el("span", { text: `${T("pl.h." + x.type)}: ${x.summary}` }));
    })));
  }

  /* ---- judge events ---- */
  let triageOut;
  function buildTriage() {
    triageOut = el("div", { class: "pl-tout", role: "status", "aria-live": "polite" });
    ui.tele = el("p", { class: "pl-tele help" });
    ui.triage.replaceChildren(head(T("pl.triage")),
      el("form", { class: "pad pl-dform", onsubmit: runTriage },
        el("p", { class: "help", text: T("pl.triage.help") }), ui.tele,
        el("label", { for: "pl-t-events", text: T("pl.triage.events") }),
        el("textarea", { id: "pl-t-events", rows: "4", placeholder: T("pl.triage.ph") }),
        el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-t-replan", checked: true }), el("span", { text: T("pl.triage.replan") })),
        el("button", { class: "btn primary pl-btn", text: T("pl.triage.go") }), triageOut));
  }
  /* what the filter has done since the server started: how many judgements, where they were answered, tokens */
  async function loadTele() {
    const r = await api("api/plans/telemetry");
    if (!r.ok || !ui.tele) return;
    const t = r.data.triage;
    const ev = r.data.events || {};
    const lines = [t.calls ? T("pl.tele", t.calls, t.cloud, t.offline, t.fell_back, t.tokens, t.wakes) : T("pl.tele.none")];
    if (ev.pump) lines.push(T("pl.tele.pump", ev.accepted, Object.values(ev.pending || {}).reduce((a, b) => a + b, 0), ev.batches));
    (ev.sources || []).forEach(x => lines.push(T("pl.tele.src", x.id, x.kind, x.polls, x.events) + (x.last_error ? T("pl.tele.src.err", x.last_error) : "")));
    ui.tele.replaceChildren(...lines.flatMap((l, i) => (i ? [document.createElement("br"), document.createTextNode(l)] : [document.createTextNode(l)])));
  }
  async function runTriage(e) {
    e.preventDefault();
    const events = document.getElementById("pl-t-events").value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!events.length || !S.id) return;
    const r = await api(planPath("/triage"), { events, replan: document.getElementById("pl-t-replan").checked });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    showTriage(r.data);
    loadTele();
  }
  function showTriage(r) {
    if (!triageOut) return;
    const lines = [
      el("p", { class: "pl-tverdict " + (r.wake_system_2 ? "wake" : "quiet"), text: r.wake_system_2 ? T("pl.t.wake") : T("pl.t.quiet") }),
      el("p", { text: T("pl.t.source." + r.source) + " · " + (r.sent_to_cloud ? T("pl.t.sent") : T("pl.t.local")) }),
      el("p", { text: T("pl.t.detail", Math.round(r.has_blocker_prob * 100), r.impact_scope, r.max_impact || "—") }),
    ];
    if (r.replan_state === "in_progress") lines.push(el("p", { text: T("pl.t.inprogress") }));
    else if (r.replanning) lines.push(el("p", { text: T("pl.t.replanning") }));
    if (r.replan_error) lines.push(el("p", { class: "bad", text: r.replan_error }));
    (r.reasons || []).slice(0, 5).forEach(x => lines.push(el("p", { class: "pl-treason", text: x })));
    triageOut.replaceChildren(...lines);
  }

  /* ---- planner settings: the cloud consent ---- */
  function buildSettings() {
    const host = document.getElementById("st-planner-body");
    if (!host) return false;
    ui.setBody = el("form", { class: "pad pl-dform", onsubmit: saveSettings });
    host.replaceChildren(ui.setBody);
    return true;
  }
  const FIELDS = ["titolo", "date", "destinazione", "partecipanti", "logistica_e_prenotazioni", "profilo_e_vincoli_partecipanti"];
  function drawSettings() {
    const s = S.settings;
    if (!s) { ui.setBody.replaceChildren(el("p", { class: "dim", text: T("pl.loadfail") })); return; }
    const picked = s.card_fields || s.card_fields_default || FIELDS;
    const personal = s.card_fields_personal || [];
    ui.setBody.replaceChildren(
      el("p", { class: "help", text: T("pl.set.help") }),
      el("p", { class: "pl-fact", text: s.key_present ? T("pl.set.key.yes") : s.key_saved ? T("pl.set.key.unused") : T("pl.set.key.no") }),
      ...(s.key_saved ? [el("p", { class: "help", text: T("pl.set.key.saved") })] : []),
      el("label", { for: "pl-s-key", text: T("pl.set.key.field") }),
      el("input", { id: "pl-s-key", type: "password", autocomplete: "new-password", autocapitalize: "off", spellcheck: "false", "aria-describedby": "pl-s-key-h" }),
      el("p", { class: "help", id: "pl-s-key-h", text: T("pl.set.key.field.help") }),
      ...(s.key_saved ? [el("button", { type: "button", class: "btn pl-btn", text: T("pl.set.key.remove"), onclick: removeKey })] : []),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-cloud", checked: s.cloud_triage, disabled: !s.key_present, "aria-describedby": "pl-s-cloud-h" }), el("span", { text: T("pl.set.cloud") })),
      el("p", { class: "help", id: "pl-s-cloud-h", text: T("pl.set.cloud.help") }),
      ...(s.cloud_triage ? [riskLabel(T("pl.risk.cloud"))] : []),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-pseudo", checked: s.pseudonymize, "aria-describedby": "pl-s-pseudo-h" }), el("span", { text: T("pl.set.pseudo") })),
      el("p", { class: "help", id: "pl-s-pseudo-h", text: T("pl.set.pseudo.help") }),
      el("label", { for: "pl-s-gate", text: T("pl.set.gate") }),
      el("select", { id: "pl-s-gate" }, ["or", "and"].map(m => el("option", { value: m, text: T("pl.set.gate." + m), selected: (s.gate_mode || "or") === m }))),
      el("p", { class: "help", text: T("pl.set.gate.help") }),
      el("label", { for: "pl-s-thr", text: T("pl.set.threshold") }),
      el("input", { id: "pl-s-thr", type: "number", min: "0", max: "1", step: "0.01", inputmode: "decimal", value: String(s.blocker_threshold ?? 0.8), "aria-describedby": "pl-s-thr-h" }),
      el("p", { class: "help", id: "pl-s-thr-h", text: T("pl.set.threshold.help") }),
      el("label", { for: "pl-s-to", text: T("pl.set.timeout") }),
      el("input", { id: "pl-s-to", type: "number", min: "0.1", step: "any", inputmode: "decimal", value: String(s.cloud_timeout ?? 5), "aria-describedby": "pl-s-to-h" }),
      el("p", { class: "help", id: "pl-s-to-h", text: T("pl.set.timeout.help") }),
      el("label", { for: "pl-s-chunk", text: T("pl.set.chunk") }),
      el("input", { id: "pl-s-chunk", type: "number", min: "1", step: "1", inputmode: "numeric", value: String(s.max_events ?? 20), "aria-describedby": "pl-s-chunk-h" }),
      el("p", { class: "help", id: "pl-s-chunk-h", text: T("pl.set.chunk.help") }),
      el("fieldset", { class: "pl-parents" }, el("legend", { text: T("pl.set.fields") }), el("p", { class: "help", text: T("pl.set.fields.help") }),
        FIELDS.map(f => [el("label", { class: "pl-check" }, el("input", { type: "checkbox", name: "field", value: f, checked: picked.includes(f) }), el("span", { text: T("pl.card." + f) })),
          personal.includes(f) ? el("p", { class: "help", text: T("pl.set.personal.help") }) : []]).flat()),
      ...(s.cloud_triage && personal.some(f => picked.includes(f)) ? [riskLabel(T("pl.risk.personal"))] : []),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-pump", checked: !!s.event_pump }), el("span", { text: T("pl.set.pump") })),
      el("p", { class: "help", text: T("pl.set.pump.help") }),
      ...(s.event_pump && s.cloud_triage ? [riskLabel(T("pl.risk.pump"))] : []),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-auto", checked: s.auto_apply !== false, "aria-describedby": "pl-s-auto-h" }), el("span", { text: T("pl.set.auto") })),
      el("p", { class: "help", id: "pl-s-auto-h", text: T("pl.set.auto.help") }),
      el("label", { for: "pl-s-sources", text: T("pl.set.sources") }),
      el("textarea", { id: "pl-s-sources", rows: "6", spellcheck: "false", text: (s.sources || []).length ? JSON.stringify(s.sources, null, 1) : "" }),
      el("p", { class: "help", text: T("pl.set.sources.help") }),
      el("button", { class: "btn primary pl-btn", text: T("pl.save") }));
  }
  async function loadSettings() {
    if (!ui.setBody && !buildSettings()) return;
    const r = await api("api/planner/settings");
    S.settings = r.ok ? r.data : null;
    drawSettings();
  }
  async function removeKey() {
    const r = await api("api/planner/settings", { clear_jev_key: true });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.settings = r.data;
    drawSettings();
    say(T("pl.saved"));
  }
  async function saveSettings(e) {
    e.preventDefault();
    const fields = [...ui.setBody.querySelectorAll("input[name=field]:checked")].map(i => i.value);
    const standard = (S.settings && S.settings.card_fields_default) || FIELDS;
    const personal = (S.settings && S.settings.card_fields_personal) || [];
    const isStandard = fields.length === standard.length && standard.every(f => fields.includes(f));
    const body = { cloud_triage: ui.setBody.querySelector("#pl-s-cloud").checked, pseudonymize: ui.setBody.querySelector("#pl-s-pseudo").checked,
      gate_mode: ui.setBody.querySelector("#pl-s-gate").value, card_fields: isStandard ? null : fields,
      event_pump: ui.setBody.querySelector("#pl-s-pump").checked, auto_apply: ui.setBody.querySelector("#pl-s-auto").checked,
      blocker_threshold: Number(ui.setBody.querySelector("#pl-s-thr").value), cloud_timeout: Number(ui.setBody.querySelector("#pl-s-to").value),
      max_events: Number(ui.setBody.querySelector("#pl-s-chunk").value) };
    if (![body.blocker_threshold, body.cloud_timeout, body.max_events].every(Number.isFinite)) { say(T("pl.badnum"), true); return; }
    const newKey = ui.setBody.querySelector("#pl-s-key").value.trim();
    if (newKey) body.jev_key = newKey;
    const text = ui.setBody.querySelector("#pl-s-sources").value.trim();
    try { body.sources = text ? JSON.parse(text) : []; } catch (err) { say(T("pl.badjson"), true); return; }
    const sends = fields.some(f => personal.includes(f));
    if (body.cloud_triage && !(S.settings && S.settings.cloud_triage) && !(await riskConfirm([T("pl.risk.cloud"), ...(sends ? [T("pl.risk.personal")] : [])]))) { drawSettings(); return; }
    if (body.event_pump && body.cloud_triage && !(S.settings && S.settings.event_pump && S.settings.cloud_triage) && !(await riskConfirm([T("pl.risk.pump")]))) { drawSettings(); return; }
    const before = (S.settings && (S.settings.card_fields || S.settings.card_fields_default)) || [];
    const hadPersonal = before.some(f => personal.includes(f));
    if (body.cloud_triage && S.settings && S.settings.cloud_triage && sends && !hadPersonal && !(await riskConfirm([T("pl.risk.personal")]))) { drawSettings(); return; }
    const r = await api("api/planner/settings", body);
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.settings = r.data;
    drawSettings();
    say(T("pl.saved"));
  }

  /* ---- everything ---- */
  function render() {
    paintPlans();
    const has = !!S.snap;
    ui.title.textContent = has ? plan().title : T("pl.title");
    ui.pick.hidden = has;
    ui.tabs.hidden = !has;
    TABS.forEach(k => { ui["panel_" + k].hidden = !has || S.tab !== k; });
    ui.instr.hidden = !has;
    ui.palette.hidden = !has;
    if (!has) return;
    drawPalette();
    const nodes = Object.values(S.snap.plan.nodes);
    ui.summary.textContent = T("pl.summary", nodes.length, nodes.filter(n => n.status === "completed").length, S.snap.ready.length,
      Object.keys(S.snap.plan.shared_state).length);
    if (S.snap.watch_degraded) ui.summary.textContent += " · " + T("pl.degraded." + S.snap.watch_degraded);
    ui.skillBtn.hidden = !(nodes.length && nodes.every(n => n.status === "completed" || n.status === "skipped") && nodes.some(n => n.status === "completed"));   // a plan that ended well
    drawGraph();
    drawList();
    const editing = ui.detail.contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
    if (!editing) drawDetail();
    if (S.rewindAsk && (S.rewindFocus || document.activeElement === document.body)) {
      const no = document.getElementById("pl-rw-no");
      if (no) no.focus();
      S.rewindFocus = false;
    }
    drawProposals();
    drawOffers();
    drawLog();
    drawHistory();
    drawAlign();
    drawHorizon();
    drawManage();
  }

  /* ---- start when the view is shown; stop the stream when it is left ---- */
  // the Chat links to a plan it touched: the id waits in the session storage until this view opens
  function wanted() {
    let id = "";
    try { id = sessionStorage.getItem("tadamoo.openplan") || ""; sessionStorage.removeItem("tadamoo.openplan"); } catch (e) { /* no link */ }
    return id;
  }
  async function enter() {
    if (!S.ready) { build(); S.ready = true; await loadList(false); } else await loadList(true);
    const id = wanted();
    if (id && id !== S.id && S.plans.some(p => p.plan_id === id)) { setTab("define", true); await choose(id); }
  }
  document.addEventListener("tadamoo:view", e => {
    if (e.detail === "settings") loadSettings();
    if (e.detail === "plans") enter();
    else if (S.ready) stopStream();
  });
  document.addEventListener("DOMContentLoaded", loadSettings);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopStream();
    else if (document.body.dataset.view === "plans" && S.id) startStream();
  });
  if (document.body.dataset.view === "plans") enter();
})();
