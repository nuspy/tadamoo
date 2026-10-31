"use strict";
/* Chat on conversations: each conversation is a long-lived agent process on this server (api/agent/sessions). The page
   follows what happens by polling the conversation's events and shows, in order, what the agent wrote, thought and did
   (tools with their result, plans touched), the questions it puts with their buttons, and what it needs from the person.
   Everything the agent or a tool wrote enters the page through text nodes; the Markdown of its answers goes through the
   same safe renderer as the model-only chat. */
(() => {
  const MODE_KEY = "basalt.chatmode", LAST_KEYS = { chat: "basalt.chat.session", agent: "basalt.agent.session" }, PROJECT_KEY = "basalt.agent.project";
  const MODES = ["agent", "ask", "direct"];
  const FAST_MS = 350, SLOW_MS = 3000, SHOWN_OUTPUT = 1500, EXPLAIN_MAX = 300;
  /* what an action is called when the model gave no sentence for it: a phrase by tool name (ch.act.*), else a generic one */
  const ACTIONS = new Set(["shell_command", "write_file", "edit_file", "read_file", "list_directory", "search_code", "tree", "search_web", "browse_url", "run_code", "spawn_subagent",
    "browser_open", "read_attachment", "write_output_file", "find_online_capability"]);
  const explainOf = v => (typeof v === "string" ? v.replace(/\s+/g, " ").trim().slice(0, EXPLAIN_MAX) : "");   // the model's words: text only, cut short
  function actionText(b) {
    const said = explainOf(b.description), name = String(b.name || "");
    if (said) return said;
    const key = ACTIONS.has(name) ? name : name.startsWith("plan_") ? "plan" : name.startsWith("browser_") ? "browser" : "";
    return key ? T("ch.act." + key) : T("ch.act.generic", name);
  }
  const load = (k, d) => { try { const v = localStorage.getItem(k); return v == null ? d : v; } catch (e) { return d; } };
  const keep = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* this page only */ } };
  const mk = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (text, cls, onclick) => { const b = mk("button", "btn " + (cls || ""), text); b.type = "button"; b.onclick = onclick; return b; };

  /* a small icon button that copies a text; the icon turns into a tick for a moment */
  const SVGNS = "http://www.w3.org/2000/svg";
  function icon(paths) {
    const s = document.createElementNS(SVGNS, "svg");
    s.setAttribute("viewBox", "0 0 24 24"); s.setAttribute("aria-hidden", "true"); s.setAttribute("focusable", "false"); s.classList.add("aicon-svg");
    paths.forEach(d => { const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", d); s.append(p); });
    return s;
  }
  const COPY_ICON = ["M9 9h10v11H9z", "M5 15V4h10"], TICK_ICON = ["M5 12l5 5 9-10"], BRANCH_ICON = ["M6 4v10", "M6 14a4 4 0 0 0 4 4h4", "M18 4v6", "M6 4h0", "M18 10h0"];
  async function clipboardCopy(text) {
    try { await navigator.clipboard.writeText(text); return true; } catch (e) { /* no clipboard here: the old way */ }
    const t = document.createElement("textarea");
    t.value = text; t.style.position = "fixed"; t.style.opacity = "0"; document.body.append(t); t.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    t.remove();
    return ok;
  }
  function copyButton(textOf) {
    const b = document.createElement("button");
    b.type = "button"; b.className = "aicon"; b.title = T("ch.copy"); b.setAttribute("aria-label", T("ch.copy"));
    b.append(icon(COPY_ICON));
    let timer = 0;
    b.onclick = async ev => {
      ev.stopPropagation();
      const text = textOf();
      if (!text) return;
      const ok = await clipboardCopy(text);
      status(T(ok ? "ch.copied" : "ch.copy.fail"));
      if (!ok) return;
      b.replaceChildren(icon(TICK_ICON)); b.classList.add("done");
      clearTimeout(timer);
      timer = setTimeout(() => { b.replaceChildren(icon(COPY_ICON)); b.classList.remove("done"); }, 1500);
    };
    return b;
  }

  let mode = load(MODE_KEY, "agent");
  if (!MODES.includes(mode)) mode = "agent";
  const inAgent = () => document.body.dataset.view === "agent";           // the Agent tab is this page, working in a folder
  const lastKey = () => LAST_KEYS[inAgent() ? "agent" : "chat"];
  let projects = [], curProject = load(PROJECT_KEY, ""), defaultWs = "", lastCtx = null;
  const sameDir = (a, b) => String(a || "").replace(/[\\/]+$/, "").toLowerCase() === String(b || "").replace(/[\\/]+$/, "").toLowerCase();
  const isChatSession = x => !x.workspace || (defaultWs && sameDir(x.workspace, defaultWs));
  const inContext = x => (inAgent() ? !isChatSession(x) : isChatSession(x));
  const curPath = () => (projects.some(x => sameDir(x.path, curProject)) ? curProject : "");
  let supported = null;                               // whether this server offers conversations
  let sid = "", after = 0, state = "dormant", rec = null, timer = 0, items = [], current = null, busyNow = false, replaying = false;
  const status = t => { $("#chat-status").textContent = t; };
  const scroller = () => (box.scrollHeight > box.clientHeight + 1 && getComputedStyle(box).overflowY !== "visible") ? box : document.scrollingElement;
  function scrollDown(force) { const s = scroller(); if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 160) s.scrollTop = s.scrollHeight; }

  const box = $("#amsgs"), empty = $("#achat-empty"), modeSel = $("#chatmode");
  let pick = null, learn = null;                                // making a skill: the requests being chosen, and where a recording began
  const bar = $("#pskill-bar"), badge = $("#learn-badge");

  async function api(method, path, body) {
    try {
      const r = await fetch("api/agent/sessions" + path, { method, cache: "no-store",
        headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...authHeaders() }, body: body ? JSON.stringify(body) : undefined });
      let j = {};
      try { j = await r.json(); } catch (e) { /* no body */ }
      return { ok: r.ok, status: r.status, json: j };
    } catch (e) { return { ok: false, status: 0, json: {} }; }
  }
  const errText = r => (r.json && r.json.error && r.json.error.message) || `HTTP ${r.status}`;

  /* ---- the mode: conversations (with tools or question only) or the model-only chat of chat.js ---- */
  const active = () => mode !== "direct" && supported !== false;
  function applyMode() {
    const on = active();
    $("#msgs").hidden = on;
    box.hidden = !on;
    $("#convs").hidden = !on;
    $("#ctx-meter").hidden = on;
    $("#attach").hidden = on ? false : $("#attach").dataset.can !== "1";
    const attachName = T(on ? "ch.att.btn" : "ch.att.img");
    $("#attach").setAttribute("aria-label", attachName); $("#attach").title = attachName;
    $("#branches-open").hidden = !on;
    $("#pskills-open").hidden = !on;
    $("#ps-chat").hidden = !on;                                               // the model-only chat has no look at a request
    $("#po-chat").hidden = !on;                                               // and no rewrite of it
    if (!on) $("#ps-chat-panel").hidden = true;
    $("#chatmode-label").hidden = inAgent();
    modeSel.title = T("ch.mode." + mode + ".d");                              // what the kind of conversation is, as a tooltip
    drawPend();
    if (on) { $("#chat-hint").textContent = T("ch.hint"); poll(true); } else { clearTimeout(timer); $("#chat-hint").textContent = T("ch.hint.direct"); }
    setControls();
  }
  function paintFolder() {
    const agent = inAgent(), chipEl = $("#folder-chip");
    const p = projects.find(x => sameDir(x.path, rec && rec.workspace ? rec.workspace : curProject));
    chipEl.hidden = !(agent && p);
    if (!chipEl.hidden) { chipEl.textContent = T("ag.folder.chip", p.name); chipEl.title = p.path; }
    $("#chg-open").hidden = !agent;
  }
  function setControls() {
    paintFolder();
    paintChip();
    paintLive();
    if (window.codingUi) window.codingUi.refresh();
    const on = active();
    if (!on) return;
    const asking = state === "asking";
    const bp = chatSettings.patterns().branches;
    $("#branch-chat").hidden = !(sid && bp && bp.on);
    $("#stop").hidden = !(busyNow || state === "busy" || state === "starting" || asking);
    $("#send").disabled = false;
    $("#prompt").placeholder = asking ? T("ch.hint.asking") : busyNow ? T("ch.ph.busy") : T("ch.ph");
    paintCopy();
  }
  modeSel.value = mode;
  modeSel.onchange = () => { mode = modeSel.value; keep(MODE_KEY, mode); if (sid) forget(); applyMode(); };

  /* ---- the column of conversations: the earlier ones to open again or delete, the favourites kept apart and above the rest ---- */
  const convs = $("#convs"), CV_KEY = "basalt.chat.convs";
  let known = [];                                                              // what the server lists, newest first
  const when = ts => (ts ? new Date(ts * 1000).toLocaleString(LANG === "it" ? "it-IT" : "en-GB", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "");
  function setColumn(open) {
    convs.classList.toggle("shut", !open);
    const t = $("#cv-toggle");
    t.setAttribute("aria-expanded", String(open));
    t.textContent = open ? "\u2039" : "\u203A";                                 // an arrow to the left closes it, one to the right opens it
    t.title = T(open ? "cv.shut" : "cv.open");
    t.setAttribute("aria-label", t.title);
  }
  setColumn(load(CV_KEY, "1") !== "0");
  $("#cv-toggle").onclick = () => { const open = convs.classList.contains("shut"); setColumn(open); keep(CV_KEY, open ? "1" : "0"); };
  function convRow(s) {
    const name = s.title || s.id, li = mk("li", "cv");
    if (s.id === sid) li.setAttribute("aria-current", "true");
    const open = mk("button", "cv-open");
    open.type = "button";
    open.append(mk("span", "cv-t", name), mk("span", "cv-s", `${T("ch.state." + s.state)} \u00B7 ${when(s.updated)}`));
    open.onclick = () => { if (s.id !== sid) openSession(s.id); };
    const star = mk("button", "cv-fav", s.favorite ? "\u2605" : "\u2606");
    star.type = "button";
    star.setAttribute("aria-pressed", String(!!s.favorite));
    star.title = T(s.favorite ? "cv.unstar" : "cv.star");
    star.setAttribute("aria-label", `${star.title}: ${name}`);
    star.onclick = async () => {
      const r = await api("POST", `/${s.id}/favorite`, { favorite: !s.favorite });
      if (!r.ok) status(errText(r));
      await refreshList();
    };
    const del = mk("button", "cv-del", "\u2715");
    del.type = "button";
    del.title = T("cv.del");
    del.setAttribute("aria-label", `${T("cv.del")}: ${name}`);
    del.onclick = async () => {
      const r = await api("POST", `/${s.id}/delete`);
      if (!r.ok) { status(errText(r)); await refreshList(); return; }
      if (s.id === sid) { forget(); setControls(); }
      await refreshList();
      status(T("cv.deleted"));
    };
    li.append(open, star, del);
    return li;
  }
  /* the Agent tab's column: the folders added, each with the conversations held in it */
  function setProject(path) { curProject = path; keep(PROJECT_KEY, path); }
  async function addFolder() {
    const p = await window.agentProjects.open();
    if (!p) return null;
    projects = window.agentProjects.list();
    setProject(p.path);
    paintList(); setControls();
    return p;
  }
  function projectGroup(p) {
    const group = mk("div", "cv-group cv-proj"), head = mk("div", "cv-projhead" + (sameDir(p.path, curProject) ? " here" : ""));
    const name = mk("button", "cv-projname", p.name);
    name.type = "button"; name.title = p.path;
    name.onclick = () => { setProject(p.path); paintList(); setControls(); };
    const plus = mk("button", "cv-act", "+"), gone = mk("button", "cv-act", "\u2715");
    plus.type = gone.type = "button";
    plus.title = T("ag.proj.new"); plus.setAttribute("aria-label", `${plus.title}: ${p.name}`);
    plus.onclick = () => { setProject(p.path); newChat(); $("#prompt").focus(); };
    gone.title = T("ag.proj.remove"); gone.setAttribute("aria-label", `${gone.title}: ${p.name}`);
    gone.onclick = async () => { await window.agentProjects.remove(p.path); projects = window.agentProjects.list(); if (sameDir(p.path, curProject)) setProject(""); paintList(); setControls(); };
    head.append(name, plus, gone);
    const mine = known.filter(x => x.state !== "closed" && inContext(x) && sameDir(x.workspace, p.path));
    const ul = mk("ul", "cv-list");
    ul.replaceChildren(...[...mine.filter(x => x.favorite), ...mine.filter(x => !x.favorite)].map(convRow));
    group.append(head, ul);
    if (!p.exists) group.append(mk("p", "cv-empty", T("ag.proj.missing")));
    else if (!mine.length) group.append(mk("p", "cv-empty", T("cv.empty")));
    return group;
  }
  function paintList() {
    const agent = inAgent();
    $("#cv-projects").hidden = !agent;
    $("#cv-chatlists").hidden = agent;
    $("#cv-title").textContent = T(agent ? "ag.projects.title" : "cv.title");
    if (agent) {
      $("#cv-proj-empty").hidden = projects.length > 0;
      $("#cv-proj-list").replaceChildren(...projects.map(projectGroup));
      return;
    }
    const shown = known.filter(s => s.state !== "closed" && inContext(s));
    const fav = shown.filter(s => s.favorite), rest = shown.filter(s => !s.favorite);
    $("#cv-fav-group").hidden = !fav.length;
    $("#cv-fav").replaceChildren(...fav.map(convRow));
    $("#cv-recent").replaceChildren(...rest.map(convRow));
    $("#cv-empty").hidden = shown.length > 0;
  }
  async function refreshList() {
    const r = await api("GET", "");
    if (!r.ok) { supported = r.status === 404 || r.status === 0 ? false : supported; return; }
    supported = true;
    known = r.json.sessions || [];
    defaultWs = r.json.default_workspace || "";
    paintList();
  }
  async function branch(at) {
    const h = await api("GET", `/${sid}/history`);
    const turn = Number.isInteger(at) ? at : (h.ok ? h.json.messages.filter(m => m.role === "user").length : 0);
    if (!turn) { status(T("ch.branch.none")); return; }
    const r = await api("POST", `/${sid}/branch`, { turn });
    if (!r.ok) { status(errText(r)); return; }
    await refreshList();
    await openSession(r.json.session.id);
    status(T("ch.branch.done"));
  }
  $("#branch-chat").onclick = () => branch();
  document.addEventListener("basalt:chatset", () => setControls());
  /* the steps one answer may take and the end-of-turn bias are read from the panel when a conversation starts; a change made in the panel
     afterwards is sent to the open conversation (it is compared with the panel's own earlier value, not with the conversation's) */
  let panelWas = JSON.stringify(chatSettings.live()), panelTimer = 0;
  document.addEventListener("basalt:chatset-settled", () => { panelWas = JSON.stringify(chatSettings.live()); });
  function paintLive() {
    const el = $("#cs-live"), on = active() && !!sid && !!rec && rec.max_turns !== undefined;
    if (!el) return;
    el.hidden = !on;
    if (on) el.textContent = T("cs.live", rec.max_turns > 0 ? rec.max_turns : T("cs.live.none"), rec.end_turn_bias || 0);
  }
  document.addEventListener("basalt:chatset", () => {
    clearTimeout(panelTimer);
    panelTimer = setTimeout(async () => {
      const now = chatSettings.live(), before = JSON.parse(panelWas);
      panelWas = JSON.stringify(now);
      if (!active() || !sid) return;
      const changed = Object.fromEntries(Object.entries(now).filter(([k, v]) => before[k] !== v));
      if (!Object.keys(changed).length) return;
      const r = await api("POST", `/${sid}/settings`, changed);
      if (r.ok) { rec = r.json.session; paintLive(); } else status(errText(r));
    }, 600);
  });

  function forget() { pick = null; learn = null; paintPick(); paintLearn(); pend = []; sid = ""; after = 0; state = "dormant"; rec = null; items = []; current = null; Object.keys(byTurn).forEach(k => delete byTurn[k]); box.querySelectorAll(".cmsg, .anote, .ablocked, .asuggest").forEach(e => e.remove()); chipsEl = null; chipTexts = []; empty.hidden = false; keep(lastKey(), ""); copyShown = false; }
  function newChat() {
    if (inAgent() && !curPath()) { addFolder(); return; }                      // a conversation in the Agent tab needs a folder to work in
    forget(); status(T("ch.new")); setControls(); paintList();
  }
  async function openSession(id) {
    forget();
    const h = await api("GET", `/${id}/history`);
    if (!h.ok) { status(errText(h)); return; }
    sid = id; keep(lastKey(), id); rec = h.json.session; state = rec.state;
    if (inAgent() && rec.workspace && projects.some(x => sameDir(x.path, rec.workspace))) setProject(rec.workspace);
    if (typeof rec.effort === "string") effortSel.value = rec.effort;
    fromHistory(h.json.messages || []);
    if (state === "busy" || state === "starting" || state === "asking") {      // mid-turn: rebuild what is going on from its events
      forget(); sid = id;
      const e = await api("GET", `/${id}/events?after=0`);
      if (e.ok) { replaying = true; try { e.json.events.forEach(apply); } finally { replaying = false; } after = e.json.next; state = e.json.state; }
    } else {
      const e = await api("GET", `/${id}/events?after=0`);
      if (e.ok) after = e.json.next;
    }
    showPending(); showQuestion(); if (rec && rec.suggestions && state === "idle") showChips(rec.suggestions);
    syncRecord(); render(); paintList(); poll(true);
  }

  /* ---- the view model: the person's messages and, for each answer, blocks in the order things happened ---- */
  const byTurn = {};                                  // the answer of each task: tasks can take turns, and come back
  /* the time of what is being added, in seconds: the event's or the saved message's while those are applied (null when not known), else now */
  let stampAt;
  const stampOf = () => (stampAt === undefined ? Date.now() / 1000 : stampAt);
  function userItem(text, files) { const it = { kind: "user", text, files: files || [], ts: stampOf() }; items.push(it); return it; }
  function assistant(turn) {
    if (turn && byTurn[turn]) { current = byTurn[turn]; return current; }
    if (!turn && current && !current.done) return current;
    current = { kind: "assistant", blocks: [], done: false, meta: "", turn };
    if (turn) byTurn[turn] = current;
    items.push(current);
    return current;
  }
  function noteItem(it) { items.push({ kind: "note", ts: stampOf(), ...it }); }
  /* Optimize prompt (harness/optimize.py): under the person's message, what the model read in its place, or why the rewrite was not used */
  const OPT_WHY = ["lost", "ratio", "answer", "leak", "cut", "empty"];
  function optimizedNote(state, reason, detail, text, original, turn) {
    const waiting = (byTurn[turn] ? byTurn[turn].blocks : []).find(b => b.wait && !b.gone);
    if (waiting) { waiting.gone = true; waiting.dirty = true; }                  // the line "rewriting the prompt" ends with the call
    if (state === "skipped" && reason === "stopped") addBlock({ t: "note", s: T("ch.opt.stopped") }, turn);
    if (state === "sent") addBlock({ t: "opt", text: String(text || ""), original: String(original || "") }, turn);
    else if (state === "rejected") addBlock({ t: "note", s: T("ch.opt.rejected." + (OPT_WHY.includes(reason) ? reason : "empty"), String(detail || "")) }, turn);
  }
  /* a reply the output limit cut is asked to go on (harness/agent_loop.py): its words join the text already shown, with no doubled space and
     none of the words the model wrote again. They are held back for a moment so the start can be compared with the end of the text. */
  function joinPending(a, force) {
    if (!a.joinTo || (!force && a.joinBuf.length < 160)) return;
    a.joinTo.s = cutText.joinCut(a.joinHead, a.joinBuf); a.joinTo.dirty = true;
    if (force) { a.joinTo = null; a.joinBuf = ""; }
  }
  function textBlock(kind, text, turn) {
    const a = assistant(turn), last = a.blocks[a.blocks.length - 1];
    if (kind === "text" && a.joinTo) { a.joinBuf += text; joinPending(a, false); return; }
    if (last && last.t === kind) { last.s += text; last.dirty = true; return; }
    if (last && last.t === "reasoning") last.over = true;
    a.blocks.push({ t: kind, s: text, dirty: true, ts: stampOf() });
  }
  function addBlock(b, turn) { const a = assistant(turn); const last = a.blocks[a.blocks.length - 1]; if (last && last.t === "reasoning") last.over = true; a.blocks.push({ ...b, dirty: true, ts: stampOf() }); return a.blocks[a.blocks.length - 1]; }
  const toolBlock = id => { for (const it of items) { const b = (it.blocks || []).find(x => x.t === "tool" && x.id === id); if (b) return b; } return null; };

  function apply(e) {
    stampAt = typeof e.t === "number" ? e.t : undefined;
    try { applyEvent(e); } finally { stampAt = undefined; }
  }
  function applyEvent(e) {
    switch (e.event) {
      case "user": closeQuestions(); clearChips(); userItem(e.text, e.attachments); break;
      case "accepted": clearChips(); assistant(e.turn); started(e.turn); break;
      case "intake": {                                                         // what the harness assumed or split before the agent began (harness/intake.py)
        const lines = [];
        if ((e.assumptions || []).length) lines.push(T("ch.intake.assumed", e.assumptions.join("; ")));
        if ((e.subtasks || []).length) lines.push(T("ch.intake.parts", e.subtasks.length, e.subtasks.join("; ")));
        if ((e.parked || []).length) lines.push(T("ch.intake.parked", e.parked.join("; ")));
        if (lines.length) addBlock({ t: "note", s: lines.join(" ") }, e.turn);
        break;
      }
      case "optimizing": addBlock({ t: "note", s: T("ch.opt.working"), wait: true }, e.turn); break;
      case "optimized": optimizedNote(e.state, e.reason, e.detail, e.text, e.original, e.turn); break;
      case "suggestions": if (!replaying) showChips(e.items); break;
      case "started": assistant(e.turn); started(e.turn); break;
      case "woke": noteItem({ s: T("ch.woke", String(e.text || "").replace(/^\[planner\]\s*/, "").slice(0, 200)), route: e.turn }); assistant(e.turn); started(e.turn); break;
      case "delta": textBlock(e.kind === "reasoning" ? "reasoning" : "text", e.text || "", e.turn); break;
      case "queued": break;                                                    // the person's own bubble is already there
      case "routed": noteItem({ s: T("ch.routed." + e.how, e.position || 0), route: e.turn, how: e.how }); break;
      case "payment": addBlock({ t: "note", s: T("ch.payment." + (e.mode === "confirmed" ? "confirmed" : "waiver"), e.amount, e.recipient, e.site || "-", e.remaining || "-") }, e.turn); break;
      case "suspended": noteItem({ s: T("ch.suspended") }); break;
      case "resumed": noteItem({ s: T("ch.resumed") }); assistant(e.turn); break;
      case "cleared": noteItem({ s: T("ch.cleared", e.count) }); break;
      case "mode": if (rec) rec.mode = e.mode; paintChip(); noteItem({ s: T("ch.mode.now", T("ag.mode." + e.mode)) }); break;
      case "role": if (rec) rec.role = e.role; noteItem({ s: T("cd.role.now", T("cd.role." + e.role)) }); setControls(); break;
      case "checks": noteItem({ s: T("cd.checks.now", e.safe) }); break;
      case "effort": if (rec) rec.effort = e.level; noteItem({ s: T("ch.effort.now", effortName(e.level)) }); break;
      case "settings":
        if (rec && "max_turns" in e) rec.max_turns = e.max_turns;
        if (rec && "end_turn_bias" in e) rec.end_turn_bias = e.end_turn_bias;
        if ("max_turns" in e) noteItem({ s: T("ch.settings.steps", e.max_turns > 0 ? e.max_turns : T("cs.live.none")) });
        if ("end_turn_bias" in e) noteItem({ s: T("ch.settings.bias", e.end_turn_bias || 0) });
        paintLive();
        break;
      case "command": document.dispatchEvent(new CustomEvent("basalt:command", { detail: e })); break;
      case "target": {                                                         // the Target Loop (target.js) tells how it goes
        const n = e.what, args = { planned: [e.criteria, e.tasks], task: [e.n, e.of, e.title], pass: [e.n, e.achieved, e.total], fix: [e.tasks], approach: [e.tasks],
          achieved: [e.achieved], blocked: [e.achieved, e.total], stopped: [e.achieved, e.total] }[n];
        if (args) noteItem({ s: T("tg.note." + n, ...args) });
        document.dispatchEvent(new CustomEvent("basalt:target", { detail: e }));
        break;
      }
      case "dropped": started(e.turn); noteItem({ s: T("ch.dropped") }); break;
      case "agent": {
        const d = e.data || {};
        if (d.type === "answer_truncated") {                                    // the output limit cut the reply: it is asked to go on, or delivered as it is
          const a = assistant(e.turn);
          a.contNote = d.final ? null : { n: d.continuation, of: d.of };
          const last = a.blocks.filter(b => b.t === "text").pop();
          if (last && !d.final) { a.joinTo = last; a.joinHead = last.s; a.joinBuf = ""; }
        } else if (d.type === "assistant") {                                    // the whole message; the pieces came before it
          const a = assistant(e.turn);
          if (a.joinTo) {
            if (!a.joinBuf && d.content) a.joinBuf = d.content;
            joinPending(a, true);
          } else if (!a.blocks.some(b => b.t === "text" || b.t === "reasoning") && (d.content || d.reasoning)) {
            if (d.reasoning) textBlock("reasoning", d.reasoning, e.turn);
            if (d.content) textBlock("text", d.content, e.turn);
          }
        } else if (d.type === "tool_start") {
          if (d.name === "ask_user") break;                                    // its question is shown by the question itself
          addBlock({ t: "tool", id: d.call_id, name: d.name, args: d.arguments || {}, status: "running", output: "", description: explainOf(d.description) }, e.turn);
        } else if (d.type === "calls_skipped") {                                 // repeats and calls over the limit are not run: said once, not once for each
          addBlock({ t: "note", s: T("ch.calls.skipped", (d.repeated || 0) + (d.surplus || 0)) }, e.turn);
        } else if (d.type === "verify") {                                        // the answer measured against what was asked
          const list = (d.violations || []).join(" ");
          addBlock({ t: "note", s: d.ok ? T("ch.verify.ok", (d.constraints || []).length) : d.retry ? T("ch.verify.retry", list) : T("ch.verify.unmet", list) }, e.turn);
        } else if (d.type === "compaction") {                                            // the older steps were summarised: what the server reads again
          addBlock({ t: "note", s: T("ch.ev.compaction", d.tokens_before, d.tokens_after, d.prefix.reread_tokens) }, e.turn);
        } else if (d.type === "compaction_cache") {
          addBlock({ t: "note", s: T("ch.ev.compaction_cache", d.prompt_tokens, d.cached_tokens) }, e.turn);
        } else if (d.type === "gate" || d.type === "budget_warning" || d.type === "budget_grace" || d.type === "stop_denied") {      // the checks before the end, and the limits: one line each
          addBlock({ t: "note", s: T(d.type === "gate" ? "ch.gate." + d.decision : "ch.ev." + d.type, d.round, d.of) }, e.turn);
        } else if (d.type === "long_answer") {                                    // the long answer the harness writes: one line that follows it (serve/longflow.py)
          longNote(e.turn, d);
        } else if (d.type === "tool_end") {
          if (d.name === "ask_user") break;
          const b = toolBlock(d.call_id) || addBlock({ t: "tool", id: d.call_id, name: d.name, args: {}, status: "running", output: "" }, e.turn);
          b.status = d.status === "completed" ? "ok" : "failed"; b.decision = d.decision; b.output = String(d.output || "").slice(0, SHOWN_OUTPUT); b.dirty = true;
        }
        break;
      }
      case "ask": addBlock({ t: "ask", id: e.id, mode: e.mode, kind: e.kind || (e.facts || {}).kind, question: e.question, options: e.options || [], facts: e.facts || {}, tool: e.tool, args: e.arguments, explain: explainOf(e.explain),
        intro: e.intro || "", questions: e.questions || [], fresh: !replaying, answered: null }, e.turn); break;
      case "answered": { for (const it of items) { const b = (it.blocks || []).find(x => x.t === "ask" && x.id === e.ask); if (b) { b.answered = e.value; b.dirty = true; } } break; }
      case "retrying": addBlock({ t: "note", s: T("ch.retrying", e.attempt, Math.round(e.wait_s)) }, e.turn); break;
      case "restarting": addBlock({ t: "note", s: T("ch.restarting", e.attempt, Math.round(e.wait_s || 0)) }); break;
      case "pending": addPending(e); break;
      case "pending_done": { for (const it of items) { const b = (it.blocks || []).find(x => x.t === "ask" && x.id === e.id); if (b && b.answered === null) { b.answered = e.value || "-"; b.dirty = true; } } break; }
      case "stalled": addBlock({ t: "note", s: T("ch.stalled") }); break;
      case "blocked": addBlock({ t: "blocked", reason: e.reason, detail: e.detail, options: e.options || [] }, e.turn); break;
      case "done": {
        const a = assistant(e.turn); a.done = true; a.stop = e.stop_reason; a.steps = e.max_turns; a.cut = !!e.cut;
        if (a.joinTo) joinPending(a, true);
        a.contNote = null;
        a.meta = e.stop_reason === "cancelled" ? T("ch.cancelled") : e.stop_reason === "asked" ? "" : e.error ? e.error : e.stop_reason === "error" ? T("ch.stop.error") : "";
        a.blocks.forEach(b => { if (b.t === "reasoning") b.over = true; });
        if (copyShown) loadCopy();
        break;
      }
      default: break;
    }
  }
  function longNote(turn, d) {
    const a = assistant(turn);
    if (d.event === "revised") {                                                // the corrected text replaces the text shown while the parts were written
      const t = a.blocks.filter(x => x.t === "text").pop();
      if (t) { t.s = d.text; t.dirty = true; }
      return;
    }
    const why = r => T(["not_found", "ambiguous", "too_short", "spans_blocks", "protected", "heading", "table", "limit"].includes(r) ? "la.r." + r
      : r === "rejected_length" ? "la.r.length" : String(r).startsWith("rejected") ? "la.r.rejected" : "la.r.invalid");
    const text = d.event === "estimate" ? T(d.long ? "la.p.estimate" : "la.p.short", d.words) : d.event === "plan" ? T("la.p.plan", d.segments.length)
      : d.event === "segment_start" ? T("la.p.part", d.index, d.of) : d.event === "segment_progress" ? T("la.p.words", d.index, d.words)
      : d.event === "segment_done" ? T("la.p.done", d.index, d.of, d.words) : d.event === "review" ? T("la.p.review", d.round, d.of)
      : d.event === "fix" ? (d.status === "applied" ? T("la.p.fix.ok", d.n, d.of, d.chunk) : T("la.p.fix.no", d.n, d.of, d.chunk, why(d.reason)))
      : d.event === "done" ? T("la.summary", d.words, d.target_words, d.segments, d.of)
        + (d.applied || (d.unapplied || []).length ? " " + T("la.fixes", d.applied || 0, (d.unapplied || []).length) : "")
        + ((d.warnings || []).length ? " " + T("la.warn", d.warnings.length, d.warnings.slice(0, 6).map(w => `${w.segment}: ${w.code}`).join("; ")) : "") : "";
    if (!text) return;
    const b = a.blocks.find(x => x.t === "note" && x.long);
    if (b) { b.s = text; b.dirty = true; } else addBlock({ t: "note", s: text, long: true }, turn);
  }
  function started(turn) {                                                      // a waiting request has begun (or was taken out): no more buttons
    items.forEach(it => { if (it.kind === "note" && it.route === turn && !it.over) { it.over = true; it.dirty = true; } });
  }
  /* a question the agent left for the person while it went on with other work: shown like any confirmation, answerable whenever */
  function addPending(e) {
    if (items.some(it => (it.blocks || []).some(x => x.t === "ask" && x.id === e.id))) return;
    const b = addBlock({ t: "ask", id: e.id, mode: "confirm", pending: true, source: e.source || {}, kind: e.kind || (e.facts || {}).kind, question: e.question, options: e.options || [], facts: e.facts || {}, tool: e.tool, args: e.arguments, explain: explainOf(e.explain), answered: null }, e.turn);
    if (current) current.done = true;
    return b;
  }
  function showPending() { ((rec && rec.pending) || []).forEach(p => addPending({ ...p, arguments: p.arguments })); }
  /* a card the agent put before the page was opened: still waiting for the person's answer */
  function showQuestion() {
    const q = rec && rec.question;
    if (!q || q.kind !== "questions" || !(q.questions || []).length || items.some(it => (it.blocks || []).some(x => x.t === "ask" && x.id === q.id))) return;
    addBlock({ t: "ask", id: q.id, mode: "question", kind: "questions", question: q.question, options: [], facts: {}, intro: q.intro || "", questions: q.questions, fresh: false, answered: null });
    if (current) current.done = true;
  }

  /* ---- the next-message chips: they put a text in the message box and never send ---- */
  let chipsEl = null, chipTexts = [];
  function clearChips() { if (chipsEl) chipsEl.remove(); chipsEl = null; chipTexts = []; }
  function showChips(list) {
    clearChips();
    const chips = (Array.isArray(list) ? list : []).filter(c => c && typeof c.prompt === "string" && c.prompt.trim()).slice(0, 4);
    if (!chips.length) return;
    chipsEl = mk("div", "asuggest");
    chipsEl.setAttribute("role", "group"); chipsEl.setAttribute("aria-label", T("ch.suggest.label"));
    chipTexts = chips.map(c => c.prompt);
    chips.forEach(c => {
      const b = btn(c.prompt, "ghost achip-s", () => { window.chatUi.setPrompt(c.prompt); });
      b.title = T("ch.suggest.tip", c.prompt); b.setAttribute("aria-label", T("ch.suggest.use", c.prompt));
      chipsEl.append(b);
    });
    const hide = btn("\u00d7", "ghost achip-x", clearChips);
    hide.title = T("ch.suggest.dismiss"); hide.setAttribute("aria-label", T("ch.suggest.dismiss"));
    chipsEl.append(hide);
    box.append(chipsEl);
    scrollDown();
  }
  document.addEventListener("input", ev => {                                       // typing something unrelated takes the chips away
    if (!chipsEl || !ev.target || ev.target.id !== "prompt") return;
    const t = ev.target.value.trim();
    if (t && !chipTexts.some(c => c.startsWith(t) || t.startsWith(c.slice(0, 20)))) clearChips();
  });
  function closeQuestions() { items.forEach(it => (it.blocks || []).forEach(b => { if (b.t === "ask" && b.mode === "question" && !b.answered) { b.answered = ""; b.dirty = true; } })); }

  function fromHistory(messages) {
    for (const m of messages) {
      stampAt = typeof m.ts === "number" ? m.ts : null;
      if (m.role === "user") {
        const text = typeof m.content === "string" ? m.content : "";
        if (text.startsWith("[harness]")) { assistant(); addBlock({ t: "note", s: text.replace(/^\[harness\]\s*/, "") }); } else {
          userItem(typeof m.raw === "string" ? m.raw : text, (m.attachments || []).map(n => ({ name: n })));
          if (m.opt && typeof m.opt === "object") { assistant(); optimizedNote(m.opt.state, m.opt.reason, m.opt.detail, text, m.raw); }
        }
      } else if (m.role === "assistant") {
        if (m.content) textBlock("text", m.content);
        (m.tool_calls || []).forEach(tc => {
          let args = {}; try { args = JSON.parse((tc.function || {}).arguments || "{}"); } catch (e) { /* shown as is */ }
          if ((tc.function || {}).name === "ask_user") return;
          addBlock({ t: "tool", id: tc.id, name: (tc.function || {}).name, args, status: "ok", output: "" });
        });
        if (current) current.done = true;
      } else if (m.role === "tool") {
        const b = toolBlock(m.tool_call_id) || (items.length ? items.flatMap(i => i.blocks || []).find(x => x.t === "tool" && x.id === m.tool_call_id) : null);
        if (b && m.skipped) {                                                   // a call that was not run: not a card of its own, but counted in one note
          items.forEach(it => { const at = (it.blocks || []).indexOf(b); if (at >= 0) { it.blocks.splice(at, 1); it.skipped = (it.skipped || 0) + 1; } });
        } else if (b) {
          b.output = String(m.content || "").slice(0, SHOWN_OUTPUT); b.description = explainOf(m.explain); b.dirty = true;
          if (b.description && b.args) ["description", "explain"].forEach(k => { if (b.args[k] === m.explain) delete b.args[k]; });   // shown above, not as an argument
        }
      }
    }
    items.forEach(it => { if (it.skipped) { it.blocks.push({ t: "note", s: T("ch.calls.skipped", it.skipped), dirty: true, ts: stampOf() }); it.skipped = 0; } });
    stampAt = undefined;
    current = null;
  }

  /* ---- drawing ---- */
  const planIdOf = b => (b.args && b.args.plan_id) || ((b.output.match(/Plan ([A-Za-z0-9_-]+):/) || [])[1]) || "";
  function openPlan(id) {
    try { sessionStorage.setItem("basalt.openplan", id); } catch (e) { /* the view opens anyway */ }
    location.hash = "plans";
  }
  function askQuestion(b) {                                                    // the harness's question, in the language of the page
    const f = b.facts || {}, key = "ch.q." + b.kind;
    if (b.kind === "plan_create") return T(key, f.title, (f.steps || []).length);
    if (b.kind === "plan_mark") return T(key, f.plan, f.step, f.to);
    if (b.kind === "plan_change") return T(key, f.plan, f.title);
    if (b.kind === "plan_archive") return T(key, f.plan);
    if (b.kind === "permission") return T(key, b.tool, JSON.stringify(b.args || {}).slice(0, 200));
    if (b.kind === "code") return T(key, f.who);
    if (b.kind === "plan_approval") return T(key, f.mode, f.plan) + (f.outside_content ? "\n\n" + T("ch.q.plan_outside") : "");
    if (b.kind === "payment") return T(key, f.amount, f.recipient, f.reason, f.system);
    if (b.kind === "capability_choice") return T(key, f.need);
    if (b.kind === "mcp_elicitation") return T(key, f.server);
    return b.question;
  }
  const capLabel = o => o.value.startsWith("install:") ? T("ch.cap.install", o.value.slice(8)) : o.value === "cancel" ? T("ch.cap.no") : o.value.startsWith("other:") ? T("ch.cap.said", o.value.slice(6)) : T("ch.cap.other");
  const capNote = (pre, n) => { const t = T(pre + n.code, ...(n.args || [])); return t === pre + n.code ? n.text : t; };
  function capCandidates(f) {                                                  // the facts of each candidate, as text lines: the source's words are never markup
    const ul = mk("ul", "acap-list");
    (f.candidates || []).forEach((c, i) => {
      const li = mk("li", "acap");
      li.append(mk("b", "", T("ch.cap.line", i + 1, c.name, T("cp.kind." + c.kind), c.source, c.version, c.score)));
      if (c.below_min) li.append(mk("span", "acap-low", " " + T("ch.cap.below", f.min_score)));
      if (c.facts.length) li.append(mk("div", "", c.facts.map(x => capNote("ch.cap.f.", x)).join(" \u00b7 ")));
      if (c.description) li.append(mk("div", "aask-h", c.description));
      c.warnings.forEach(w => li.append(mk("div", "acap-warn", T("ch.cap.warn", capNote("ch.cap.w.", w)))));
      li.append(mk("div", "aask-h", T("ch.cap.runs", c.command)));
      if (c.keys.length) li.append(mk("div", "aask-h", T("ch.cap.keys", c.keys.join(", "))));
      ul.append(li);
    });
    (f.notes || []).forEach(n => ul.append(mk("li", "aask-h", n)));
    return ul;
  }
  const optLabel = (b, o) => (b.kind === "capability_choice" ? capLabel(o) : b.kind === "parked_question" ? (o.label || String(o.value || "").replace(/^other:/, "")) : b.mode === "confirm" ? (o.value === "always" ? T("ch.always", o.rule || "") : o.value === "chat" ? T("ch.chat", o.rule || "") : T(o.value === "confirm" ? (b.kind === "permission" ? "ch.once" : "ch.yes") : "ch.no")) : o.label);

  /* the sentence of their own for a question left for the person: sent after "other:" */
  function parkedOther(b) {
    const say = mk("div", "aask-b"), input = mk("input"), go = btn(T("ch.cap.send"), "primary", () => { if (input.value.trim()) answer(b, "other:" + input.value.trim().slice(0, 500)); });
    input.type = "text"; input.maxLength = 500; input.placeholder = T("ch.ask.other.ph"); input.setAttribute("aria-label", T("ch.ask.other.ph"));
    input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
    say.append(input, go);
    return say;
  }

  function makeBlock(b) {
    if (b.t === "reasoning") {
      const d = mk("details", "think"); d.hidden = true;
      d.append(Object.assign(mk("summary"), {}), mk("div", "think-b"));
      d.querySelector("summary").append(mk("span", "think-t"));
      d.querySelector(".think-b").tabIndex = 0; d.querySelector(".think-b").setAttribute("role", "region"); d.querySelector(".think-b").setAttribute("aria-label", T("ch.reasoning"));
      d.append(mk("div", "think-end", "")); d.querySelector(".think-end").append(copyButton(() => b.s || ""));
      const wrap = mk("div", "ablock"); wrap.append(d); window.chatUi.watchThink(wrap); return wrap;
    }
    if (b.t === "text") return mk("div", "answer");
    if (b.t === "tool") {
      const d = mk("details", "atool"), s = mk("summary"), a = mk("pre", "atool-a"), o = mk("pre", "atool-o");
      s.append(mk("span", "atool-d"), mk("span", "atool-n"), mk("span", "atool-s"));
      d.append(s, a, o); return d;
    }
    if (b.t === "opt") {
      const d = mk("details", "atool aopt"), sum = mk("summary"), body = mk("pre", "atool-o");
      sum.append(mk("span", "atool-d")); d.append(sum, body); return d;
    }
    if (b.t === "ask") return mk("div", "aask");
    if (b.t === "blocked") return mk("div", "ablocked");
    return mk("div", "anote");
  }
  /* where a web tool got its information, for the buttons that open it: the page a tool read, and the addresses of a search's results */
  function sourcesOf(b) {
    if (b.name !== "search_web" && b.name !== "browse_url") return [];
    const found = [], add = u => {
      try {
        const x = new URL(u);
        if ((x.protocol === "https:" || x.protocol === "http:") && !x.username && !x.password && !found.some(f => f.url === x.href)) found.push({ url: x.href, host: x.hostname.replace(/^www\./, "") });
      } catch (e) { /* not an address */ }
    };
    if (b.args && typeof b.args.url === "string") add(b.args.url);
    if (b.name === "search_web") (String(b.output || "").match(/https?:\/\/[^\s"'<>\\)\]]+/g) || []).forEach(u => add(u.replace(/[.,;:!?]+$/, "")));
    return found.slice(0, 6);
  }
  function paintBlock(b, el, item) {
    if (b.t === "reasoning") { window.chatUi.paintThink(el, { reasoning: b.s, text: b.over || item.done ? "x" : "" }, !(b.over || item.done)); el.hidden = !b.s; return; }
    if (b.t === "text") { window.chatUi.renderMarkdown(el, b.s); return; }
    if (b.t === "note") { el.textContent = b.s; el.hidden = !!b.gone; if (b.wait) el.setAttribute("role", "status"); return; }
    if (b.t === "opt") {                                                        // what the model read instead of the person's message, closed until asked for
      el.querySelector(".atool-d").textContent = T("ch.opt.title");
      el.title = T("ch.opt.tip");
      el.querySelector("pre").textContent = b.text;
      let act = el.querySelector(".aopt-act");
      if (!act && b.original) {
        act = btn(T("ch.opt.original"), "ghost aopt-act", () => sendOriginal(b.original, act));
        act.title = T("ch.opt.original.tip");
        el.append(act);
      }
      return;
    }
    if (b.t === "tool") {
      el.dataset.s = b.status;
      const said = explainOf(b.description), head = el.querySelector(".atool-d");
      head.textContent = actionText(b); head.title = said ? T("ch.explain.said", said) : "";
      el.querySelector(".atool-n").textContent = b.name;
      el.querySelector(".atool-s").textContent = " · " + T("ch.tool." + b.status);
      el.querySelector(".atool-a").textContent = JSON.stringify(b.args, null, 1);
      el.querySelector(".atool-o").textContent = b.output;
      el.querySelector(".atool-o").hidden = !b.output;
      const made = /^\[file:([a-f0-9]{12}):([^\]]{1,120})\]/.exec(b.output || "");          // written by the tool, never by the model's own text
      let save = el.querySelector(".asave");
      const wrote = ["write_file", "edit_file"].includes(b.name) && b.status === "ok" && b.args && typeof b.args.path === "string" ? b.args.path : "";   // a file it wrote or edited can be saved too
      if (wrote && !save) { const leaf = wrote.split(/[\\/]/).pop(); save = btn(T("ch.out.save", leaf), "ghost asave", () => saveWorkspace(wrote, leaf, save)); el.querySelector("summary").after(save); }
      if (made && !save) { save = btn(T("ch.out.save", made[2]), "primary asave", () => saveOutput(made[1], made[2], save)); el.querySelector("summary").after(save); }
      let chip = el.querySelector(".chip");
      const pid = b.name.startsWith("plan_") ? planIdOf(b) : "";
      if (pid && !chip) { chip = btn(T("ch.plan.open"), "ghost chip", () => openPlan(pid)); el.querySelector("summary").after(chip); }
      const sources = sourcesOf(b);
      let row = el.querySelector(".asrc");
      if (sources.length && !row) { row = mk("div", "asrc"); el.querySelector("summary").after(row); }
      if (row) row.replaceChildren(...sources.map(s => window.chatUi.sourceLink(s.url, s.host)));
      return;
    }
    if (b.t === "ask" && b.kind === "questions") {                            // the card of questions: choices, a row of their own, free text, yes/no
      const prompt = $("#prompt"), free = !prompt || !prompt.value.trim() || document.activeElement === prompt;
      window.AskCard.draw(el, b, { T, answered: b.answered !== null, focusFirst: b.fresh && free && !document.hidden, send: (value, how) => answerCard(b, value, how) });
      b.fresh = false;
      return;
    }
    if (b.t === "ask" && b.kind === "code") {                                  // a second-factor code: typed here, it goes to the tool and nowhere else
      el.dataset.mode = b.mode;
      const done = b.answered !== null;
      const input = mk("input"); input.type = "password"; input.autocomplete = "one-time-code"; input.inputMode = "numeric"; input.maxLength = 32;
      input.setAttribute("aria-label", T("ch.code.label")); input.disabled = done;
      const go = btn(T("ch.code.send"), "primary", () => { if (input.value.trim()) answer(b, input.value.trim()); }); go.disabled = done;
      input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
      const row = mk("div", "aask-b"); row.append(input, go);
      el.replaceChildren(mk("p", "aask-q", askQuestion(b)), row);
      if (done) el.append(mk("p", "aask-h", T("ch.code.sent")));
      return;
    }
    if (b.t === "ask" && b.kind === "mcp_elicitation") {                      // a tool server stops to ask for a few values: typed here, sent to that server only
      el.dataset.mode = b.mode;
      const done = b.answered !== null;
      if (el.dataset.built && !done) return;                                  // the person is typing: the form is not drawn again
      el.dataset.built = "1";
      const f = b.facts || {}, form = mk("div", "aask-f"), fields = [], row = mk("div", "aask-b"), note = mk("p", "aask-h"), prefix = "elicit:";
      (f.fields || []).forEach((fd, i) => {
        const id = `mcpf-${b.id}-${i}`, wrap = mk("div", "aask-field"), label = mk("label", "", fd.title + (fd.required ? " *" : ""));
        let input;
        label.htmlFor = id;
        if (fd.type === "choice") {
          input = mk("select");
          if (!fd.required) input.append(mk("option", "", ""));
          (fd.options || []).forEach(o => { const op = mk("option", "", String(o)); op.value = String(o); input.append(op); });
        } else {
          input = mk("input");
          input.type = fd.type === "boolean" ? "checkbox" : fd.type === "string" ? "text" : "number";
          if (fd.type === "number") input.step = "any";
          if (fd.type === "boolean") input.checked = fd.default === true;
          else if (fd.default !== undefined) input.value = String(fd.default);
        }
        input.id = id; input.disabled = done; input.required = !!fd.required;
        wrap.append(label, input);
        if (fd.description) wrap.append(mk("div", "aask-h", fd.description));
        form.append(wrap);
        fields.push([fd, input]);
      });
      const valueOf = ([fd, input]) => {
        if (fd.type === "boolean") return input.checked;
        if (input.value === "") return undefined;
        if (fd.type === "integer" || fd.type === "number") return Number(input.value);
        if (fd.type === "choice") return (fd.options || []).find(o => String(o) === input.value);
        return input.value;
      };
      const send = btn(T("ch.mcp.send"), "primary", () => {
        const content = {};
        for (const pair of fields) {
          const v = valueOf(pair);
          if (v !== undefined) content[pair[0].name] = v;
          else if (pair[0].required) { note.textContent = T("ch.mcp.need", pair[0].title); pair[1].focus(); return; }
        }
        const text = prefix + JSON.stringify(content);
        if (text.length > prefix.length + 500) { note.textContent = T("ch.mcp.toolong"); return; }
        answer(b, text);
      });
      send.disabled = done;
      row.setAttribute("role", "group"); row.setAttribute("aria-label", T("ch.answers"));
      row.append(send);
      (b.options || []).forEach(o => {
        const x = btn(T(o.value === "decline" ? "ch.mcp.decline" : "ch.mcp.cancel"), "", () => answer(b, o.value));
        x.disabled = done;
        if (done && b.answered === o.value) x.classList.add("picked");
        row.append(x);
      });
      el.replaceChildren(mk("p", "aask-q", askQuestion(b)), mk("blockquote", "aask-quote", f.message || ""), mk("p", "aask-h", T("ch.mcp.warn")), form, row, note);
      if (done) {
        const sent = String(b.answered).startsWith(prefix);
        el.append(mk("p", "aask-h", sent ? T("ch.mcp.sent") : T("ch.answered", T(b.answered === "decline" ? "ch.mcp.decline" : "ch.mcp.cancel"))));
      }
      return;
    }
    if (b.t === "ask" && b.kind === "capability_choice") {                    // the candidates found online: one button each, or a sentence after Other
      el.dataset.mode = b.mode;
      const done = b.answered !== null, row = mk("div", "aask-b"), say = mk("div", "aask-b"), input = mk("input");
      const go = btn(T("ch.cap.send"), "primary", () => { if (input.value.trim()) answer(b, "other:" + input.value.trim().slice(0, 500)); });
      row.setAttribute("role", "group"); row.setAttribute("aria-label", T("ch.answers"));
      input.type = "text"; input.maxLength = 500; input.placeholder = T("ch.cap.say"); input.setAttribute("aria-label", T("ch.cap.say"));
      input.onkeydown = e => { if (e.key === "Enter") { e.preventDefault(); go.click(); } };
      say.append(input, go); say.hidden = true;
      (b.options || []).forEach(o => {
        const x = btn(capLabel(o), o.value.startsWith("install:") ? "primary" : "", () => { if (o.value === "other") { say.hidden = false; input.focus(); } else answer(b, o.value); });
        x.disabled = input.disabled = go.disabled = done;
        if (done && (b.answered === o.value || (o.value === "other" && String(b.answered).startsWith("other:")))) x.classList.add("picked");
        row.append(x);
      });
      el.replaceChildren(mk("p", "aask-q", askQuestion(b)), capCandidates(b.facts || {}), row, say);
      if (done) el.append(mk("p", "aask-h", T("ch.answered", capLabel({ value: String(b.answered) }))));
      return;
    }
    if (b.t === "ask") {
      el.dataset.mode = b.mode;
      const q = mk("p", "aask-q", askQuestion(b)), row = mk("div", "aask-b");
      row.setAttribute("role", "group"); row.setAttribute("aria-label", T("ch.answers"));
      const done = b.answered !== null;
      (b.options || []).forEach(o => {
        const x = btn(optLabel(b, o), o.value === "confirm" ? "primary" : "", () => answer(b, o.value));
        x.disabled = done;
        if (done && b.answered === o.value) x.classList.add("picked");
        row.append(x);
      });
      el.replaceChildren(q, ...(b.explain ? [mk("p", "aask-h", T("ch.explain.said", b.explain))] : []), row);
      if (b.pending && !done) el.append(mk("p", "aask-h", T("ch.pending.h", b.source && b.source.named ? T("ch.pending.src." + b.source.named) : "")));
      if (b.kind === "parked_question" && !done) el.append(parkedOther(b));
      if (done) el.append(mk("p", "aask-h", T("ch.answered", optLabel(b, (b.options || []).find(o => o.value === b.answered) || { label: b.answered, value: b.answered }))));
      return;
    }
    if (b.t === "blocked") {
      const t = T("ch.blocked." + b.reason); const known = t !== "ch.blocked." + b.reason;
      el.replaceChildren(mk("b", "", known ? t : T("ch.blocked.other")), mk("p", "", b.detail || ""));
      const row = mk("div", "aask-b");
      row.append(btn(T("ch.resume"), "primary", () => control("resume")), btn(T("ch.close"), "", () => control("close")));
      if (!b.options.length) row.hidden = true;
      el.append(row);
    }
  }
  const answerText = it => it.blocks.filter(x => x.t === "text").map(x => x.s).join("\n\n");
  /* the line under an answer that the output limit cut and the model is being asked to go on: "(1 of 2)" */
  function paintCont(it) {
    const on = !it.done && !!it.contNote;
    if (!it.contEl && !on) return;
    if (!it.contEl) { it.contEl = mk("div", "astop"); it.contEl.setAttribute("role", "status"); it.body.after(it.contEl); }
    it.contEl.hidden = !on;
    if (on) it.contEl.textContent = T("ch.ev.answer_truncated", it.contNote.n, it.contNote.of);
  }
  /* why an answer ended when it did not simply finish: out of steps (with a button to go on), out of budget, or repeating itself */
  function paintStop(it) {
    const kind = it.done ? (it.cut && !["max_turns", "budget", "loop"].includes(it.stop) ? "length" : it.stop) : "";   // an answer the output limit ended is cut, whatever else ended the run
    if (!["max_turns", "budget", "loop", "length"].includes(kind)) return;
    if (!it.stopEl) {
      const steps = it.steps > 0 ? it.steps : (rec && rec.max_turns > 0 ? rec.max_turns : "?");
      it.stopEl = mk("div", "astop"); it.stopEl.setAttribute("role", "status");
      it.stopEl.append(mk("span", "", kind === "max_turns" ? T("ch.stop.max_turns", steps) : T("ch.stop." + kind)));
      if (kind === "max_turns") {
        it.goEl = btn(T("ch.continue"), "primary", () => send(T("ch.continue.text")));
        it.stopEl.append(it.goEl, mk("span", "aask-h", T("ch.stop.max_turns.h")));
      } else if (kind === "length") {
        it.goEl = btn(T("ch.continue"), "primary", () => send(T("ch.continue.cut.text")));
        it.stopEl.append(it.goEl, mk("span", "aask-h", T("ch.stop.length.h")));
      }
      it.body.after(it.stopEl);
    }
    if (it.goEl) it.goEl.hidden = busyNow || items.slice(items.indexOf(it) + 1).some(x => x.kind === "user");    // only the latest answer can be continued
  }
  function paintNote(it) {
    const row = mk("div", "arow"), body = mk("span");
    row.append(window.chatUi.stamp(it.ts), body);
    it.el.replaceChildren(row);
    if (it.build) { body.append(it.build()); return; }
    body.append(mk("span", "", it.s));
    if (it.route && it.how === "queue" && !it.over) {                         // a request that waits: it can be made urgent or taken out
      body.append(btn(T("ch.promote"), "ghost", () => tell("promote", it.route)), btn(T("ch.drop"), "ghost", () => tell("drop", it.route)));
    }
  }
  async function tell(what, turn) {
    const r = await api("POST", `/${sid}/${what}`, { turn });
    if (!r.ok) status(errText(r));
  }
  function render() {
    empty.hidden = items.length > 0;
    items.forEach(it => {
      if (it.kind === "note") {
        if (!it.el) { it.el = mk("div", "anote cmsg"); box.insertBefore(it.el, null); it.dirty = true; }
        if (it.dirty) { paintNote(it); it.dirty = false; }
        return;
      }
      if (!it.el) {
        it.el = mk("article", `cmsg ${it.kind}`);
        if (it.kind === "user") {
          const bubble = mk("div", "bubble"), row = mk("div", "arow");
          if (it.files && it.files.length) bubble.append(mk("div", "achips", it.files.map(f => f.name).join(" · ")));
          bubble.append(mk("div", "", it.text));
          row.append(window.chatUi.stamp(it.ts), bubble);
          it.el.append(row);
          it.row = row;
          const acts = mk("div", "aacts");
          acts.append(copyButton(() => it.text || ""), backButton(it), skillButton(it));
          it.el.append(acts);
        }
        else { it.body = mk("div", "ablocks"); it.metaEl = mk("div", "meta"); it.el.append(it.body, it.metaEl); }
        box.insertBefore(it.el, null);
      }
      if (it.kind !== "assistant") return;
      if (it.done && !it.actsEl) {                                              // at the end of an answer: copy it, or go on from there in a conversation of its own
        it.actsEl = mk("div", "aacts aend");
        const branchBtn = btn(T("ch.branch.from"), "ghost abranch", async () => {
          if (busyNow || state === "busy" || state === "asking") { status(T("ch.back.busy")); return; }
          const turn = items.slice(0, items.indexOf(it)).filter(x => x.kind === "user").length;
          if (turn) await branch(turn);
        });
        branchBtn.title = T("ch.branch.from.tip");
        branchBtn.prepend(icon(BRANCH_ICON));
        const save = btn(T("sf.btn"), "ghost savefile", () => {
          const text = answerText(it);
          if (text && window.BasaltSave) window.BasaltSave.message(save, { text, time: it.ts || 0, turn: items.indexOf(it) + 1 });     // only on the person's click
        });
        save.title = T("sf.tip");
        it.actsEl.append(copyButton(() => answerText(it)), save, branchBtn);
        it.el.append(it.actsEl);
      }
      it.blocks.forEach(b => {
        if (!b.el) { b.el = makeBlock(b); b.row = mk("div", "arow"); b.row.append(window.chatUi.stamp(b.ts), b.el); it.body.append(b.row); b.dirty = true; }
        if (b.dirty || b.t === "reasoning" || b.t === "text") { paintBlock(b, b.el, it); b.dirty = false; }
        b.row.hidden = b.el.hidden;                                           // a reasoning box with nothing in it shows no time either
      });
      it.metaEl.textContent = it.meta;
      paintCont(it);
      paintStop(it);
    });
    if (chipsEl) box.append(chipsEl);                                          // the chips stay below the last answer
    scrollDown();
    setControls();
  }

  /* ---- going back: the conversation returns to just after one of the person's requests (what came later is taken off, a copy is kept) ---- */
  function backButton(it) {
    let step = 0, timer = 0, note = null;                  // 0 at rest, 1 asked once, 2 the actions that stay done were shown
    const reset = () => { step = 0; clearTimeout(timer); b.textContent = T("ch.back"); if (note) { note.remove(); note = null; } };
    const arm = (text, ms) => { clearTimeout(timer); b.textContent = text; timer = setTimeout(reset, ms); };
    const b = btn(T("ch.back"), "ghost aback", async () => {
      if (busyNow || state === "busy" || state === "asking") { status(T("ch.back.busy")); return; }
      const turn = items.filter(x => x.kind === "user").indexOf(it) + 1;
      if (step === 0) { step = 1; arm(T("ch.back.sure"), 5000); return; }
      if (step === 1) {                                     // what came after is looked at first: an action that cannot be undone is named
        const seen = await api("GET", `/${sid}/back-impact?turn=${turn}`);
        if (seen.ok && seen.json.needs_yes) {
          const stays = seen.json.not_undone, ul = mk("ul", "astays-l");
          stays.forEach(a => ul.append(mk("li", "", a.effect === "unknown" ? `${a.what} (${T("ch.back.unknown")})` : a.what)));
          note = mk("div", "astays"); note.setAttribute("role", "alert");
          note.append(mk("b", "", T("ch.back.stays", stays.length)), ul);
          b.after(note); step = 2; arm(T("ch.back.stays.go", stays.length), 20000);
          return;
        }
      }
      const accept = step === 2;
      reset();
      const r = await api("POST", `/${sid}/back`, { turn, ...(accept ? { accept_irreversible: true } : {}) });
      if (!r.ok) { status(errText(r)); return; }
      const keep = sid; await openSession(keep); status(T("ch.back.done"));
    });
    b.setAttribute("aria-label", T("ch.back.n", it.text.slice(0, 60)));
    return b;
  }

  /* ---- making a skill from requests: "Make a skill" on a request puts a box beside each request up to it, the person checks the ones that
     belong to the task, and pskills.js goes on from there; /start-learn-skill and /stop-learn-skill mark the first and the last request ---- */
  function requestsOf() { return items.filter(x => x.kind === "user"); }
  const GAP_S = 30 * 60;
  function suggested(end) {                                     // back from the request pressed, while the requests follow each other closely
    const users = requestsOf(), out = [end];
    for (let n = end - 1; n >= 1 && out.length < 10; n--) {
      const before = users[n - 1], after = users[n];
      if (before.ts && after.ts && after.ts - before.ts > GAP_S) break;
      out.push(n);
    }
    return out;
  }
  function paintPick() {
    requestsOf().forEach((it, i) => {
      const n = i + 1, want = !!pick && n <= pick.end && !!it.row;
      if (want && !it.box) {
        const cb = mk("input", "askill-box"); cb.type = "checkbox";
        cb.setAttribute("aria-label", T("psk.sel.check", n));
        cb.onchange = () => { if (!pick) return; if (cb.checked) pick.picked.add(n); else pick.picked.delete(n); paintPick(); };
        it.row.prepend(cb); it.box = cb;
      }
      if (!want && it.box) { it.box.remove(); it.box = null; }
      if (it.box) it.box.checked = pick.picked.has(n);
    });
    bar.hidden = !pick;
    if (pick) $("#pskill-bar-text").textContent = T("psk.sel.bar", [...pick.picked].sort((a, b) => a - b).join(", ") || "-", pick.end);
  }
  function startPick(end, picked) {
    if (!sid || !window.pskills) return;
    pick = { end, picked: new Set(picked || suggested(end)) };
    paintPick();
    bar.scrollIntoView({ block: "nearest" });
  }
  $("#pskill-cancel").onclick = () => { pick = null; paintPick(); };
  $("#pskill-go").onclick = () => {
    if (!pick || !pick.picked.size) { status(T("psk.sel.none")); return; }
    const turns = [...pick.picked].sort((a, b) => a - b);
    pick = null; paintPick();
    window.pskills.make(sid, turns);
  };
  function skillButton(it) {
    const b = btn(T("psk.make.btn"), "ghost aback askill", () => startPick(requestsOf().indexOf(it) + 1));
    b.title = T("psk.make.btn.title");
    b.setAttribute("aria-label", `${T("psk.make.btn")}: ${it.text.slice(0, 60)}`);
    return b;
  }
  function paintLearn() {
    badge.hidden = !learn;
    if (learn) badge.textContent = T("psk.learn.on", learn.from);
  }
  const skills = {
    startLearn() { learn = { from: requestsOf().length + 1 }; paintLearn(); note(T("psk.learn.started")); },
    stopLearn() {
      if (!learn) { note(T("psk.learn.none")); return; }
      const end = requestsOf().length, from = learn.from;
      learn = null; paintLearn();
      if (end < from || !sid) { note(T("psk.learn.empty")); return; }
      startPick(end, Array.from({ length: end - from + 1 }, (_, k) => from + k));
    },
    learning: () => !!learn,
  };

  /* ---- files to attach: uploaded to this conversation as they are picked ---- */
  let pend = [];                                      // [{id, name, family, size} | {name, uploading, pct}]
  const sizeText = n => n >= 1e6 ? (n / 1e6).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1e3)) + " kB";
  function drawPend() {
    const row = $("#aatt");
    row.hidden = !pend.length || !active();
    row.replaceChildren(...pend.map((f, k) => {
      const d = mk("div", "aatt-i"); d.setAttribute("role", "listitem");
      d.append(mk("span", "aatt-n", f.name), mk("span", "dim", f.uploading ? `${f.pct || 0}%` : f.error ? f.error : `${T("ch.fam." + f.family)} · ${sizeText(f.size)}`));
      const rm = btn(T("ch.att.remove"), "ghost", async () => {
        if (f.id) await api2("POST", `${sid}/${f.id}/remove`);
        pend.splice(pend.indexOf(f), 1); drawPend(); $("#prompt").focus();
      });
      rm.setAttribute("aria-label", T("ch.att.remove.n", f.name));
      d.append(rm); return d;
    }));
    setControls();
  }
  const api2 = async (method, path) => { try { const r = await fetch("api/attachments/" + path, { method, headers: { ...authHeaders(), "Content-Type": "application/json" }, body: method === "POST" ? "{}" : undefined }); return r.ok; } catch (e) { return false; } };
  function upload(file) {
    return new Promise(resolve => {
      const f = { name: file.name || T("ch.att.pasted"), uploading: true, pct: 0 };
      pend.push(f); drawPend();
      const x = new XMLHttpRequest();
      x.open("POST", `api/attachments?session=${encodeURIComponent(sid)}&name=${encodeURIComponent(f.name)}`);
      const h = authHeaders(); Object.keys(h).forEach(k => x.setRequestHeader(k, h[k]));
      x.upload.onprogress = e => { if (e.lengthComputable) { f.pct = Math.round(100 * e.loaded / e.total); drawPend(); } };
      x.onload = () => {
        let j = {}; try { j = JSON.parse(x.responseText); } catch (e) { /* no body */ }
        if (x.status === 200 && j.attachment) Object.assign(f, j.attachment, { uploading: false });
        else { f.uploading = false; f.error = (j.error && j.error.message) || `HTTP ${x.status}`; setTimeout(() => { pend = pend.filter(p => p !== f); drawPend(); }, 4000); }
        drawPend(); resolve();
      };
      x.onerror = () => { f.uploading = false; f.error = T("ch.att.fail"); drawPend(); setTimeout(() => { pend = pend.filter(p => p !== f); drawPend(); }, 4000); resolve(); };
      x.send(file);
    });
  }
  async function attachFiles(files) {
    if (!files.length || !(await ensure())) return;
    for (const f of files) await upload(f);
  }
  document.getElementById("attach").addEventListener("click", e => { if (active()) { e.preventDefault(); e.stopImmediatePropagation(); $("#afile").click(); } }, true);
  $("#afile").onchange = e => { attachFiles([...e.target.files]); e.target.value = ""; };
  const dropZone = document.getElementById("view-chat");
  dropZone.addEventListener("dragover", e => { if (active() && [...(e.dataTransfer || {}).types || []].includes("Files")) e.preventDefault(); });
  dropZone.addEventListener("drop", e => { if (active() && e.dataTransfer && e.dataTransfer.files.length) { e.preventDefault(); attachFiles([...e.dataTransfer.files]); } });

  /* ---- a file the agent made: a button to save it where the person chooses ---- */
  async function saveOutput(id, name, button) {
    button.disabled = true;
    try {
      const r = await fetch(`api/outputs/${encodeURIComponent(sid)}/${encodeURIComponent(id)}`, { headers: authHeaders() });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      await saveBlob(await r.blob(), name);
    } catch (e) { if (!(e && e.name === "AbortError")) status(T("ch.out.fail", name)); }
    button.disabled = false;
  }
  /* a file of the conversation's folder: the button of a link the agent wrote, or of a file it wrote or edited */
  async function saveWorkspace(path, name, button) {
    if (button) button.disabled = true;
    try {
      const r = await fetch(`api/agent/sessions/${encodeURIComponent(sid)}/file?path=${encodeURIComponent(path)}`, { headers: authHeaders() });
      if (!r.ok) {
        let why = ""; try { why = ((await r.json()).error || {}).message || ""; } catch (e) { /* no body */ }
        throw new Error(why || `HTTP ${r.status}`);
      }
      await saveBlob(await r.blob(), name || path.split(/[\\/]/).pop());
    } catch (e) { if (!(e && e.name === "AbortError")) status(T("ch.out.fail", name || path) + (e && e.message ? ` (${e.message})` : "")); }
    if (button) button.disabled = false;
  }
  /* every button a link to a file became, in any answer, saves through the same two functions */
  box.addEventListener("click", e => {
    const b = e.target.closest && e.target.closest("button.afile");
    if (!b || !sid) return;
    if (b.dataset.kind === "file") saveOutput(b.dataset.id, b.dataset.name, b);
    else saveWorkspace(b.dataset.path, b.dataset.name, b);
  });
  async function saveBlob(blob, name) {
    try {
      const app = window.chrome && window.chrome.webview;
      if (app) {                                              // the desktop app opens "Save as" itself and says how it ended
        const ended = new Promise(done => {
          const timer = setTimeout(() => { app.removeEventListener("message", on); done("failed"); }, 600000);
          const on = e => {
            const m = e.data || {};
            if (m.type !== "download" || m.name !== name) return;
            clearTimeout(timer); app.removeEventListener("message", on); done(m.state);
          };
          app.addEventListener("message", on);
        });
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove();
        const state = await ended;
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        if (state === "completed") status(T("ch.out.saved", name)); else if (state !== "cancelled") status(T("ch.out.fail", name));
      } else if (window.showSaveFilePicker) {                 // the system's "Save as" window, with the file's own name
        const h = await window.showSaveFilePicker({ suggestedName: name });
        const w = await h.createWritable(); await w.write(blob); await w.close();
        status(T("ch.out.saved", name));
      } else {
        const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = name; document.body.append(a); a.click(); a.remove();
        setTimeout(() => URL.revokeObjectURL(a.href), 30000);
        status(T("ch.out.saved", name));
      }
    } catch (e) { if (!(e && e.name === "AbortError")) status(T("ch.out.fail", name)); }
  }

  /* ---- what a new conversation may do without asking: its mode and the rules that let a kind of command run ---- */
  const PERM_KEY = "basalt.chat.perm", ALLOW_KEY = "basalt.chat.allow";
  const permRadios = [...document.querySelectorAll('input[name="cs-perm"]')], allowBox = $("#cs-allow"), autoRisk = $("#cs-auto-risk");
  const permMode = () => (permRadios.find(r => r.checked) || {}).value || "default";
  const PERM_CYCLE = ["default", "plan", "read_only", "auto"];                // the order Shift+Tab goes through
  const permNow = () => (active() && sid && rec && rec.mode) ? rec.mode : load(PERM_KEY, "default");
  const chip = $("#mode-chip");
  function paintChip() {
    const on = active() && mode !== "ask";                                    // a conversation without tools has no mode to show
    chip.hidden = !on;
    if (!on) return;
    const m = permNow();
    chip.dataset.mode = m;
    chip.replaceChildren(mk("span", "mc-hint", T("ch.chip.hint")), mk("b", "mc-mode", T("ag.mode." + m)));
    chip.setAttribute("aria-label", T("ch.chip", T("ag.mode." + m)));
    chip.title = T("ag.mode." + m + ".d");
  }
  window.basaltAutoMode = () => permNow() === "auto";                         // the risk questions of other settings answer yes by themselves while this is on
  async function setPerm(m) {                                                // false when the server did not take it
    if (sid) {
      const r = await api("POST", `/${sid}/mode`, { mode: m });
      if (!r.ok) { status(errText(r)); return false; }
      rec = r.json.session;
    } else { keep(PERM_KEY, m); paintPerm(); }
    paintChip();
    return true;
  }
  async function cyclePerm() {
    const at = PERM_CYCLE.indexOf(permNow());
    const next = PERM_CYCLE[(at + 1) % PERM_CYCLE.length];
    if (await setPerm(next)) status(T("ch.mode.now", T("ag.mode." + next)));
  }
  chip.onclick = () => { cyclePerm(); $("#prompt").focus(); };
  const effortSel = $("#effort");
  const effortName = level => { const o = [...effortSel.options].find(x => x.value === (level || "")); return o ? o.text : String(level); };
  effortSel.addEventListener("change", () => { if (active() && sid) api("POST", `/${sid}/effort`, { level: effortSel.value }); });
  function paintPerm() {
    const m = load(PERM_KEY, "default");
    permRadios.forEach(r => { r.checked = r.value === m; });
    autoRisk.replaceChildren(...(m === "auto" ? [riskLabel(T("ag.auto.risk"))] : []));
  }
  allowBox.value = load(ALLOW_KEY, "");
  allowBox.oninput = () => keep(ALLOW_KEY, allowBox.value);
  permRadios.forEach(r => r.addEventListener("change", () => { keep(PERM_KEY, r.value); paintPerm(); paintChip(); }));
  paintPerm();
  const allowRules = () => allowBox.value.split("\n").map(s => s.trim()).filter(Boolean);

  /* ---- the conversation's own copy of the folder: what it changed waits here until the person applies it ---- */
  const copyBar = $("#acopy"), copyText = $("#acopy-text"), copyOpen = $("#acopy-open"), copyPanel = $("#acopy-panel");
  /* pictures and charts: each is switched on by its own box and asked for at a level; they apply to the next new conversation */
  const VIS_KEY = "basalt.visuals";
  const vis = { imgOn: $("#cs-img-on"), imgLevel: $("#cs-img-level"), chartOn: $("#cs-chart-on"), chartLevel: $("#cs-chart-level") };
  (() => {
    let saved = {};
    try { saved = JSON.parse(load(VIS_KEY, "{}")) || {}; } catch (e) { saved = {}; }
    vis.imgOn.checked = !!saved.imgOn; vis.chartOn.checked = !!saved.chartOn;
    if (saved.imgLevel) vis.imgLevel.value = saved.imgLevel;
    if (saved.chartLevel) vis.chartLevel.value = saved.chartLevel;
    const store = () => keep(VIS_KEY, JSON.stringify({ imgOn: vis.imgOn.checked, imgLevel: vis.imgLevel.value, chartOn: vis.chartOn.checked, chartLevel: vis.chartLevel.value }));
    const sync = () => { vis.imgLevel.disabled = !vis.imgOn.checked; vis.chartLevel.disabled = !vis.chartOn.checked; store(); };
    Object.values(vis).forEach(e => e.addEventListener("change", sync));
    sync();
  })();
  const visuals = () => ({ images: vis.imgOn.checked ? vis.imgLevel.value : "", charts: vis.chartOn.checked ? vis.chartLevel.value : "" });
  const copyMode = () => "auto";                              // the page does not offer a choice: a copy is made only when the server finds another conversation in the same git folder
  let copyShown = false;
  const kindText = s => T("wc." + (["added", "deleted"].includes(s) ? s : "modified"));
  function paintCopy() {
    const on = active() && !!(rec && rec.worktree);
    copyBar.hidden = !on;
    copyText.textContent = on ? T("wc.in.copy") : "";
    copyOpen.hidden = !on;
    if (!on) { copyPanel.hidden = true; copyShown = false; return; }
    copyOpen.textContent = T(copyShown ? "wc.hide" : "wc.review");
    copyOpen.setAttribute("aria-expanded", String(copyShown));
    copyPanel.hidden = !copyShown;
  }
  async function loadCopy(message) {
    if (!sid || !rec || !rec.worktree) return;
    const r = await api("GET", `/${sid}/copy`);
    copyPanel.replaceChildren();
    if (!r.ok) { copyPanel.append(mk("p", "acopy-msg", T("wc.fail", errText(r)))); return; }
    const changes = r.json.changes || [];
    copyOpen.textContent = T(copyShown ? "wc.hide" : "wc.review.n", changes.length);
    if (!changes.length) copyPanel.append(mk("p", "acopy-msg", T("wc.none")));
    const ul = mk("ul", "acopy-files");
    for (const c of changes) {
      const li = mk("li", "acopy-file"), label = mk("label"), cb = mk("input");
      cb.type = "checkbox"; cb.checked = true; cb.value = c.path;
      label.append(cb, mk("span", "", c.path));
      const shown = mk("pre", "acopy-diff"); shown.hidden = true;
      const more = btn(T("wc.show"), "ghost", async () => {
        if (shown.hidden && !shown.textContent) {
          const d = await api("GET", `/${sid}/copy/diff?path=${encodeURIComponent(c.path)}`);
          shown.textContent = d.ok ? (d.json.diff || "") : T("wc.fail", errText(d));
        }
        shown.hidden = !shown.hidden;
        more.textContent = T(shown.hidden ? "wc.show" : "wc.hide.one");
      });
      li.append(label, mk("span", "acopy-kind", kindText(c.status)), more, shown);
      ul.append(li);
    }
    copyPanel.append(ul);
    if (changes.length) {
      const acts = mk("div", "acopy-actions");
      acts.append(btn(T("wc.apply.sel"), "primary", () => applyCopy(false)), btn(T("wc.apply.all"), "", () => applyCopy(true)));
      copyPanel.append(acts);
    }
    const drop = btn(T("wc.discard"), "ghost", () => discardCopy(drop));
    copyPanel.append(drop);
    copyPanel.append(mk("p", "acopy-msg", message || (changes.length ? T("wc.kept") : "")));
    copyPanel.lastChild.setAttribute("role", "status");
  }
  async function applyCopy(all) {
    const paths = all ? null : [...copyPanel.querySelectorAll('.acopy-file input[type="checkbox"]:checked')].map(i => i.value);
    if (!all && !paths.length) { loadCopy(T("wc.pick")); return; }
    const r = await api("POST", `/${sid}/copy/apply`, paths ? { paths } : {});
    if (!r.ok) { loadCopy(r.status === 409 && r.json.error && r.json.error.code === "busy" ? T("wc.busy") : T("wc.fail", errText(r))); return; }
    rec = r.json.session || rec;
    loadCopy(T("wc.applied", (r.json.applied || []).join(", ") || "-"));
  }
  let armed = 0;
  async function discardCopy(button) {
    if (!armed) {                                              // a second press within a few seconds: no dialog to answer, nothing lost by a stray tap
      button.textContent = T("wc.discard.again"); status(T("wc.discard.sure"));
      armed = setTimeout(() => { armed = 0; if (button.isConnected) button.textContent = T("wc.discard"); }, 6000);
      return;
    }
    clearTimeout(armed); armed = 0;
    const r = await api("POST", `/${sid}/copy/discard`, {});
    if (!r.ok) { loadCopy(r.status === 409 ? T("wc.busy") : T("wc.fail", errText(r))); return; }
    rec = r.json.session; copyShown = false; paintCopy(); status(T("wc.discarded")); await refreshList();
  }
  copyOpen.onclick = () => { copyShown = !copyShown; paintCopy(); if (copyShown) loadCopy(); };

  /* ---- talking to the conversation ---- */
  async function ensure() {
    if (sid) return true;
    if (inAgent() && !curPath() && !(await addFolder())) { status(T("ag.noproject")); return false; }
    const r = await api("POST", "", { ...(inAgent() ? { workspace: curPath(), ...(window.codingUi ? { role: window.codingUi.role() } : {}) } : {}), mode: permMode(), allow: allowRules(), ask_only: mode === "ask", effort: effortSel.value, context_patterns: chatSettings.patterns(), context_policy: chatSettings.policy(), context_threshold: chatSettings.threshold(), ...chatSettings.live(), worktree: copyMode(), ...visuals() });
    if (!r.ok) { status(errText(r)); return false; }
    sid = r.json.session.id; rec = r.json.session; keep(lastKey(), sid); after = 0;
    await refreshList();
    return true;
  }
  async function send(text) {
    if (!text) return;
    if (!(await ensure())) { window.chatUi.setPrompt(text); return; }
    if (pend.some(f => f.uploading)) { status(T("ch.att.wait")); window.chatUi.setPrompt(text); return; }
    const files = pend.filter(f => f.id);
    const r = await api("POST", `/${sid}/send`, { text, ...(files.length ? { attachments: files.map(f => f.id) } : {}) });
    if (!r.ok) { status(errText(r)); window.chatUi.setPrompt(text); return; }
    document.dispatchEvent(new CustomEvent("basalt:sent", { detail: { text } }));
    clearChips();
    pend = []; drawPend();
    state = "busy"; busyNow = true; status(T("ch.working")); setControls(); poll(true);
  }
  /* "Send my original instead": the person's own text goes as a new message that is not rewritten */
  async function sendOriginal(text, button) {
    if (!text || !sid || busyNow || state === "busy" || state === "asking") { status(T("ch.back.busy")); return; }
    const r = await api("POST", `/${sid}/send`, { text, optimize: false });
    if (!r.ok) { status(errText(r)); return; }
    if (button) button.disabled = true;
    document.dispatchEvent(new CustomEvent("basalt:sent", { detail: { text } }));
    clearChips();
    state = "busy"; busyNow = true; status(T("ch.working")); setControls(); poll(true);
  }
  /* a card of questions: the answer goes to the server as ids and typed text; the server checks it and puts it in words for the agent */
  async function answerCard(b, value, how) {
    const r = await api("POST", `/${sid}/answer`, { ask: b.id, value });
    if (!r.ok) { status(errText(r)); return; }
    b.answered = how; b.status = how; b.dirty = true;
    if (how === "chat") { const p = $("#prompt"); if (p) p.focus(); } else { state = "busy"; busyNow = true; }
    render(); poll(true);
  }
  async function answer(b, value) {
    const r = await api("POST", `/${sid}/answer`, { ask: b.id, value });
    if (!r.ok) { status(errText(r)); return; }
    b.answered = b.kind === "code" ? "-" : value; b.dirty = true; if (!b.pending) state = "busy"; render(); poll(true);
  }
  async function control(what) {
    const r = await api("POST", `/${sid}/${what}`, {});
    if (!r.ok) { status(errText(r)); return; }
    if (what === "close") { newChat(); refreshList(); } else { state = "busy"; busyNow = true; poll(true); }
  }
  async function stop() {
    if (!sid) return;
    const r = await api("POST", `/${sid}/cancel`, {});
    if (!r.ok) status(errText(r)); else status(T("ch.stopping"));
  }

  /* ---- following the conversation ---- */
  function syncRecord() {
    busyNow = state === "busy" || state === "starting" || state === "asking";
    setControls();
  }
  async function poll(now) {
    clearTimeout(timer);
    if (!active() || !sid || document.hidden || !document.getElementById("view-chat") || document.getElementById("view-chat").hidden) {
      if (active()) timer = setTimeout(() => poll(), SLOW_MS);
      return;
    }
    const r = await api("GET", `/${sid}/events?after=${after}`);
    if (r.ok) {
      const before = state;
      r.json.events.forEach(apply);
      after = r.json.next; state = r.json.state;
      syncRecord();
      if (r.json.events.length || before !== state) render();
      if (before !== state && (state === "idle" || state === "dormant")) { status(T("ch.done")); refreshList(); }
    } else if (r.status === 404) { newChat(); }
    timer = setTimeout(() => poll(), busyNow ? FAST_MS : SLOW_MS);
  }
  /* the same page serves two tabs: the conversation held with no folder, and the conversations of the folders added */
  const chgSide = $("#chg-side"), chgOpen = $("#chg-open");
  function showChanges(on) { chgSide.hidden = !on; chgOpen.setAttribute("aria-expanded", String(on)); }
  chgOpen.onclick = () => showChanges(chgSide.hidden);
  $("#chg-close").onclick = () => showChanges(false);
  $("#cv-addfolder").onclick = () => { addFolder(); };
  function paintContext() {
    const agent = inAgent();
    $("#achat-empty").querySelector("b").textContent = T(agent ? "ag.empty.t" : "ch.empty.t");
    $("#achat-empty").querySelector("span").textContent = T(agent ? "ag.empty.b" : "ch.empty.b");
    if (!agent) showChanges(false);
  }
  async function enterContext() {
    const agent = inAgent();
    if (agent === lastCtx) return;
    lastCtx = agent;
    const kept = { chat: load(LAST_KEYS.chat, ""), agent: load(LAST_KEYS.agent, "") };
    forget();                                                                    // what is open belongs to the tab it was opened in
    keep(LAST_KEYS.chat, kept.chat); keep(LAST_KEYS.agent, kept.agent);       // each tab goes back to its own last conversation
    if (agent) mode = "agent";
    else { mode = load(MODE_KEY, "agent"); if (!MODES.includes(mode)) mode = "agent"; }
    modeSel.value = mode;
    paintContext();
    await window.agentProjects.load();
    projects = window.agentProjects.list();
    await refreshList();
    applyMode();
    const last = load(lastKey(), "");
    if (active() && last && known.some(x => x.id === last && inContext(x))) openSession(last);
  }
  window.agentProjects.onChange(list => { projects = list; paintList(); setControls(); });
  document.addEventListener("basalt:view", e => { if (e.detail === "chat" || e.detail === "agent") { enterContext().then(() => { refreshList(); poll(true); }); } });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(true); });

  /* ---- what the slash commands (slash.js) use ---- */
  const answersOf = () => items.filter(it => it.kind === "assistant").map(it => it.blocks.filter(b => b.t === "text").map(b => b.s).join("\n\n")).filter(Boolean);
  function note(plain, build) {                                              // in the conversation; in the model-only chat, on the status line
    if (!active()) { status(plain); return; }
    noteItem({ s: plain, ...(build ? { build } : {}) }); render();
  }
  async function transcript() {
    if (!sid) return "";
    const h = await api("GET", `/${sid}/history`);
    if (!h.ok) { status(errText(h)); return ""; }
    return (h.json.messages || []).map(m => {
      if (m.role === "user") return `${T("ch.you")}: ${typeof m.content === "string" ? m.content : ""}`;
      if (m.role === "assistant") return [m.content ? `${T("ch.agent")}: ${m.content}` : "", ...(m.tool_calls || []).map(tc => `[${(tc.function || {}).name}] ${(tc.function || {}).arguments || ""}`)].filter(Boolean).join("\n");
      return m.role === "tool" ? `  -> ${String(m.content || "").slice(0, SHOWN_OUTPUT)}` : "";
    }).filter(Boolean).join("\n\n");
  }
  async function runOnServer(name, args) {                                  // the process answers with a `command` event, which slash.js draws
    if (!(await ensure())) return false;
    const r = await api("POST", `/${sid}/command`, { name, args });
    if (!r.ok) { status(errText(r)); return false; }
    poll(true);
    return true;
  }
  async function rename(title) {
    if (!sid) return false;
    const r = await api("POST", `/${sid}/rename`, { title });
    if (!r.ok) { status(errText(r)); return false; }
    rec = r.json.session; await refreshList();
    return true;
  }
  /* the conversation as plain data for export.js: each request, and for each answer its reasoning, actions and text, in the order they happened */
  function snapshot() {
    const out = [];
    items.forEach((it, n) => {
      if (it.kind === "user") { out.push({ kind: "prompt", text: it.text || "", time: it.ts || 0, attachments: (it.files || []).map(f => f.name) }); return; }
      if (it.kind !== "assistant") return;
      it.blocks.forEach(b => {
        const time = b.ts || 0;
        if (b.t === "reasoning" && b.s) out.push({ kind: "reasoning", turn: n, text: b.s, time });
        else if (b.t === "text" && b.s) out.push({ kind: "answer", turn: n, text: b.s, time });
        else if (b.t === "tool") out.push({ kind: "action", turn: n, name: b.name, description: b.description || b.desc || "", arguments: b.args, result: b.output || "", status: b.status, sources: sourcesOf(b), time });
      });
    });
    const listed = known.find(s => s.id === sid) || {};
    return { title: (rec && rec.title) || listed.title || "", session: sid, items: out };
  }
  window.agentChat = { record: () => rec, folder: curPath, busy: () => busyNow, skills, snapshot, refresh: () => poll(true), active, send, stop, newChat, current: () => sid, ensure, note, runOnServer, rename, transcript, saveBlob, answers: answersOf,
    perm: { now: permNow, set: setPerm, cycle: cyclePerm, available: () => active() && mode !== "ask", modes: PERM_CYCLE },
    sessions: () => known.filter(s => s.state !== "closed" && inContext(s)).map(s => ({ id: s.id, label: `${s.title || s.id} \u00B7 ${T("ch.state." + s.state)}` })), open: openSession, branch, hasCopy: () => !!(rec && rec.worktree),
    toggleCopy: () => copyOpen.click() };
  document.addEventListener("basalt:open-session", async e => {      // the tree of branches opens a conversation here
    const id = e.detail && e.detail.id;
    if (!id) return;
    await refreshList();
    const there = known.find(x => x.id === id);
    if (there && !inContext(there)) { view(inAgent() ? "chat" : "agent"); await enterContext(); }     // a conversation of the other tab: go there
    if (mode === "direct") { mode = "agent"; modeSel.value = mode; keep(MODE_KEY, mode); applyMode(); }
    await openSession(id);
  });
  window.chatUi.agentActive = active;
  window.chatUi.sessionId = () => sid;

  [box, $("#acopy"), convs].forEach(root => applyT(root));
  convs.setAttribute("aria-label", T("cv.title"));   // the static texts, in the page's language
  (async () => {
    await window.agentProjects.load();
    projects = window.agentProjects.list();
    await refreshList();
    $("#attach").dataset.can = $("#attach").hidden ? "" : "1";
    if (supported === false) { modeSel.querySelectorAll("option").forEach(o => { if (o.value !== "direct") o.disabled = true; }); modeSel.value = "direct"; }
    lastCtx = inAgent();
    if (lastCtx) { mode = "agent"; modeSel.value = mode; }
    paintContext();
    applyMode();
    const last = load(lastKey(), "");
    if (active() && last && known.some(s => s.id === last && inContext(s))) openSession(last);
  })();
})();
