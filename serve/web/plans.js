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
  const DIRECT = ["plan:update_node", "plan:mark", "plan:rewind", "plan:update_meta", "plan:fallback_response"];
  const S = {
    plans: [], id: "", snap: null, selected: "", proposal: "", mode: "", seq: 0, running: false, ready: false,
    settings: null, refreshTimer: null, abort: null, flash: {}, align: null,
  };

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
  function build() {
    statusEl = el("p", { class: "pl-status", role: "status", "aria-live": "polite" });
    connEl = el("span", { class: "pl-conn", "data-s": "off" });
    ui.select = el("select", { id: "pl-select", onchange: e => choose(e.target.value) });
    ui.newForm = el("details", { class: "pl-new", id: "pl-new" },
      el("summary", { class: "btn pl-btn", text: T("pl.new") }),
      el("form", { class: "pl-newform", onsubmit: createPlan },
        el("label", { for: "pl-new-title", text: T("pl.new.title") }),
        el("input", { id: "pl-new-title", required: true, maxlength: "120", autocomplete: "off" }),
        el("button", { class: "btn primary pl-btn", text: T("pl.create") })));
    ui.modeGraph = el("button", { type: "button", class: "btn pl-btn", "aria-pressed": "false", text: T("pl.view.graph"), onclick: () => setMode("graph") });
    ui.modeList = el("button", { type: "button", class: "btn pl-btn", "aria-pressed": "false", text: T("pl.view.list"), onclick: () => setMode("list") });
    ui.summary = el("p", { class: "pl-summary" });
    ui.graph = el("div", { class: "pl-graph", role: "group", "aria-label": T("pl.graph.label") });
    ui.list = el("ol", { class: "pl-list", "aria-label": T("pl.list.label") });
    ui.detail = el("section", { class: "sec pl-detail", "aria-labelledby": "pl-detail-h" });
    ui.proposals = el("section", { class: "sec pl-props", "aria-labelledby": "pl-props-h" });
    ui.offers = el("section", { class: "sec pl-offers", "aria-labelledby": "pl-offers-h" });
    ui.add = el("details", { class: "sec pl-add" });
    ui.manage = el("details", { class: "sec pl-manage" });
    ui.history = el("section", { class: "sec pl-history", "aria-labelledby": "pl-hist-h" });
    ui.align = el("section", { class: "sec pl-align", "aria-labelledby": "pl-al-h" });
    ui.horizon = el("details", { class: "sec pl-horizon" });
    ui.archived = el("details", { class: "sec pl-archived", ontoggle: e => { if (e.target.open) loadArchived(); } });
    ui.triage = el("details", { class: "sec pl-triage" });
    ui.empty = el("p", { class: "pl-empty dim", text: T("pl.none") });
    root.replaceChildren(
      el("div", { class: "pl-bar" },
        el("label", { for: "pl-select", text: T("pl.select") }), ui.select, connEl,
        el("div", { class: "pl-modes", role: "group", "aria-label": T("pl.view.label") }, ui.modeGraph, ui.modeList)),
      ui.newForm, ui.archived, statusEl, ui.empty, ui.summary,
      el("div", { class: "pl-main" }, el("div", { class: "pl-canvas" }, ui.graph, ui.list), el("div", { class: "pl-side" }, ui.detail, ui.align, ui.proposals, ui.offers, ui.history)),
      ui.add, ui.horizon, ui.manage, ui.triage);
    buildAdd();
    buildTriage();
    buildArchived();
    setMode(narrow() ? "list" : "graph", true);
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
  async function loadList(keep) {
    const r = await api("api/plans");
    if (!r.ok) { say(errText(r.data) || T("pl.loadfail"), true); return; }
    S.plans = r.data.plans || [];
    ui.select.replaceChildren(...S.plans.map(p => el("option", { value: p.plan_id,
      text: `${p.title} (${p.completed}/${p.nodes})` + (p.conflicts && p.conflicts.length ? ` · ${T("pl.al.n", p.conflicts.length)}` : "") + (p.review_due ? ` · ${T("pl.review.due")}` : "") })));
    const id = keep && S.plans.some(p => p.plan_id === S.id) ? S.id : (S.plans[0] ? S.plans[0].plan_id : "");
    ui.select.value = id;
    ui.empty.hidden = S.plans.length > 0;
    await choose(id);
  }

  async function choose(id) {
    stopStream();
    S.id = id;
    S.snap = null;
    S.selected = S.proposal = "";
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
      say(T("pl.ev.replan", ev.proposals.length));
    } else if (ev.type === "plan:replan_failed") {
      say(T("pl.ev.replanfail", ev.error), true);
    } else if (ev.type === "plan:fallback_offered") {
      say(T("pl.ev.offer", ev.offer.node_id));
    } else if (ev.type === "plan:triage") {
      showTriage(ev.result);
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
    return { pos, ghosts, width: PAD * 2 + cols * W + (cols - 1) * GX, height: PAD * 2 + maxRows * H + (maxRows - 1) * GY };
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
    return svg("rect", { x, y, width: W, height: H, rx: type === "sensor" ? H / 2 : 8 });
  }

  function drawGraph() {
    const lay = layoutOf(S.snap);
    const nodes = S.snap.plan.nodes;
    const root_ = svg("svg", { class: "pl-svg", viewBox: `0 0 ${lay.width} ${lay.height}`, role: "img", "aria-label": T("pl.graph.label") },
      svg("defs", {}, svg("marker", { id: "pl-arrow", viewBox: "0 0 10 10", refX: "9", refY: "5", markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" },
        svg("path", { d: "M0,0 L10,5 L0,10 z", class: "pl-arrowhead" }))));
    const edge = (a, b, cls) => {
      const p = lay.pos[a], q = lay.pos[b];
      if (!p || !q) return;
      const x1 = p.x + W, y1 = p.y + H / 2, x2 = q.x, y2 = q.y + H / 2, dx = Math.max(24, (x2 - x1) / 2);
      root_.append(svg("path", { d: `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`, class: "pl-edge " + cls, "marker-end": "url(#pl-arrow)" }));
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
        class: `pl-node t-${n.type} st-${status} ${ghost ? "ghost hl-added" : hlClass(n.id)} ${S.selected === n.id ? "sel" : ""}`,
        tabindex: "0", role: "button", "aria-pressed": String(S.selected === n.id),
        "aria-label": `${n.title}. ${T("pl.type." + n.type)}, ${T("pl.status." + status)}, ${T("pl.assignee." + n.assignee)}`,
      }, svg("title", {}, document.createTextNode(`${n.title}\n${n.description || ""}`)), shape(n.type, p.x, p.y));
      const label = svg("text", { x: p.x + W / 2, y: p.y + H / 2 - 4, class: "pl-t", "text-anchor": "middle" });
      label.textContent = clip(n.title, 20);
      const sub = svg("text", { x: p.x + W / 2, y: p.y + H / 2 + 14, class: "pl-s", "text-anchor": "middle" });
      sub.textContent = `${ICON[status]} ${T("pl.status." + status)} · ${T("pl.assignee.short." + n.assignee)}`;
      g.append(label, sub);
      g.addEventListener("click", () => select(n.id));
      g.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(n.id); } });
      if (ghost) g.setAttribute("aria-disabled", "true");
      root_.append(g);
    };
    Object.values(nodes).forEach(n => drawNode(n, false));
    lay.ghosts.forEach(n => drawNode({ ...n, status: "pending", description: n.description || "" }, true));
    ui.graph.replaceChildren(root_);
  }

  function drawList() {
    const order = S.snap.layers.flat();
    const nodes = S.snap.plan.nodes;
    ui.list.replaceChildren(...order.map(id => {
      const n = nodes[id];
      const deps = n.dependencies.length ? T("pl.after", n.dependencies.map(d => nodes[d].title).join(", ")) : T("pl.start");
      return el("li", { class: `pl-li st-${n.status} ${hlClass(id)}` },
        el("button", { type: "button", class: "pl-libtn" + (S.selected === id ? " sel" : ""), "aria-pressed": String(S.selected === id), onclick: () => select(id) },
          el("span", { class: "pl-li-title", text: n.title }),
          el("span", { class: "pl-li-meta", text: `${ICON[n.status]} ${T("pl.status." + n.status)} · ${T("pl.type." + n.type)} · ${T("pl.assignee." + n.assignee)}` }),
          el("span", { class: "pl-li-deps", text: deps })));
    }));
  }

  function select(id) {
    S.selected = id;
    render();
    const h = document.getElementById("pl-detail-h");
    if (h && narrow()) h.scrollIntoView({ block: "nearest" });
  }

  /* ---- detail of one node ---- */
  function drawDetail() {
    const n = S.selected && S.snap.plan.nodes[S.selected];
    ui.detail.replaceChildren(el("div", { class: "sh" }, el("b", { id: "pl-detail-h", text: T("pl.detail") })));
    if (!n) { ui.detail.append(el("p", { class: "pad dim", text: T("pl.detail.none") })); return; }
    const done = n.status === "completed";
    const title = el("input", { id: "pl-d-title", value: n.title, maxlength: "200", disabled: done });
    const desc = el("textarea", { id: "pl-d-desc", rows: "3", disabled: done, text: n.description });
    const assignee = el("select", { id: "pl-d-assignee", disabled: done },
      ["agent", "user", "third_party"].map(a => el("option", { value: a, text: T("pl.assignee." + a), selected: a === n.assignee })));
    const cond = n.type === "decision"
      ? el("textarea", { id: "pl-d-cond", rows: "4", disabled: done, spellcheck: "false", text: JSON.stringify(n.conditions, null, 2) })
      : null;
    const out = el("textarea", { id: "pl-d-out", rows: "2", spellcheck: "false", placeholder: '{"invitati_confermati": ["Anna"]}' });
    const facts = [];
    if (n.state_variables.blocked_by) facts.push(T("pl.blocked_by", n.state_variables.blocked_by.map(i => (S.snap.plan.nodes[i] || {}).title || i).join(", ")));
    if (n.state_variables.fallback_reason) facts.push(T("pl.fallback", n.state_variables.fallback_reason));
    if (n.state_variables.selected) facts.push(T("pl.selected", n.state_variables.selected.join(", ")));
    if (n.state_variables.outputs) facts.push(T("pl.outputs", JSON.stringify(n.state_variables.outputs)));
    const isReady = S.snap.ready.includes(n.id);
    ui.detail.append(el("form", { class: "pad pl-dform", onsubmit: e => { e.preventDefault(); saveNode(n.id, cond); } },
      el("p", { class: "pl-dmeta", text: `${T("pl.type." + n.type)} · ${ICON[n.status]} ${T("pl.status." + n.status)}${isReady ? " · " + T("pl.ready") : ""}` }),
      ...facts.map(f => el("p", { class: "pl-fact", text: f })),
      el("label", { for: "pl-d-title", text: T("pl.f.title") }), title,
      el("label", { for: "pl-d-desc", text: T("pl.f.desc") }), desc,
      el("label", { for: "pl-d-assignee", text: T("pl.f.assignee") }), assignee,
      cond ? [el("label", { for: "pl-d-cond", text: T("pl.f.cond") }), cond, el("p", { class: "help", text: T("pl.f.cond.help") })] : [],
      done ? el("p", { class: "help", text: T("pl.completed.help") }) : el("button", { class: "btn primary pl-btn", text: T("pl.save") }),
      el("label", { for: "pl-d-out", text: T("pl.f.out") }), out,
      el("div", { class: "pl-acts", role: "group", "aria-label": T("pl.progress") },
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.start.btn"), onclick: () => mark(n.id, "in_progress") }),
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.done"), onclick: () => mark(n.id, "completed", out) }),
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.fail"), onclick: () => mark(n.id, "failed") }),
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.rewind"), onclick: () => rewind(n.id) }),
        removeButton(n))));
  }

  function removeButton(n) {
    const b = el("button", { type: "button", class: "btn pl-btn pl-danger", text: T("pl.remove") });
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
  async function saveNode(id, cond) {
    const changes = { title: document.getElementById("pl-d-title").value.trim(), description: document.getElementById("pl-d-desc").value,
      assignee: document.getElementById("pl-d-assignee").value };
    if (cond) {
      try { changes.conditions = JSON.parse(cond.value || "{}"); } catch (e) { say(T("pl.badjson"), true); return; }
    }
    if (!changes.title) { say(T("pl.f.title.required"), true); return; }
    await event({ type: "plan:update_node", node_id: id, changes }, T("pl.saved"));
  }
  async function mark(id, status, outBox) {
    let outputs;
    if (outBox && outBox.value.trim()) {
      try { outputs = JSON.parse(outBox.value); } catch (e) { say(T("pl.badjson"), true); return; }
      if (!outputs || typeof outputs !== "object" || Array.isArray(outputs)) { say(T("pl.badjson"), true); return; }
    }
    await event({ type: "plan:mark", node_id: id, status, ...(outputs ? { outputs } : {}) }, T("pl.marked"));
  }
  async function rewind(id) { await event({ type: "plan:rewind", node_id: id }, T("pl.rewound")); }
  async function userDiff(diff, title) {
    const p = await api(planPath("/events"), { type: "plan:propose_diff", title, source: "user", diff });
    if (!p.ok) { say(errText(p.data) || T("pl.failed"), true); return; }
    await event({ type: "plan:approve", proposal_id: p.data.proposal_id }, T("pl.ev.applied"));
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
        el("div", { class: "pl-chips" }, chips),
        el("div", { class: "pl-acts" },
          el("button", { type: "button", class: "btn primary pl-btn", text: T("pl.approve"),
            onclick: () => event({ type: "plan:approve", proposal_id: p.proposal_id }, T("pl.ev.applied")) }),
          el("button", { type: "button", class: "btn pl-btn", text: T("pl.reject"),
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
  function buildAdd() {
    ui.add.replaceChildren(el("summary", { text: T("pl.add") }));
    ui.addForm = el("form", { class: "pad pl-dform", onsubmit: addNode });
    ui.add.append(ui.addForm);
  }
  function drawAddForm() {
    if (!S.snap) { ui.addForm.replaceChildren(); return; }
    const nodes = Object.values(S.snap.plan.nodes);
    ui.addForm.replaceChildren(
      el("label", { for: "pl-a-title", text: T("pl.f.title") }), el("input", { id: "pl-a-title", required: true, maxlength: "200", autocomplete: "off" }),
      el("label", { for: "pl-a-desc", text: T("pl.f.desc") }), el("textarea", { id: "pl-a-desc", rows: "2" }),
      el("label", { for: "pl-a-type", text: T("pl.f.type") }), el("select", { id: "pl-a-type" }, TYPES.map(t => el("option", { value: t, text: T("pl.type." + t) }))),
      el("label", { for: "pl-a-assignee", text: T("pl.f.assignee") }),
      el("select", { id: "pl-a-assignee" }, ["agent", "user", "third_party"].map(a => el("option", { value: a, text: T("pl.assignee." + a) }))),
      nodes.length ? el("fieldset", { class: "pl-parents" }, el("legend", { text: T("pl.f.parents") }),
        nodes.map(n => el("label", { class: "pl-check" }, el("input", { type: "checkbox", name: "parent", value: n.id }), el("span", { text: n.title })))) : [],
      el("button", { class: "btn primary pl-btn", text: T("pl.add.btn") }));
  }
  async function addNode(e) {
    e.preventDefault();
    const f = ui.addForm;
    const title = f.querySelector("#pl-a-title").value.trim();
    if (!title) return;
    const id = "n_" + Math.random().toString(36).slice(2, 8);
    const parents = [...f.querySelectorAll("input[name=parent]:checked")].map(i => i.value);
    await userDiff({ add_nodes: [{ id, type: f.querySelector("#pl-a-type").value, title, description: f.querySelector("#pl-a-desc").value,
      assignee: f.querySelector("#pl-a-assignee").value, dependencies: parents, outputs: [] }] }, T("pl.add"));
    f.reset();
    S.selected = id;
    render();
  }

  async function createPlan(e) {
    e.preventDefault();
    const title = document.getElementById("pl-new-title").value.trim();
    if (!title) return;
    const slug = title.toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "").replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 40) || "plan";
    const plan_id = `${slug}_${Math.random().toString(36).slice(2, 6)}`;
    const r = await api("api/plans", { plan: { plan_id, title, target_type: "occasional" }, card: { titolo: title } });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    document.getElementById("pl-new-title").value = "";
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
    ui.horizon.replaceChildren(el("summary", { text: T("pl.hz.title") }),
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
    ui.manage.replaceChildren(el("summary", { text: T("pl.manage") }),
      el("form", { class: "pad pl-dform", onsubmit: saveMeta },
        el("p", { class: "help", text: T("pl.manage.help") }),
        field("pl-m-title", T("pl.f.title"), el("input", { id: "pl-m-title", value: p.title, maxlength: "200", required: true, autocomplete: "off" })),
        field("pl-m-kind", T("pl.f.kind"), el("select", { id: "pl-m-kind" },
          ["occasional", "constant"].map(k => el("option", { value: k, text: T("pl.kind." + k), selected: k === p.target_type })))),
        el("p", { class: "help", text: T("pl.kind.help") }),
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
        el("button", { type: "button", class: "btn pl-btn", text: T("pl.mermaid"), onclick: copyMermaid })),
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
    ui.archived.replaceChildren(el("summary", { text: T("pl.archives") }), ui.archivedBody);
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
    ui.triage.replaceChildren(el("summary", { text: T("pl.triage") }),
      el("form", { class: "pad pl-dform", onsubmit: runTriage },
        el("p", { class: "help", text: T("pl.triage.help") }),
        el("label", { for: "pl-t-events", text: T("pl.triage.events") }),
        el("textarea", { id: "pl-t-events", rows: "4", placeholder: T("pl.triage.ph") }),
        el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-t-replan", checked: true }), el("span", { text: T("pl.triage.replan") })),
        el("button", { class: "btn primary pl-btn", text: T("pl.triage.go") }), triageOut));
  }
  async function runTriage(e) {
    e.preventDefault();
    const events = document.getElementById("pl-t-events").value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!events.length || !S.id) return;
    const r = await api(planPath("/triage"), { events, replan: document.getElementById("pl-t-replan").checked });
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    showTriage(r.data);
  }
  function showTriage(r) {
    if (!triageOut) return;
    const lines = [
      el("p", { class: "pl-tverdict " + (r.wake_system_2 ? "wake" : "quiet"), text: r.wake_system_2 ? T("pl.t.wake") : T("pl.t.quiet") }),
      el("p", { text: T("pl.t.source." + r.source) + " · " + (r.sent_to_cloud ? T("pl.t.sent") : T("pl.t.local")) }),
      el("p", { text: T("pl.t.detail", Math.round(r.has_blocker_prob * 100), r.impact_scope, r.max_impact || "—") }),
    ];
    if (r.replanning) lines.push(el("p", { text: T("pl.t.replanning") }));
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
      el("p", { class: "pl-fact", text: s.key_present ? T("pl.set.key.yes") : T("pl.set.key.no") }),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-cloud", checked: s.cloud_triage, disabled: !s.key_present }), el("span", { text: T("pl.set.cloud") })),
      ...(s.cloud_triage ? [riskLabel(T("pl.risk.cloud"))] : []),
      el("label", { class: "pl-check" }, el("input", { type: "checkbox", id: "pl-s-pseudo", checked: s.pseudonymize }), el("span", { text: T("pl.set.pseudo") })),
      el("fieldset", { class: "pl-parents" }, el("legend", { text: T("pl.set.fields") }),
        FIELDS.map(f => [el("label", { class: "pl-check" }, el("input", { type: "checkbox", name: "field", value: f, checked: picked.includes(f) }), el("span", { text: T("pl.card." + f) })),
          personal.includes(f) ? el("p", { class: "help", text: T("pl.set.personal.help") }) : []]).flat()),
      ...(s.card_fields && personal.some(f => s.card_fields.includes(f)) ? [riskLabel(T("pl.risk.personal"))] : []),
      el("button", { class: "btn primary pl-btn", text: T("pl.save") }));
  }
  async function loadSettings() {
    if (!ui.setBody && !buildSettings()) return;
    const r = await api("api/planner/settings");
    S.settings = r.ok ? r.data : null;
    drawSettings();
  }
  async function saveSettings(e) {
    e.preventDefault();
    const fields = [...ui.setBody.querySelectorAll("input[name=field]:checked")].map(i => i.value);
    const standard = (S.settings && S.settings.card_fields_default) || FIELDS;
    const personal = (S.settings && S.settings.card_fields_personal) || [];
    const isStandard = fields.length === standard.length && standard.every(f => fields.includes(f));
    const body = { cloud_triage: ui.setBody.querySelector("#pl-s-cloud").checked, pseudonymize: ui.setBody.querySelector("#pl-s-pseudo").checked,
      card_fields: isStandard ? null : fields };
    if (body.cloud_triage && !(S.settings && S.settings.cloud_triage) && !(await riskConfirm([T("pl.risk.cloud")]))) { drawSettings(); return; }
    const hadPersonal = S.settings && S.settings.card_fields && personal.some(f => S.settings.card_fields.includes(f));
    if (fields.some(f => personal.includes(f)) && !hadPersonal && !(await riskConfirm([T("pl.risk.personal")]))) { drawSettings(); return; }
    const r = await api("api/planner/settings", body);
    if (!r.ok) { say(errText(r.data) || T("pl.failed"), true); return; }
    S.settings = r.data;
    drawSettings();
    say(T("pl.saved"));
  }

  /* ---- everything ---- */
  function render() {
    ui.empty.hidden = S.plans.length > 0;
    const has = !!S.snap;
    [ui.summary, ui.detail, ui.align, ui.proposals, ui.offers, ui.history, ui.add, ui.horizon, ui.manage, ui.triage].forEach(n => { n.hidden = !has; });
    document.querySelector(".pl-main").hidden = !has;
    if (!has) return;
    const nodes = Object.values(S.snap.plan.nodes);
    ui.summary.textContent = T("pl.summary", nodes.length, nodes.filter(n => n.status === "completed").length, S.snap.ready.length,
      Object.keys(S.snap.plan.shared_state).length);
    drawGraph();
    drawList();
    const editing = ui.detail.contains(document.activeElement) && document.activeElement.matches("input, textarea, select");
    if (!editing) drawDetail();
    drawProposals();
    drawOffers();
    drawHistory();
    drawAlign();
    drawHorizon();
    drawManage();
    if (!ui.addForm.contains(document.activeElement)) drawAddForm();
  }

  /* ---- start when the view is shown; stop the stream when it is left ---- */
  // the Chat links to a plan it touched: the id waits in the session storage until this view opens
  function wanted() {
    let id = "";
    try { id = sessionStorage.getItem("basalt.openplan") || ""; sessionStorage.removeItem("basalt.openplan"); } catch (e) { /* no link */ }
    return id;
  }
  async function enter() {
    if (!S.ready) { build(); S.ready = true; await loadList(false); } else await loadList(true);
    const id = wanted();
    if (id && id !== S.id && [...ui.select.options].some(o => o.value === id)) { ui.select.value = id; await choose(id); }
  }
  document.addEventListener("basalt:view", e => {
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
